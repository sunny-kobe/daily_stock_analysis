# -*- coding: utf-8 -*-
"""Prepare bounded market evidence for current portfolio research."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from importlib import metadata
import math
from typing import Any, Callable, Dict, Optional

import pandas as pd

from data_provider.base import DataFetcherManager
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.portfolio_service import PortfolioService

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency path
    yf = None


class _ExistingBarConflict(ValueError):
    pass


class PortfolioResearchEvidenceService:
    """Prepare research inputs without running analysis or mutating the ledger."""

    SCHEMA_VERSION = "portfolio-research-evidence-prepare-v1"
    BENCHMARK_BY_MARKET = {"cn": "000300", "hk": "HSI", "us": "SPY"}
    BENCHMARK_FETCH_CODE = {"HSI": "^HSI"}
    DAILY_LOOKBACK_DAYS = 10
    FX_MAX_AGE_DAYS = 7

    def __init__(
        self,
        portfolio_service: Optional[PortfolioService] = None,
        *,
        stock_repo: Optional[StockRepository] = None,
        portfolio_repo: Optional[PortfolioRepository] = None,
        fetcher_manager: Optional[DataFetcherManager] = None,
        as_of_provider: Callable[[], date] = date.today,
        fx_fetcher: Optional[Callable[[str, str, date], Dict[str, Any]]] = None,
    ) -> None:
        self.portfolio_service = portfolio_service or PortfolioService()
        self.stock_repo = stock_repo or StockRepository()
        self.portfolio_repo = portfolio_repo or PortfolioRepository()
        self.fetcher_manager = fetcher_manager or DataFetcherManager()
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
            benchmark_code = self.BENCHMARK_BY_MARKET.get(market)

            price = self._prepare_bar(
                fetch_code=symbol,
                storage_code=symbol,
                as_of=as_of,
                blocker_prefix="position",
            )
            if benchmark_code is None:
                benchmark = {
                    "status": "insufficient",
                    "code": None,
                    "blockers": ["benchmark_not_configured"],
                }
            else:
                if benchmark_code not in benchmark_cache:
                    benchmark_cache[benchmark_code] = self._prepare_bar(
                        fetch_code=self.BENCHMARK_FETCH_CODE.get(
                            benchmark_code,
                            benchmark_code,
                        ),
                        storage_code=benchmark_code,
                        as_of=as_of,
                        blocker_prefix="benchmark",
                    )
                benchmark = benchmark_cache[benchmark_code]

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
    ) -> Dict[str, Any]:
        try:
            frame, source = self.fetcher_manager.get_daily_data(
                fetch_code,
                days=self.DAILY_LOOKBACK_DAYS,
            )
            source = str(source or "").strip() or "Unknown"
            filtered = self._filter_frame_at_or_before(frame, as_of=as_of)
            if filtered.empty:
                raise ValueError("no daily bar at or before evidence date")
            adjustment = self._source_adjustment(source)
            data_source = self._data_source_label(source, adjustment)
            ordered = filtered.sort_values("date")
            source_row = ordered.iloc[-1]
            target_date = source_row["date"]
            existing_dates = set()
            for _, fetched_row in ordered.iterrows():
                bar_date = fetched_row["date"]
                existing = self.stock_repo.get_daily_on_date(
                    code=storage_code,
                    target_date=bar_date,
                )
                if existing is None:
                    continue
                if not self._bar_matches_source(
                    existing,
                    source_row=fetched_row,
                    data_source=data_source,
                ):
                    raise _ExistingBarConflict(
                        "existing daily bar differs from fetched evidence"
                    )
                existing_dates.add(bar_date)
            new_rows = filtered.loc[~filtered["date"].isin(existing_dates)].copy()
            if not new_rows.empty:
                self.stock_repo.save_dataframe(
                    new_rows,
                    code=storage_code,
                    data_source=data_source,
                )
            row = self.stock_repo.get_daily_on_date(
                code=storage_code,
                target_date=target_date,
            )
            if (
                row is None
                or row.close is None
                or float(row.close) <= 0
                or not self._bar_matches_source(
                    row,
                    source_row=source_row,
                    data_source=data_source,
                )
            ):
                raise ValueError("saved daily bar could not be verified")
        except _ExistingBarConflict:
            return {
                "status": "insufficient",
                "code": storage_code,
                "blockers": [f"{blocker_prefix}_existing_bar_conflict"],
            }
        except Exception:
            return {
                "status": "insufficient",
                "code": storage_code,
                "blockers": [f"{blocker_prefix}_market_data_unavailable"],
            }

        data_source = str(row.data_source or "").strip()
        source = data_source.split("|adjustment=", 1)[0] or "Unknown"
        adjustment = self._source_adjustment(data_source)
        blockers = []
        if adjustment == "unknown":
            blockers.append(f"{blocker_prefix}_adjustment_identity_unknown")
        return {
            "status": "insufficient" if blockers else "ready",
            "code": storage_code,
            "date": row.date.isoformat(),
            "close": float(row.close),
            "data_source": data_source,
            "source": source,
            "adjustment": adjustment,
            "blockers": blockers,
        }

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
    ) -> bool:
        if str(getattr(row, "data_source", "") or "") != data_source:
            return False
        for field in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            persisted = getattr(row, field, None)
            fetched = source_row.get(field)
            if cls._optional_number(persisted) != cls._optional_number(fetched):
                return False
        return True

    @staticmethod
    def _optional_number(value: Any) -> float | None:
        if value is None or pd.isna(value):
            return None
        number = float(value)
        return round(number, 12) if math.isfinite(number) else None

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
