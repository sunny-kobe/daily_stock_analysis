# -*- coding: utf-8 -*-
"""Read-only near-close execution evidence refresh for a frozen research scope."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Optional

from data_provider.base import DataFetcherManager, canonical_stock_code, normalize_stock_code
from src.core.trading_calendar import MarketPhase, infer_market_phase
from src.services.portfolio_research_product_evidence import (
    frozen_product_evidence_is_ready,
    product_evidence_for_account,
)


class PortfolioResearchExecutionService:
    """Refresh only action-sensitive evidence without persisting research state."""

    SCHEMA_VERSION = "portfolio-research-execution-check-v1"
    MAX_QUOTE_AGE = timedelta(minutes=15)
    BENCHMARK_PROVIDER_SYMBOLS = {
        ("cn", "000300"): "sh000300",
        ("hk", "HSI"): "r_hkHSI",
        ("us", "SPY"): "SPY",
    }

    def __init__(
        self,
        *,
        quote_loader: Optional[Callable[[str], Any]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if quote_loader is None:
            manager = DataFetcherManager()
            self.quote_loader = lambda symbol: manager.get_realtime_quote(
                symbol,
                log_final_failure=False,
                supplement=False,
                preserve_provider_symbol=True,
            )
        else:
            self.quote_loader = quote_loader
        self.now = now or (lambda: datetime.now(timezone.utc))

    def check(
        self,
        research_snapshot: Mapping[str, Any],
        *,
        research_snapshot_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        snapshot = dict(research_snapshot)
        checked_at = self._checked_at()
        research_cutoff = self._aware_datetime(snapshot.get("cutoff"))
        scope = list(snapshot.get("scope") or [])
        if not scope:
            raise ValueError("research execution check requires an explicit scope")

        positions = {
            self._identity(item): dict(item)
            for item in snapshot.get("positions") or []
            if isinstance(item, Mapping)
        }
        instruments = {
            self._instrument_key(item): dict(item)
            for item in snapshot.get("instruments") or []
            if isinstance(item, Mapping)
        }
        benchmarks = {
            str(item.get("market") or "").strip().lower(): dict(item)
            for item in snapshot.get("benchmarks") or []
            if isinstance(item, Mapping)
        }
        benchmark_quotes: dict[str, Any] = {}
        items = []
        for scope_item in scope:
            identity = self._identity(scope_item)
            account_id, market, symbol = identity
            position = positions.get(identity)
            instrument = instruments.get((market, symbol))
            if instrument is not None:
                instrument = dict(instrument)
                instrument["product_evidence"] = product_evidence_for_account(
                    instrument,
                    account_id=account_id,
                )
            blockers: list[str] = []
            if position is None:
                blockers.append("execution_position_missing")
            if instrument is None or instrument.get("verification_status") != "verified":
                blockers.append("execution_instrument_identity_unverified")

            quote = self._load_quote(symbol)
            checked_at = max(checked_at, self._checked_at())
            current = self._quote_evidence(
                quote,
                expected_symbol=symbol,
                market=market,
                checked_at=checked_at,
            )
            blockers.extend(current.pop("blockers"))

            benchmark = benchmarks.get(market)
            benchmark_code = str((benchmark or {}).get("code") or "").strip()
            if benchmark_code:
                provider_benchmark_symbol = self._benchmark_provider_symbol(
                    market,
                    benchmark_code,
                )
                if provider_benchmark_symbol not in benchmark_quotes:
                    benchmark_quotes[provider_benchmark_symbol] = self._load_quote(
                        provider_benchmark_symbol
                    )
                checked_at = max(checked_at, self._checked_at())
                current_benchmark = self._quote_evidence(
                    benchmark_quotes[provider_benchmark_symbol],
                    expected_symbol=provider_benchmark_symbol,
                    market=market,
                    checked_at=checked_at,
                )
                blockers.extend(
                    f"benchmark_{value}"
                    for value in current_benchmark.pop("blockers")
                )
            else:
                current_benchmark = {}
                blockers.append("execution_benchmark_missing")

            blockers.extend(
                self._product_blockers(
                    instrument,
                    current=current,
                    cutoff=research_cutoff,
                )
            )
            blockers = list(dict.fromkeys(blockers))
            changed_fields = []
            if self._changed((position or {}).get("last_price"), current.get("price")):
                changed_fields.append("price")
            if self._changed((benchmark or {}).get("price"), current_benchmark.get("price")):
                changed_fields.append("benchmark_price")
            requires_reconfirmation = bool(blockers or changed_fields)
            items.append(
                {
                    "account_id": account_id,
                    "market": market,
                    "symbol": symbol,
                    "name": (instrument or {}).get("name"),
                    "status": "insufficient" if blockers else "ready",
                    "reference_evidence": {
                        "price": (position or {}).get("last_price"),
                        "benchmark_code": benchmark_code or None,
                        "benchmark_price": (benchmark or {}).get("price"),
                    },
                    "current_evidence": {
                        **current,
                        "benchmark_code": benchmark_code or None,
                        "benchmark_price": current_benchmark.get("price"),
                        "benchmark_source": current_benchmark.get("source"),
                        "benchmark_as_of": current_benchmark.get("as_of"),
                    },
                    "changed_fields": changed_fields,
                    "blockers": blockers,
                    "requires_reconfirmation": requires_reconfirmation,
                }
            )

        return {
            "schema_version": self.SCHEMA_VERSION,
            "checked_at": checked_at.isoformat(),
            "research_snapshot_hash": str(
                research_snapshot_hash or snapshot.get("snapshot_hash") or ""
            ),
            "scope": scope,
            "status": "ready" if all(item["status"] == "ready" for item in items) else "partial",
            "requires_reconfirmation": any(
                item["requires_reconfirmation"] for item in items
            ),
            "items": items,
        }

    def _load_quote(self, symbol: str) -> Any:
        try:
            return self.quote_loader(symbol)
        except Exception:
            return None

    def _checked_at(self) -> datetime:
        checked_at = self.now()
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("research execution check requires a timezone-aware clock")
        return checked_at.astimezone(timezone.utc)

    @classmethod
    def _benchmark_provider_symbol(cls, market: str, benchmark_code: str) -> str:
        normalized_market = str(market or "").strip().lower()
        normalized_code = str(benchmark_code or "").strip().upper()
        return cls.BENCHMARK_PROVIDER_SYMBOLS.get(
            (normalized_market, normalized_code),
            benchmark_code,
        )

    @classmethod
    def _quote_evidence(
        cls,
        quote: Any,
        *,
        expected_symbol: str,
        market: str,
        checked_at: datetime,
    ) -> dict[str, Any]:
        if quote is None:
            return {
                "price": None,
                "trading_status": None,
                "spread_bps": None,
                "volume": None,
                "volume_ratio": None,
                "vwap": None,
                "source": None,
                "as_of": None,
                "blockers": ["execution_quote_unavailable"],
            }
        actual_symbol = cls._text(cls._field(quote, "symbol", "code", "stock_code"))
        blockers = []
        if actual_symbol and cls._canonical(actual_symbol) != cls._canonical(expected_symbol):
            blockers.append("execution_quote_identity_mismatch")
        price = cls._number(
            cls._field(quote, "current_price", "price", "latest_price", "last_price")
        )
        if price is None or price <= 0:
            blockers.append("execution_quote_price_invalid")
        if cls._field(quote, "is_stale") is True:
            blockers.append("execution_quote_stale")
        source = cls._text(cls._field(quote, "source", "provider"))
        if not source:
            blockers.append("execution_quote_source_missing")
        raw_as_of = cls._field(
            quote,
            "provider_timestamp",
            "quote_timestamp",
            "timestamp",
        )
        as_of = cls._aware_datetime(raw_as_of)
        if as_of is None:
            blockers.append("execution_quote_timestamp_missing")
        elif as_of > checked_at:
            blockers.append("execution_quote_after_check")
        elif checked_at - as_of > cls.MAX_QUOTE_AGE:
            blockers.append("execution_quote_stale")
        provider_trading_status = cls._text(
            cls._field(quote, "trading_status", "market_status", "status")
        )
        market_phase = infer_market_phase(market, current_time=checked_at)
        if market_phase in {MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION}:
            trading_status = provider_trading_status or "open"
        elif market_phase == MarketPhase.LUNCH_BREAK:
            trading_status = "paused"
        elif market_phase in {
            MarketPhase.PREMARKET,
            MarketPhase.POSTMARKET,
            MarketPhase.NON_TRADING,
        }:
            trading_status = "closed"
        else:
            trading_status = provider_trading_status
        if not trading_status:
            blockers.append("execution_trading_status_missing")
        elif (
            trading_status.lower() in {"halted", "suspended", "closed", "paused"}
            or provider_trading_status.lower() in {"halted", "suspended", "closed"}
        ):
            blockers.append("execution_trading_unavailable")
        bid = cls._number(cls._field(quote, "bid", "bid_price"))
        ask = cls._number(cls._field(quote, "ask", "ask_price"))
        spread_bps = None
        if bid is not None and ask is not None and ask >= bid and bid + ask > 0:
            spread_bps = round((ask - bid) / ((ask + bid) / 2.0) * 10000.0, 4)
        return {
            "price": price,
            "trading_status": trading_status or None,
            "spread_bps": spread_bps,
            "volume": cls._number(cls._field(quote, "volume")),
            "volume_ratio": cls._number(cls._field(quote, "volume_ratio")),
            "vwap": cls._number(cls._field(quote, "vwap")),
            "source": source or None,
            "as_of": as_of.isoformat() if as_of is not None else None,
            "blockers": list(dict.fromkeys(blockers)),
        }

    @staticmethod
    def _product_blockers(
        instrument: Optional[Mapping[str, Any]],
        *,
        current: Mapping[str, Any],
        cutoff: Optional[datetime],
    ) -> list[str]:
        instrument_type = str((instrument or {}).get("instrument_type") or "")
        daily_reset = bool(
            instrument_type == "daily_leveraged_product"
            or (instrument or {}).get("daily_reset") is True
        )
        if instrument_type != "qdii" and not daily_reset:
            return []
        blockers: list[str] = []
        if cutoff is None or not frozen_product_evidence_is_ready(
            instrument or {},
            cutoff=cutoff,
        ):
            blockers.append(
                "qdii_execution_evidence_incomplete"
                if instrument_type == "qdii"
                else "daily_reset_execution_evidence_incomplete"
            )
        if current.get("spread_bps") is None:
            blockers.append("execution_spread_missing")
        volume = PortfolioResearchExecutionService._number(current.get("volume"))
        if volume is None or volume <= 0:
            blockers.append("execution_volume_missing")
        vwap = PortfolioResearchExecutionService._number(current.get("vwap"))
        if vwap is None or vwap <= 0:
            blockers.append("execution_vwap_missing")
        return blockers

    @staticmethod
    def _aware_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _field(value: Any, *names: str) -> Any:
        for name in names:
            if isinstance(value, Mapping) and value.get(name) is not None:
                return value.get(name)
            attribute = getattr(value, name, None)
            if attribute is not None:
                return attribute
        return None

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _changed(reference: Any, current: Any) -> bool:
        left = PortfolioResearchExecutionService._number(reference)
        right = PortfolioResearchExecutionService._number(current)
        if left is None or right is None:
            return left != right
        return not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)

    @staticmethod
    def _canonical(symbol: str) -> str:
        return canonical_stock_code(normalize_stock_code(symbol)).upper()

    @classmethod
    def _identity(cls, item: Mapping[str, Any]) -> tuple[int, str, str]:
        return (
            int(item.get("account_id")),
            str(item.get("market") or "").strip().lower(),
            cls._canonical(str(item.get("symbol") or "")),
        )

    @classmethod
    def _instrument_key(cls, item: Mapping[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("market") or "").strip().lower(),
            cls._canonical(str(item.get("symbol") or "")),
        )
