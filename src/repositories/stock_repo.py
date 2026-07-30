# -*- coding: utf-8 -*-
"""
===================================
股票数据访问层
===================================

职责：
1. 封装股票数据的数据库操作
2. 提供日线数据查询接口
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional, List, Dict, Any

import pandas as pd
from sqlalchemy import and_, desc, select

from src.storage import DatabaseManager, StockDaily
from src.core.trading_calendar import get_market_for_stock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExactPairedForwardBars:
    stock_observation_anchor: Optional[StockDaily]
    benchmark_observation_anchor: Optional[StockDaily]
    stock_bars: List[StockDaily]
    benchmark_bars: List[StockDaily]
    adjustment_marker: Optional[str]
    unable_reason: Optional[str]
    input_bar_hash: str


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
        stock_market: str,
        benchmark_code: str,
        benchmark_market: str,
        anchor_date: date,
        decision_market_phase: str | None,
        eval_window_days: int,
    ) -> ExactPairedForwardBars:
        """Return point-in-time observations and next-session shadow bars."""

        identity_payload = {
            "stock_market": str(stock_market or "").strip().lower(),
            "stock_code": str(stock_code or "").strip().upper(),
            "benchmark_market": str(benchmark_market or "").strip().lower(),
            "benchmark_code": str(benchmark_code or "").strip().upper(),
            "anchor_date": anchor_date.isoformat(),
            "decision_market_phase": decision_market_phase,
            "eval_window_days": eval_window_days,
        }
        empty_hash = self._bar_input_hash(identity_payload, [])
        if not self._market_symbol_identity_matches(stock_market, stock_code) or not self._market_symbol_identity_matches(
            benchmark_market, benchmark_code
        ):
            return ExactPairedForwardBars(
                None, None, [], [], None, "instrument_identity_ambiguous", empty_hash
            )

        phase = str(decision_market_phase or "").strip().lower()
        partial_phases = {"premarket", "intraday", "lunch_break", "closing_auction"}
        fully_known_phases = {"postmarket", "non_trading"}
        if phase not in partial_phases | fully_known_phases:
            return ExactPairedForwardBars(
                None, None, [], [], None, "execution_anchor_unverified", empty_hash
            )
        observation_operator = StockDaily.date < anchor_date if phase in partial_phases else StockDaily.date <= anchor_date

        with self.db.get_session() as session:
            stock_anchor = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == stock_code, observation_operator))
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            if stock_anchor is None:
                return ExactPairedForwardBars(
                    None, None, [], [], None, "missing_anchor_price", empty_hash
                )

            benchmark_date_operator = (
                StockDaily.date < anchor_date
                if phase in partial_phases
                else StockDaily.date <= anchor_date
            )
            benchmark_anchor = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == benchmark_code, benchmark_date_operator))
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            if benchmark_anchor is None:
                return ExactPairedForwardBars(
                    stock_anchor, None, [], [], None, "missing_benchmark_anchor", empty_hash
                )
            if stock_anchor.date != benchmark_anchor.date:
                return ExactPairedForwardBars(
                    stock_anchor,
                    benchmark_anchor,
                    [],
                    [],
                    None,
                    "observation_anchor_unaligned",
                    empty_hash,
                )

            stock_bars = list(
                session.execute(
                    select(StockDaily)
                    .where(
                        and_(StockDaily.code == stock_code, StockDaily.date > anchor_date)
                    )
                    .order_by(StockDaily.date)
                    .limit(eval_window_days)
                ).scalars().all()
            )
            benchmark_bars = list(
                session.execute(
                    select(StockDaily)
                    .where(
                        and_(StockDaily.code == benchmark_code, StockDaily.date > anchor_date)
                    )
                    .order_by(StockDaily.date)
                    .limit(eval_window_days)
                ).scalars().all()
            )
            input_hash = self._bar_input_hash(
                identity_payload,
                [stock_anchor, benchmark_anchor, *stock_bars, *benchmark_bars],
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
                    input_hash,
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
                    input_hash,
                )
            return ExactPairedForwardBars(
                stock_anchor,
                benchmark_anchor,
                stock_bars,
                benchmark_bars,
                next(iter(markers)),
                None,
                input_hash,
            )

    @staticmethod
    def _market_symbol_identity_matches(market: str, code: str) -> bool:
        expected = str(market or "").strip().lower()
        inferred = get_market_for_stock(str(code or "").strip().upper())
        return bool(expected and inferred and inferred == expected)

    @staticmethod
    def _bar_input_hash(identity: Dict[str, Any], bars: List[StockDaily]) -> str:
        payload = {
            "identity": identity,
            "bars": [
                {
                    "code": bar.code,
                    "date": bar.date.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "data_source": bar.data_source,
                }
                for bar in bars
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _adjustment_marker(data_source: Any) -> Optional[str]:
        source = str(data_source or "").strip().lower()
        for marker in ("qfq", "hfq", "unadjusted", "adjusted", "raw", "none"):
            if marker in source:
                return marker
        return None
