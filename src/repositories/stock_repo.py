# -*- coding: utf-8 -*-
"""
===================================
股票数据访问层
===================================

职责：
1. 封装股票数据的数据库操作
2. 提供日线数据查询接口
"""

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, List, Dict, Any, Callable

import pandas as pd
from sqlalchemy import and_, desc, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)
_SQLITE_CHUNK = 50


class DailyBarInsertConflict(RuntimeError):
    """Raised when an insert-only daily-bar batch cannot be stored intact."""


@dataclass(frozen=True)
class DailyBarSnapshot:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    pct_chg: float
    data_source: str


@dataclass(frozen=True)
class DailyBarBatchInsertResult:
    inserted_count: int
    verified_rows: tuple[DailyBarSnapshot, ...]

    def get_on_date(self, target_date: date) -> Optional[DailyBarSnapshot]:
        return next(
            (row for row in self.verified_rows if row.date == target_date),
            None,
        )


@dataclass(frozen=True)
class ExactPairedForwardBars:
    stock_anchor: Optional[StockDaily]
    benchmark_anchor: Optional[StockDaily]
    stock_bars: List[StockDaily]
    benchmark_bars: List[StockDaily]
    adjustment_marker: Optional[str]
    unable_reason: Optional[str]


class StockRepository:
    """
    股票数据访问层
    
    封装 StockDaily 表的数据库操作
    """
    
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        """
        初始化数据访问层
        
        Args:
            db_manager: 数据库管理器（可选，默认使用单例）
        """
        self.db = db_manager or DatabaseManager.get_instance()
    
    def get_latest(self, code: str, days: int = 2) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        try:
            return self.db.get_latest_data(code, days)
        except Exception as e:
            logger.error(f"获取最新数据失败: {e}")
            return []
    
    def get_range(
        self,
        code: str,
        start_date: date,
        end_date: date
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            StockDaily 对象列表
        """
        try:
            return self.db.get_data_range(code, start_date, end_date)
        except Exception as e:
            logger.error(f"获取日期范围数据失败: {e}")
            return []
    
    def save_dataframe(
        self,
        df: pd.DataFrame,
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存 DataFrame 到数据库
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源
            
        Returns:
            保存的记录数
        """
        try:
            return self.db.save_daily_data(df, code, data_source)
        except Exception as e:
            logger.error(f"保存日线数据失败: {e}")
            return 0

    def insert_missing_dataframe(
        self,
        df: pd.DataFrame,
        code: str,
        data_source: str,
        existing_row_matches: Optional[Callable[[StockDaily, Dict[str, Any]], bool]] = None,
    ) -> int:
        """Return the number of missing rows inserted by the atomic batch."""
        return self.insert_missing_dataframe_verified(
            df,
            code,
            data_source,
            existing_row_matches=existing_row_matches,
        ).inserted_count

    def insert_missing_dataframe_verified(
        self,
        df: pd.DataFrame,
        code: str,
        data_source: str,
        existing_row_matches: Optional[Callable[[StockDaily, Dict[str, Any]], bool]] = None,
    ) -> DailyBarBatchInsertResult:
        """Atomically validate a full batch and insert only its missing rows."""
        if df is None or df.empty:
            return DailyBarBatchInsertResult(0, ())

        now = datetime.now()
        records_by_date: Dict[date, Dict[str, Any]] = {}
        for row in df.to_dict(orient="records"):
            row_date = self.db._normalize_daily_date(row.get("date"))
            if row_date in records_by_date:
                raise DailyBarInsertConflict("daily bar insert conflict")
            required_values = {
                field: self._finite_daily_value(row.get(field))
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "pct_chg",
                )
            }
            records_by_date[row_date] = {
                "code": code,
                "date": row_date,
                **required_values,
                "ma5": self.db._normalize_sql_value(row.get("ma5")),
                "ma10": self.db._normalize_sql_value(row.get("ma10")),
                "ma20": self.db._normalize_sql_value(row.get("ma20")),
                "volume_ratio": self.db._normalize_sql_value(row.get("volume_ratio")),
                "data_source": data_source,
                "created_at": now,
                "updated_at": now,
            }
        records = list(records_by_date.values())
        if not records:
            return DailyBarBatchInsertResult(0, ())

        def _insert(session) -> DailyBarBatchInsertResult:
            try:
                dates = list(records_by_date)
                existing_statement = select(StockDaily).where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date.in_(dates),
                    )
                )
                if not self.db._is_sqlite_engine:
                    existing_statement = existing_statement.with_for_update()
                existing_rows = session.execute(
                    existing_statement
                ).scalars().all()
                existing_by_date = {row.date: row for row in existing_rows}
                for row_date, existing in existing_by_date.items():
                    expected = records_by_date[row_date]
                    if not self._existing_daily_row_matches(
                        existing,
                        expected,
                        data_source=data_source,
                        existing_row_matches=existing_row_matches,
                    ):
                        raise DailyBarInsertConflict("daily bar insert conflict")

                missing_records = [
                    record
                    for record in records
                    if record["date"] not in existing_by_date
                ]
                inserted_count = 0
                if self.db._is_sqlite_engine:
                    for index in range(0, len(missing_records), _SQLITE_CHUNK):
                        chunk = missing_records[index : index + _SQLITE_CHUNK]
                        if not chunk:
                            continue
                        statement = sqlite_insert(StockDaily).values(chunk)
                        statement = statement.on_conflict_do_nothing(
                            index_elements=["code", "date"]
                        )
                        result = session.execute(statement)
                        chunk_count = result.rowcount or 0
                        if chunk_count != len(chunk):
                            raise DailyBarInsertConflict(
                                "daily bar insert conflict"
                            )
                        inserted_count += chunk_count
                else:
                    if missing_records:
                        session.execute(
                            StockDaily.__table__.insert().values(missing_records)
                        )
                    inserted_count = len(missing_records)
            except IntegrityError as exc:
                raise DailyBarInsertConflict("daily bar insert conflict") from exc
            if inserted_count != len(missing_records):
                raise DailyBarInsertConflict("daily bar insert conflict")

            verified_statement = select(StockDaily).where(
                and_(
                    StockDaily.code == code,
                    StockDaily.date.in_(dates),
                )
            )
            if not self.db._is_sqlite_engine:
                verified_statement = verified_statement.with_for_update()
            verified_rows = session.execute(
                verified_statement
            ).scalars().all()
            if len(verified_rows) != len(records):
                raise DailyBarInsertConflict("daily bar insert conflict")
            verified_by_date = {row.date: row for row in verified_rows}
            verified_snapshots = []
            for expected in records:
                verified = verified_by_date.get(expected["date"])
                if expected["date"] in existing_by_date:
                    matches = self._existing_daily_row_matches(
                        verified,
                        expected,
                        data_source=data_source,
                        existing_row_matches=existing_row_matches,
                    )
                else:
                    matches = self._daily_row_matches_expected(
                        verified,
                        expected,
                        data_source=data_source,
                    )
                if not matches:
                    raise DailyBarInsertConflict("daily bar insert conflict")
                verified_snapshots.append(self._snapshot_daily_row(verified))
            return DailyBarBatchInsertResult(
                inserted_count=inserted_count,
                verified_rows=tuple(verified_snapshots),
            )

        return self.db._run_write_transaction(
            f"insert missing daily data[{code}]",
            _insert,
        )

    @classmethod
    def _existing_daily_row_matches(
        cls,
        row: Optional[StockDaily],
        expected: Dict[str, Any],
        *,
        data_source: str,
        existing_row_matches: Optional[Callable[[StockDaily, Dict[str, Any]], bool]],
    ) -> bool:
        if row is None:
            return False
        if existing_row_matches is None:
            return cls._daily_row_matches_expected(
                row,
                expected,
                data_source=data_source,
            )
        try:
            return bool(existing_row_matches(row, expected))
        except Exception as exc:
            raise DailyBarInsertConflict("daily bar insert conflict") from exc

    @classmethod
    def _daily_row_matches_expected(
        cls,
        row: Optional[StockDaily],
        expected: Dict[str, Any],
        *,
        data_source: str,
    ) -> bool:
        return bool(
            row is not None
            and str(row.data_source or "") == data_source
            and all(
                cls._finite_daily_values_equal(
                    getattr(row, field, None),
                    expected[field],
                )
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "pct_chg",
                )
            )
        )

    @classmethod
    def _snapshot_daily_row(cls, row: StockDaily) -> DailyBarSnapshot:
        return DailyBarSnapshot(
            date=row.date,
            open=cls._finite_daily_value(row.open),
            high=cls._finite_daily_value(row.high),
            low=cls._finite_daily_value(row.low),
            close=cls._finite_daily_value(row.close),
            volume=cls._finite_daily_value(row.volume),
            amount=cls._finite_daily_value(row.amount),
            pct_chg=cls._finite_daily_value(row.pct_chg),
            data_source=str(row.data_source or ""),
        )

    @staticmethod
    def _finite_daily_value(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise DailyBarInsertConflict("daily bar insert conflict") from exc
        if not math.isfinite(number):
            raise DailyBarInsertConflict("daily bar insert conflict")
        return number

    @classmethod
    def _finite_daily_values_equal(cls, left: Any, right: Any) -> bool:
        try:
            return cls._finite_daily_value(left) == cls._finite_daily_value(right)
        except DailyBarInsertConflict:
            return False
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否有指定日期的数据
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        try:
            return self.db.has_today_data(code, target_date)
        except Exception as e:
            logger.error(f"检查数据存在失败: {e}")
            return False
    
    def get_analysis_context(
        self, 
        code: str, 
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析上下文
        
        Args:
            code: 股票代码
            target_date: 目标日期
            
        Returns:
            分析上下文字典
        """
        try:
            return self.db.get_analysis_context(code, target_date)
        except Exception as e:
            logger.error(f"获取分析上下文失败: {e}")
            return None

    def get_start_daily(self, *, code: str, analysis_date: date) -> Optional[StockDaily]:
        """Return StockDaily for analysis_date (preferred) or nearest previous date."""
        with self.db.get_session() as session:
            row = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date <= analysis_date))
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            return row

    def get_daily_on_date(self, *, code: str, target_date: date) -> Optional[StockDaily]:
        """Return StockDaily for the exact target_date without trading-day fallback."""
        with self.db.get_session() as session:
            row = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date == target_date))
                .limit(1)
            ).scalar_one_or_none()
            return row

    def get_forward_bars(self, *, code: str, analysis_date: date, eval_window_days: int) -> List[StockDaily]:
        """Return forward daily bars after analysis_date, up to eval_window_days."""
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date > analysis_date))
                .order_by(StockDaily.date)
                .limit(eval_window_days)
            ).scalars().all()
            return list(rows)

    def get_exact_paired_forward_bars(
        self,
        *,
        stock_code: str,
        benchmark_code: str,
        anchor_date: date,
        eval_window_days: int,
    ) -> ExactPairedForwardBars:
        """Return the first aligned tradable bar after cutoff and exact horizon bars."""

        with self.db.get_session() as session:
            stock_anchor = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == stock_code, StockDaily.date > anchor_date))
                .order_by(StockDaily.date)
                .limit(1)
            ).scalar_one_or_none()
            if stock_anchor is None:
                return ExactPairedForwardBars(None, None, [], [], None, "missing_anchor_price")

            benchmark_anchor = session.execute(
                select(StockDaily).where(
                    and_(
                        StockDaily.code == benchmark_code,
                        StockDaily.date == stock_anchor.date,
                    )
                )
            ).scalar_one_or_none()
            if benchmark_anchor is None:
                return ExactPairedForwardBars(
                    stock_anchor, None, [], [], None, "missing_benchmark_anchor"
                )

            stock_bars = list(
                session.execute(
                    select(StockDaily)
                    .where(
                        and_(StockDaily.code == stock_code, StockDaily.date >= stock_anchor.date)
                    )
                    .order_by(StockDaily.date)
                    .limit(eval_window_days)
                ).scalars().all()
            )
            benchmark_bars = list(
                session.execute(
                    select(StockDaily)
                    .where(
                        and_(
                            StockDaily.code == benchmark_code,
                            StockDaily.date >= stock_anchor.date,
                        )
                    )
                    .order_by(StockDaily.date)
                    .limit(eval_window_days)
                ).scalars().all()
            )
            if (
                len(stock_bars) != eval_window_days
                or len(benchmark_bars) != eval_window_days
                or [bar.date for bar in stock_bars] != [bar.date for bar in benchmark_bars]
            ):
                return ExactPairedForwardBars(
                    stock_anchor,
                    benchmark_anchor,
                    stock_bars,
                    benchmark_bars,
                    None,
                    "insufficient_forward_bars",
                )

            markers = {
                self._adjustment_marker(bar.data_source)
                for bar in [stock_anchor, benchmark_anchor, *stock_bars, *benchmark_bars]
            }
            if None in markers or len(markers) != 1:
                return ExactPairedForwardBars(
                    stock_anchor,
                    benchmark_anchor,
                    stock_bars,
                    benchmark_bars,
                    None,
                    "corporate_action_adjustment_unknown",
                )
            return ExactPairedForwardBars(
                stock_anchor,
                benchmark_anchor,
                stock_bars,
                benchmark_bars,
                next(iter(markers)),
                None,
            )

    @staticmethod
    def _adjustment_marker(data_source: Any) -> Optional[str]:
        source = str(data_source or "").strip().lower()
        for marker in ("qfq", "hfq", "unadjusted", "adjusted", "raw", "none"):
            if marker in source:
                return marker
        return None
