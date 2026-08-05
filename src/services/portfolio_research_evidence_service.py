# -*- coding: utf-8 -*-
"""Prepare bounded market evidence for current portfolio research."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from importlib import metadata
import math
from typing import Any, Callable, Dict, Optional

import pandas as pd

from data_provider.base import DataFetcherManager
from data_provider.tencent_fetcher import TencentFetcher
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock
from src.repositories.portfolio_market_evidence_repo import (
    PortfolioMarketEvidenceRepository,
)
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.portfolio_service import PortfolioService

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency path
    yf = None


class PortfolioResearchEvidenceService:
    """Prepare research inputs without running analysis or mutating the ledger."""

    SCHEMA_VERSION = "portfolio-research-evidence-prepare-v2"
    BENCHMARK_ROUTES = {
        "cn": {
            "storage_code": "000300",
            "fetch_code": "sh000300",
            "provider": "tencent",
            "source": "TencentFetcher",
        },
        "hk": {
            "storage_code": "HSI",
            "fetch_code": "^HSI",
            "provider": "yfinance",
            "source": "YfinanceFetcher",
        },
        "us": {
            "storage_code": "SPY",
            "fetch_code": "SPY",
            "provider": "yfinance",
            "source": "YfinanceFetcher",
        },
    }
    DAILY_LOOKBACK_DAYS = 10
    INITIAL_DAILY_LOOKBACK_DAYS = 260
    MIN_LOCAL_HISTORY_BARS = 200
    FETCH_WARMUP_BARS = 1
    FX_MAX_AGE_DAYS = 7
    LEGACY_SOURCES = frozenset({"TencentFetcher", "YfinanceFetcher"})

    def __init__(
        self,
        portfolio_service: Optional[PortfolioService] = None,
        *,
        stock_repo: Optional[StockRepository] = None,
        market_evidence_repo: Optional[PortfolioMarketEvidenceRepository] = None,
        portfolio_repo: Optional[PortfolioRepository] = None,
        fetcher_manager: Optional[DataFetcherManager] = None,
        tencent_benchmark_fetcher: Optional[Any] = None,
        yfinance_benchmark_fetcher: Optional[Any] = None,
        as_of_provider: Callable[[], date] = date.today,
        fx_fetcher: Optional[Callable[[str, str, date], Dict[str, Any]]] = None,
    ) -> None:
        self.portfolio_service = portfolio_service or PortfolioService()
        self.stock_repo = stock_repo or StockRepository()
        self.market_evidence_repo = market_evidence_repo or (
            PortfolioMarketEvidenceRepository(self.stock_repo.db)
        )
        self.portfolio_repo = portfolio_repo or PortfolioRepository()
        self.fetcher_manager = fetcher_manager or DataFetcherManager()
        self._benchmark_fetchers = {
            "tencent": tencent_benchmark_fetcher or TencentFetcher(),
            "yfinance": yfinance_benchmark_fetcher or YfinanceFetcher(),
        }
        self.as_of_provider = as_of_provider
        self.fx_fetcher = fx_fetcher or self._fetch_fx_quote

    def prepare(self) -> Dict[str, Any]:
        as_of = self.as_of_provider()
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            as_of=as_of,
            cost_method="fifo",
            include_realtime=False,
            persist_snapshot=False,
        )
        positions = []
        for account in snapshot.get("accounts") or []:
            for position in account.get("positions") or []:
                try:
                    quantity = float(position.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                if quantity <= 0:
                    continue
                positions.append((account, position))

        if positions:
            items = self._prepare_positions(positions=positions, as_of=as_of)
            ready_count = sum(item["status"] == "ready" for item in items)
            insufficient_count = len(items) - ready_count
            return {
                "schema_version": self.SCHEMA_VERSION,
                "prepared_at": datetime.now(timezone.utc).isoformat(),
                "as_of": as_of.isoformat(),
                "status": "ready" if insufficient_count == 0 else "partial",
                "position_count": len(items),
                "ready_count": ready_count,
                "insufficient_count": insufficient_count,
                "items": items,
            }
        return {
            "schema_version": self.SCHEMA_VERSION,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "as_of": as_of.isoformat(),
            "status": "empty",
            "position_count": 0,
            "ready_count": 0,
            "insufficient_count": 0,
            "items": [],
        }

    def _prepare_positions(
        self,
        *,
        positions: list[tuple[Dict[str, Any], Dict[str, Any]]],
        as_of: date,
    ) -> list[Dict[str, Any]]:
        benchmark_cache: Dict[str, Dict[str, Any]] = {}
        fx_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
        items = []
        for account, position in positions:
            market = str(position.get("market") or "").strip().lower()
            symbol = str(position.get("symbol") or "").strip()
            currency = str(position.get("currency") or "").strip().upper()
            base_currency = str(account.get("base_currency") or "").strip().upper()
            benchmark_route = self.BENCHMARK_ROUTES.get(market)
            benchmark_code = (
                str(benchmark_route["storage_code"])
                if benchmark_route is not None
                else None
            )

            price = self._prepare_bar(
                fetch_code=symbol,
                storage_code=symbol,
                as_of=as_of,
                blocker_prefix="position",
                market=market,
            )
            if benchmark_code is None:
                benchmark = {
                    "status": "insufficient",
                    "code": None,
                    "blockers": ["benchmark_not_configured"],
                }
            else:
                if market not in benchmark_cache:
                    benchmark_cache[market] = self._prepare_bar(
                        fetch_code=str(benchmark_route["fetch_code"]),
                        storage_code=benchmark_code,
                        as_of=as_of,
                        blocker_prefix="benchmark",
                        market=market,
                        direct_fetcher=self._benchmark_fetchers.get(
                            str(benchmark_route["provider"])
                        ),
                        expected_source=str(benchmark_route["source"]),
                    )
                benchmark = benchmark_cache[market]

            fx_key = (currency, base_currency)
            if fx_key not in fx_cache:
                fx_cache[fx_key] = self._prepare_fx(
                    from_currency=currency,
                    to_currency=base_currency,
                    as_of=as_of,
                )
            fx = fx_cache[fx_key]
            blockers = [
                blocker
                for evidence in (price, benchmark, fx)
                for blocker in evidence.get("blockers") or []
            ]
            items.append(
                {
                    "account_id": int(account["account_id"]),
                    "symbol": symbol,
                    "market": market,
                    "currency": currency,
                    "benchmark_code": benchmark_code,
                    "status": "insufficient" if blockers else "ready",
                    "price": self._without_internal_blockers(price),
                    "benchmark": self._without_internal_blockers(benchmark),
                    "fx": self._without_internal_blockers(fx),
                    "blockers": blockers,
                }
            )
        return items

    def _prepare_bar(
        self,
        *,
        fetch_code: str,
        storage_code: str,
        as_of: date,
        blocker_prefix: str,
        market: Optional[str] = None,
        direct_fetcher: Optional[Any] = None,
        expected_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        resolved_market = market or get_market_for_stock(storage_code)
        expected_date = self._expected_daily_bar_date(
            market=resolved_market,
            as_of=as_of,
        )
        now = datetime.now(timezone.utc)
        batch = (
            self.market_evidence_repo.get_latest_batch(
                code=storage_code,
                cutoff=now,
                target_date=expected_date,
                source_version=self.SCHEMA_VERSION,
                data_source=expected_source,
            )
            if expected_date is not None
            else None
        )
        row = self._reusable_batch_row(
            batch,
            expected_date=expected_date,
            expected_source=expected_source,
        )
        try:
            if row is None:
                lookback_days = self._daily_lookback_days(storage_code)
                fetch_days = lookback_days + self.FETCH_WARMUP_BARS
                if direct_fetcher is None:
                    frame, source = self.fetcher_manager.get_daily_data(
                        fetch_code,
                        days=fetch_days,
                    )
                else:
                    if (
                        not expected_source
                        or str(getattr(direct_fetcher, "name", "")) != expected_source
                    ):
                        raise ValueError("fixed benchmark provider identity mismatch")
                    frame = direct_fetcher.get_daily_data(fetch_code, days=fetch_days)
                    source = expected_source
                source = str(source or "").strip() or "Unknown"
                filtered = self._filter_frame_at_or_before(frame, as_of=as_of)
                if filtered.empty:
                    raise ValueError("no daily bar at or before evidence date")
                filtered = filtered.sort_values("date")
                if source in self.LEGACY_SOURCES:
                    if len(filtered) < 2:
                        raise ValueError("pct_chg warmup bar unavailable")
                    filtered = filtered.iloc[1:]
                filtered = filtered.tail(lookback_days).copy()
                adjustment = self._source_adjustment(source)
                ordered = filtered.sort_values("date")
                source_row = ordered.iloc[-1]
                target_date = source_row["date"]
                batch = self.market_evidence_repo.append_batch(
                    ordered,
                    code=storage_code,
                    data_source=source,
                    source_version=self.SCHEMA_VERSION,
                    adjustment_identity=adjustment,
                    captured_at=now,
                )
                row = next((item for item in batch.rows if item.date == target_date), None)
                if row is None or float(row.close) <= 0:
                    raise ValueError("saved daily bar could not be verified")
        except Exception:
            return {
                "status": "insufficient",
                "code": storage_code,
                "blockers": [f"{blocker_prefix}_market_data_unavailable"],
            }

        source = str(row.data_source or "").strip() or "Unknown"
        adjustment = str(row.adjustment_identity or "").strip() or "unknown"
        data_source = self._data_source_label(source, adjustment)
        blockers = []
        if expected_date is None:
            blockers.append(f"{blocker_prefix}_market_calendar_unavailable")
        elif row.date != expected_date:
            blockers.append(f"{blocker_prefix}_market_data_stale")
        if adjustment == "unknown":
            blockers.append(f"{blocker_prefix}_adjustment_identity_unknown")
        return {
            "status": "insufficient" if blockers else "ready",
            "code": storage_code,
            "date": row.date.isoformat(),
            "expected_date": expected_date.isoformat() if expected_date else None,
            "close": float(row.close),
            "data_source": data_source,
            "source": source,
            "source_version": row.source_version,
            "adjustment": adjustment,
            "captured_at": row.captured_at.replace(tzinfo=timezone.utc).isoformat(),
            "evidence_batch_hash": row.batch_hash,
            "evidence_bar_hash": row.bar_hash,
            "blockers": blockers,
        }

    @classmethod
    def _reusable_batch_row(
        cls,
        batch: Any,
        *,
        expected_date: Optional[date],
        expected_source: Optional[str],
    ) -> Optional[Any]:
        if (
            batch is None
            or expected_date is None
            or batch.source_version != cls.SCHEMA_VERSION
            or not batch.rows
            or batch.rows[-1].date != expected_date
            or float(batch.rows[-1].close) <= 0
        ):
            return None
        if expected_source and batch.data_source != expected_source:
            return None
        return batch.rows[-1]

    @staticmethod
    def _expected_daily_bar_date(
        *,
        market: Optional[str],
        as_of: date,
    ) -> Optional[date]:
        if not market:
            return None
        expected = get_effective_trading_date(
            market,
            current_time=datetime.combine(as_of, datetime.min.time()),
        )
        return expected if expected < as_of else None

    def _prepare_fx(
        self,
        *,
        from_currency: str,
        to_currency: str,
        as_of: date,
    ) -> Dict[str, Any]:
        if not from_currency or not to_currency:
            return {
                "status": "insufficient",
                "from_currency": from_currency,
                "to_currency": to_currency,
                "blockers": ["fx_identity_missing"],
            }
        if from_currency == to_currency:
            return {
                "status": "ready",
                "from_currency": from_currency,
                "to_currency": to_currency,
                "rate": 1.0,
                "rate_date": as_of.isoformat(),
                "source": "identity",
                "source_version": "1",
                "blockers": [],
            }
        try:
            quote = self.fx_fetcher(from_currency, to_currency, as_of)
            rate = float(quote["rate"])
            rate_date = self._coerce_date(quote["rate_date"])
            source = str(quote.get("source") or "").strip()
            source_version = str(quote.get("source_version") or "").strip()
            if (
                str(quote.get("from_currency") or "").strip().upper() != from_currency
                or str(quote.get("to_currency") or "").strip().upper() != to_currency
                or rate <= 0
                or rate_date > as_of
                or (as_of - rate_date).days > self.FX_MAX_AGE_DAYS
                or not source
                or not source_version
            ):
                raise ValueError("incomplete FX source contract")
            self.portfolio_repo.save_fx_rate(
                from_currency=from_currency,
                to_currency=to_currency,
                rate_date=rate_date,
                rate=rate,
                source=self._fx_source_label(source, source_version),
                is_stale=False,
            )
        except Exception:
            return {
                "status": "insufficient",
                "from_currency": from_currency,
                "to_currency": to_currency,
                "blockers": ["fx_evidence_unavailable"],
            }
        return {
            "status": "ready",
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": rate,
            "rate_date": rate_date.isoformat(),
            "source": source,
            "source_version": source_version,
            "blockers": [],
        }

    @staticmethod
    def _filter_frame_at_or_before(frame: Any, *, as_of: date) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty or "date" not in frame.columns:
            return pd.DataFrame()
        filtered = frame.copy()
        normalized_dates = pd.to_datetime(filtered["date"], errors="coerce")
        filtered = filtered.loc[normalized_dates.notna()].copy()
        normalized_dates = normalized_dates.loc[normalized_dates.notna()]
        filtered["date"] = normalized_dates.dt.date
        return filtered.loc[filtered["date"] < as_of].copy()

    @classmethod
    def _bar_matches_source(
        cls,
        row: Any,
        *,
        source_row: Any,
        data_source: str,
        fetched_source: str,
    ) -> bool:
        persisted_source = str(getattr(row, "data_source", "") or "")
        if persisted_source != data_source and not (
            persisted_source in cls.LEGACY_SOURCES
            and persisted_source == fetched_source
        ):
            return False
        for field in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            persisted = getattr(row, field, None)
            fetched = source_row.get(field)
            if not cls._finite_numbers_equal(persisted, fetched):
                return False
        return True

    @staticmethod
    def _finite_numbers_equal(left: Any, right: Any) -> bool:
        try:
            left_number = float(left)
            right_number = float(right)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(left_number)
            and math.isfinite(right_number)
            and left_number == right_number
        )

    def _daily_lookback_days(self, storage_code: str) -> int:
        del storage_code
        return self.INITIAL_DAILY_LOOKBACK_DAYS

    @staticmethod
    def _source_adjustment(source: str) -> str:
        normalized = source.strip().lower()
        for marker in ("qfq", "hfq", "unadjusted", "adjusted", "raw", "none"):
            if f"adjustment={marker}" in normalized:
                return marker
        if any(name in normalized for name in ("efinance", "akshare", "tencent")):
            return "qfq"
        if "yfinance" in normalized:
            return "adjusted"
        return "unknown"

    @staticmethod
    def _explicit_adjustment(source: str) -> str:
        normalized = source.strip().lower()
        for marker in ("qfq", "hfq", "unadjusted", "adjusted", "raw", "none"):
            if f"adjustment={marker}" in normalized:
                return marker
        return "unknown"

    @staticmethod
    def _data_source_label(source: str, adjustment: str) -> str:
        suffix = f"|adjustment={adjustment}"
        source_limit = max(1, 50 - len(suffix))
        return f"{source[:source_limit]}{suffix}"

    @staticmethod
    def _fx_source_label(source: str, source_version: str) -> str:
        return f"{source}@{source_version}"[:32]

    @staticmethod
    def _without_internal_blockers(evidence: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in evidence.items() if key != "blockers"}

    @staticmethod
    def _coerce_date(value: Any) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @staticmethod
    def _fetch_fx_quote(
        from_currency: str,
        to_currency: str,
        as_of: date,
    ) -> Dict[str, Any]:
        if yf is None:
            raise RuntimeError("yfinance is unavailable")
        history = yf.Ticker(f"{from_currency}{to_currency}=X").history(
            start=(as_of - timedelta(days=7)).isoformat(),
            end=(as_of + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
        if history is None or history.empty or "Close" not in history:
            raise ValueError("FX history unavailable")
        close = history["Close"].dropna()
        if close.empty:
            raise ValueError("FX close unavailable")
        rate_date = pd.Timestamp(close.index[-1]).date()
        rate = float(close.iloc[-1])
        if rate <= 0 or rate_date > as_of:
            raise ValueError("FX quote is invalid")
        try:
            source_version = metadata.version("yfinance")
        except metadata.PackageNotFoundError:
            source_version = str(getattr(yf, "__version__", "")).strip()
        if not source_version:
            raise ValueError("FX source version unavailable")
        return {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": rate,
            "rate_date": rate_date,
            "source": "yfinance",
            "source_version": source_version,
        }
