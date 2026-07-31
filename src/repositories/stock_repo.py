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
from dataclasses import dataclass
from datetime import date
from typing import Optional, List, Dict, Any

import pandas as pd
from sqlalchemy import and_, desc, select

from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)


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
