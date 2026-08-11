# -*- coding: utf-8 -*-
"""Read-only near-close execution evidence refresh for a frozen research scope."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Optional

from data_provider.base import DataFetcherManager, canonical_stock_code, normalize_stock_code
from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.core.trading_calendar import MarketPhase, infer_market_phase
from src.services.portfolio_research_product_evidence import (
    frozen_product_evidence_is_ready,
    product_evidence_for_account,
    qdii_execution_reference_observation,
)


class PortfolioResearchExecutionService:
    """Refresh only action-sensitive evidence without persisting research state."""

    SCHEMA_VERSION = "portfolio-research-execution-check-v1"
    MAX_QUOTE_AGE = timedelta(minutes=15)
    MAX_PRODUCT_TIMESTAMP_ALIGNMENT = timedelta(minutes=2)
    BENCHMARK_PROVIDER_SYMBOLS = {
        ("cn", "000300"): "sh000300",
        ("hk", "HSI"): "r_hkHSI",
        ("us", "SPY"): "SPY",
    }

    def __init__(
        self,
        *,
        quote_loader: Optional[Callable[[str], Any]] = None,
        qdii_reference_loader: Optional[Callable[..., Any]] = None,
        fx_quote_loader: Optional[Callable[..., Any]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._uses_default_quote_loader = quote_loader is None
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
        self.qdii_reference_loader = qdii_reference_loader or self._fetch_qdii_reference
        self.fx_quote_loader = fx_quote_loader or self._fetch_fx_quote
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

            quote = self._load_position_quote(
                symbol,
                market=market,
                instrument=instrument,
            )
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

            product_execution_evidence = self._collect_product_execution_evidence(
                instrument,
                product_quote=quote,
                current=current,
                checked_at=checked_at,
            )
            if product_execution_evidence is not None:
                checked_at = max(checked_at, self._checked_at())
                blockers.extend(product_execution_evidence.pop("blockers"))

            blockers.extend(
                self._product_blockers(
                    instrument,
                    current=current,
                    cutoff=research_cutoff,
                    product_execution_evidence=product_execution_evidence,
                )
            )
            blockers = list(dict.fromkeys(blockers))
            changed_fields = []
            if self._changed((position or {}).get("last_price"), current.get("price")):
                changed_fields.append("price")
            if self._changed((benchmark or {}).get("price"), current_benchmark.get("price")):
                changed_fields.append("benchmark_price")
            if (
                product_execution_evidence is not None
                and product_execution_evidence.get("status") == "ready"
            ):
                changed_fields.append("product_execution_evidence")
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
                    "product_execution_evidence": product_execution_evidence,
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

    def _load_position_quote(
        self,
        symbol: str,
        *,
        market: str,
        instrument: Optional[Mapping[str, Any]],
    ) -> Any:
        instrument_type = str((instrument or {}).get("instrument_type") or "")
        daily_reset = bool(
            instrument_type == "daily_leveraged_product"
            or (instrument or {}).get("daily_reset") is True
        )
        if self._uses_default_quote_loader and market == "us" and daily_reset:
            try:
                quote = YfinanceFetcher().get_realtime_us_execution_quote(symbol)
            except Exception:
                quote = None
            return quote
        return self._load_quote(symbol)

    def _load_underlying_quote(self, instrument: Mapping[str, Any]) -> Any:
        symbol = str(instrument.get("underlying_symbol") or "").strip()
        market = str(instrument.get("underlying_market") or "").strip().lower()
        if not symbol:
            return None
        if self._uses_default_quote_loader and market == "kr":
            try:
                quote = YfinanceFetcher().get_realtime_kr_execution_quote(symbol)
            except Exception:
                quote = None
            if quote is not None:
                return quote
        if self._uses_default_quote_loader and market == "us":
            try:
                return YfinanceFetcher().get_realtime_us_execution_quote(symbol)
            except Exception:
                return None
        return self._load_quote(symbol)

    @staticmethod
    def _fetch_qdii_reference(*, symbol: str, **_: Any) -> Any:
        return AkshareFetcher().get_sse_etf_iopv(symbol)

    @staticmethod
    def _fetch_fx_quote(
        *,
        from_currency: str,
        to_currency: str,
        **_: Any,
    ) -> Any:
        try:
            quote = AkshareFetcher().get_realtime_fx_quote(
                from_currency,
                to_currency,
            )
        except Exception:
            quote = None
        if quote is not None:
            return quote
        return YfinanceFetcher().get_realtime_fx_quote(
            from_currency,
            to_currency,
        )

    def _collect_product_execution_evidence(
        self,
        instrument: Optional[Mapping[str, Any]],
        *,
        product_quote: Any,
        current: Mapping[str, Any],
        checked_at: datetime,
    ) -> Optional[dict[str, Any]]:
        instrument_type = str((instrument or {}).get("instrument_type") or "")
        daily_reset = bool(
            instrument_type == "daily_leveraged_product"
            or (instrument or {}).get("daily_reset") is True
        )
        if instrument_type == "qdii":
            return self._collect_qdii_execution_evidence(
                instrument or {},
                product_quote=product_quote,
                current=current,
                checked_at=checked_at,
            )
        if daily_reset:
            return self._collect_daily_reset_execution_evidence(
                instrument or {},
                product_quote=product_quote,
                current=current,
                checked_at=checked_at,
            )
        return None

    def _collect_qdii_execution_evidence(
        self,
        instrument: Mapping[str, Any],
        *,
        product_quote: Any,
        current: Mapping[str, Any],
        checked_at: datetime,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        symbol = str(instrument.get("symbol") or "").strip().upper()
        reference = self._load_dynamic_input(
            self.qdii_reference_loader,
            symbol=symbol,
            market=str(instrument.get("market") or "").strip().lower(),
            checked_at=checked_at,
        )
        checked_at = max(checked_at, self._checked_at())
        reference_component, reference_blockers = self._reference_component(
            reference,
            checked_at=checked_at,
        )
        blockers.extend(reference_blockers)

        from_currency = str(instrument.get("underlying_currency") or "").strip().upper()
        to_currency = str(instrument.get("quote_currency") or "").strip().upper()
        fx = self._load_dynamic_input(
            self.fx_quote_loader,
            from_currency=from_currency,
            to_currency=to_currency,
            checked_at=checked_at,
        )
        checked_at = max(checked_at, self._checked_at())
        fx_component, fx_blockers = self._fx_component(
            fx,
            expected_pair=f"{from_currency}/{to_currency}",
            checked_at=checked_at,
            require_previous_rate=False,
        )
        blockers.extend(fx_blockers)

        frozen_product_evidence = instrument.get("product_evidence")
        frozen_cutoff = (
            self._aware_datetime(frozen_product_evidence.get("evidence_cutoff"))
            if isinstance(frozen_product_evidence, Mapping)
            else None
        )
        frozen_observation = qdii_execution_reference_observation(
            instrument,
            cutoff=frozen_cutoff or checked_at,
        )
        if frozen_observation is None:
            blockers.append("qdii_execution_reference_observation_missing")

        product_as_of = self._aware_datetime(current.get("as_of"))
        for component, blocker in (
            (reference_component, "qdii_execution_reference_timestamp_misaligned"),
            (fx_component, "qdii_execution_fx_timestamp_misaligned"),
        ):
            if component and not self._timestamps_aligned(
                product_as_of,
                self._aware_datetime(component.get("provider_timestamp")),
            ):
                blockers.append(blocker)

        market_price = self._number(current.get("price"))
        reference_value = self._number((reference_component or {}).get("value"))
        premium_discount = None
        if market_price is not None and market_price > 0 and reference_value is not None:
            premium_discount = {
                "premium_discount_pct": round(
                    (market_price / reference_value - 1.0) * 100.0,
                    6,
                ),
                "market_price": market_price,
                "reference_value": reference_value,
                "sources": [current.get("source"), reference_component.get("source")],
                "provider_timestamps": [
                    current.get("as_of"),
                    reference_component.get("provider_timestamp"),
                ],
            }
        else:
            blockers.append("qdii_execution_premium_discount_missing")

        product_return = self._interval_return(
            baseline=(frozen_observation or {}).get("market_price"),
            current={
                "value": market_price,
                "source": current.get("source"),
                "provider_timestamp": current.get("as_of"),
            },
        )
        reference_return = self._interval_return(
            baseline=(frozen_observation or {}).get("reference_value"),
            current=reference_component,
        )
        fx_return = self._interval_return(
            baseline=(frozen_observation or {}).get("fx"),
            current={
                **(fx_component or {}),
                "value": (fx_component or {}).get("rate"),
            },
            baseline_value_field="rate",
        )
        tracking = None
        if None not in (product_return, reference_return, fx_return):
            tracking = {
                "tracking_difference_pct": round(
                    product_return - reference_return,
                    6,
                ),
                "product_return_pct": product_return,
                "reference_return_pct": reference_return,
                "fx_return_pct": fx_return,
                "formula": "product_return-reference_return",
                "inputs": {
                    "baseline": frozen_observation,
                    "current": {
                        "market_price": {
                            "value": market_price,
                            "source": current.get("source"),
                            "provider_timestamp": current.get("as_of"),
                        },
                        "reference_value": reference_component,
                        "fx": fx_component,
                    },
                },
            }
        else:
            blockers.append("qdii_execution_tracking_inputs_missing")

        spread = self._number(current.get("spread_bps"))
        vwap = self._number(current.get("vwap"))
        if spread is None:
            blockers.append("execution_spread_missing")
        if vwap is None or vwap <= 0:
            blockers.append("execution_vwap_missing")
        body = {
            "instrument_type": "qdii",
            "status": "ready" if not blockers else "insufficient",
            "market_price": {
                "value": market_price,
                "source": current.get("source"),
                "provider_timestamp": current.get("as_of"),
            },
            "reference_value": reference_component,
            "premium_discount": premium_discount,
            "fx": fx_component,
            "spread": {
                "spread_bps": spread,
                "source": current.get("spread_source"),
                "provider_timestamp": current.get("spread_as_of"),
            },
            "vwap": {
                "value": vwap,
                "source": current.get("vwap_source"),
                "provider_timestamp": current.get("vwap_as_of"),
                "method": current.get("vwap_method"),
            },
            "tracking": tracking,
            "blockers": list(dict.fromkeys(blockers)),
        }
        body["status"] = "ready" if not body["blockers"] else "insufficient"
        return body

    def _collect_daily_reset_execution_evidence(
        self,
        instrument: Mapping[str, Any],
        *,
        product_quote: Any,
        current: Mapping[str, Any],
        checked_at: datetime,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        symbol = str(instrument.get("symbol") or "").strip().upper()
        underlying_symbol = str(instrument.get("underlying_symbol") or "").strip()
        product, product_blockers = self._return_input_component(
            product_quote,
            expected_symbol=symbol,
            checked_at=checked_at,
            blocker_prefix="daily_reset_execution",
        )
        blockers.extend(product_blockers)
        underlying_quote = self._load_underlying_quote(instrument)
        checked_at = max(checked_at, self._checked_at())
        underlying, underlying_blockers = self._return_input_component(
            underlying_quote,
            expected_symbol=underlying_symbol,
            checked_at=checked_at,
            blocker_prefix="daily_reset_underlying_execution",
        )
        blockers.extend(underlying_blockers)

        product_as_of = self._aware_datetime((product or {}).get("provider_timestamp"))
        underlying_as_of = self._aware_datetime((underlying or {}).get("provider_timestamp"))
        alignment_seconds = None
        if product_as_of is None:
            blockers.append("daily_reset_execution_timestamp_missing")
        if underlying_as_of is None:
            blockers.append("daily_reset_underlying_execution_timestamp_missing")
        if product_as_of is not None and underlying_as_of is not None:
            alignment_seconds = abs((product_as_of - underlying_as_of).total_seconds())
            if alignment_seconds > self.MAX_PRODUCT_TIMESTAMP_ALIGNMENT.total_seconds():
                blockers.append("daily_reset_execution_timestamp_misaligned")

        product_return = self._number((product or {}).get("return_pct"))
        underlying_return = self._number((underlying or {}).get("return_pct"))
        observed_leverage = None
        if product_return is None or underlying_return is None:
            blockers.append("daily_reset_execution_synchronized_returns_missing")
        elif math.isclose(underlying_return, 0.0, rel_tol=0.0, abs_tol=1e-12):
            blockers.append("daily_reset_execution_observed_leverage_unavailable")
        else:
            observed_leverage = round(product_return / underlying_return, 6)

        spread = self._number(current.get("spread_bps"))
        volume = self._number(current.get("volume"))
        vwap = self._number(current.get("vwap"))
        if spread is None:
            blockers.append("execution_spread_missing")
        if volume is None or volume <= 0:
            blockers.append("execution_volume_missing")
        if vwap is None or vwap <= 0:
            blockers.append("execution_vwap_missing")
        blockers = list(dict.fromkeys(blockers))
        return {
            "instrument_type": "daily_leveraged_product",
            "status": "ready" if not blockers else "insufficient",
            "product": product,
            "underlying": underlying,
            "timestamp_alignment_seconds": alignment_seconds,
            "product_return_pct": product_return,
            "underlying_return_pct": underlying_return,
            "observed_leverage": observed_leverage,
            "spread_bps": spread,
            "volume": volume,
            "vwap": vwap,
            "liquidity_evidence": {
                "spread": {
                    "value": spread,
                    "source": current.get("spread_source"),
                    "provider_timestamp": current.get("spread_as_of"),
                },
                "volume": {
                    "value": volume,
                    "source": current.get("volume_source"),
                    "provider_timestamp": current.get("volume_as_of"),
                },
                "vwap": {
                    "value": vwap,
                    "source": current.get("vwap_source"),
                    "provider_timestamp": current.get("vwap_as_of"),
                    "method": current.get("vwap_method"),
                },
            },
            "blockers": blockers,
        }

    @staticmethod
    def _load_dynamic_input(loader: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            return loader(**kwargs)
        except Exception:
            return None

    @classmethod
    def _reference_component(
        cls,
        value: Any,
        *,
        checked_at: datetime,
    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        reference_value = cls._number(cls._field(value, "reference_value", "iopv", "value"))
        reference_type = cls._text(cls._field(value, "reference_type", "kind"))
        component, blockers = cls._timestamped_component(
            value,
            checked_at=checked_at,
            blocker_prefix="qdii_execution_reference",
        )
        if reference_value is None or reference_value <= 0 or not reference_type:
            blockers.append("qdii_execution_reference_value_missing")
            return None, list(dict.fromkeys(blockers))
        if component is None:
            return None, list(dict.fromkeys(blockers))
        component.update(
            {
                "reference_type": reference_type,
                "value": reference_value,
            }
        )
        return component, list(dict.fromkeys(blockers))

    @classmethod
    def _fx_component(
        cls,
        value: Any,
        *,
        expected_pair: str,
        checked_at: datetime,
        require_previous_rate: bool = True,
    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        rate = cls._number(cls._field(value, "rate", "price"))
        previous_rate = cls._number(cls._field(value, "previous_rate", "pre_close"))
        pair = cls._text(cls._field(value, "pair", "code"))
        component, blockers = cls._timestamped_component(
            value,
            checked_at=checked_at,
            blocker_prefix="qdii_execution_fx",
        )
        if pair != expected_pair or rate is None or rate <= 0:
            blockers.append("qdii_execution_fx_missing")
            return None, list(dict.fromkeys(blockers))
        if require_previous_rate and (previous_rate is None or previous_rate <= 0):
            blockers.append("qdii_execution_fx_return_missing")
            return None, list(dict.fromkeys(blockers))
        if component is None:
            return None, list(dict.fromkeys(blockers))
        component.update(
            {
                "pair": pair,
                "rate": rate,
                "previous_rate": previous_rate,
                "return_pct": (
                    round((rate / previous_rate - 1.0) * 100.0, 6)
                    if previous_rate is not None and previous_rate > 0
                    else None
                ),
            }
        )
        return component, list(dict.fromkeys(blockers))

    @classmethod
    def _interval_return(
        cls,
        *,
        baseline: Any,
        current: Any,
        baseline_value_field: str = "value",
    ) -> Optional[float]:
        if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
            return None
        baseline_value = cls._number(baseline.get(baseline_value_field))
        current_value = cls._number(current.get("value"))
        baseline_timestamp = cls._aware_datetime(baseline.get("provider_timestamp"))
        current_timestamp = cls._aware_datetime(current.get("provider_timestamp"))
        if (
            baseline_value is None
            or baseline_value <= 0
            or current_value is None
            or current_value <= 0
            or baseline_timestamp is None
            or current_timestamp is None
            or current_timestamp <= baseline_timestamp
        ):
            return None
        return round((current_value / baseline_value - 1.0) * 100.0, 6)

    @classmethod
    def _return_input_component(
        cls,
        value: Any,
        *,
        expected_symbol: str,
        checked_at: datetime,
        blocker_prefix: str,
    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        component, blockers = cls._timestamped_component(
            value,
            checked_at=checked_at,
            blocker_prefix=blocker_prefix,
        )
        actual_symbol = cls._text(cls._field(value, "symbol", "code", "stock_code"))
        if not actual_symbol or cls._canonical(actual_symbol) != cls._canonical(expected_symbol):
            blockers.append(f"{blocker_prefix}_identity_mismatch")
        current_price = cls._number(
            cls._field(value, "current_price", "price", "latest_price", "last_price")
        )
        reference_price = cls._number(cls._field(value, "pre_close", "previous_close"))
        if current_price is None or current_price <= 0:
            blockers.append(f"{blocker_prefix}_current_price_missing")
        if reference_price is None or reference_price <= 0:
            blockers.append(f"{blocker_prefix}_reference_price_missing")
        if component is None or blockers:
            return None, list(dict.fromkeys(blockers))
        component.update(
            {
                "symbol": expected_symbol,
                "current_price": current_price,
                "reference_price": reference_price,
                "return_pct": round(
                    (current_price / reference_price - 1.0) * 100.0,
                    6,
                ),
            }
        )
        return component, []

    @classmethod
    def _timestamped_component(
        cls,
        value: Any,
        *,
        checked_at: datetime,
        blocker_prefix: str,
    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        if value is None:
            return None, [f"{blocker_prefix}_missing"]
        source = cls._source_text(cls._field(value, "source", "provider"))
        provider_timestamp = cls._aware_datetime(
            cls._field(value, "provider_timestamp", "quote_timestamp", "timestamp")
        )
        blockers = []
        if not source:
            blockers.append(f"{blocker_prefix}_source_missing")
        if provider_timestamp is None:
            blockers.append(f"{blocker_prefix}_timestamp_missing")
        elif provider_timestamp > checked_at:
            blockers.append(f"{blocker_prefix}_timestamp_after_check")
        elif checked_at - provider_timestamp > cls.MAX_QUOTE_AGE:
            blockers.append(f"{blocker_prefix}_stale")
        if blockers:
            return None, blockers
        return {
            "source": source,
            "provider_timestamp": provider_timestamp.isoformat(),
        }, []

    @classmethod
    def _timestamps_aligned(
        cls,
        left: Optional[datetime],
        right: Optional[datetime],
    ) -> bool:
        return bool(
            left is not None
            and right is not None
            and abs((left - right).total_seconds())
            <= cls.MAX_PRODUCT_TIMESTAMP_ALIGNMENT.total_seconds()
        )

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
        source = cls._source_text(cls._field(quote, "source", "provider"))
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
        volume = cls._number(cls._field(quote, "volume"))
        vwap = cls._number(cls._field(quote, "vwap"))
        spread_provenance, spread_blockers = cls._liquidity_provenance(
            quote,
            component="spread",
            source_field="bid_ask_source",
            timestamp_field="bid_ask_provider_timestamp",
            default_source=source,
            default_as_of=as_of,
            checked_at=checked_at,
            value_present=spread_bps is not None,
        )
        volume_provenance, volume_blockers = cls._liquidity_provenance(
            quote,
            component="volume",
            source_field="volume_source",
            timestamp_field="volume_provider_timestamp",
            default_source=source,
            default_as_of=as_of,
            checked_at=checked_at,
            value_present=volume is not None,
        )
        vwap_provenance, vwap_blockers = cls._liquidity_provenance(
            quote,
            component="vwap",
            source_field="vwap_source",
            timestamp_field="vwap_provider_timestamp",
            default_source=source,
            default_as_of=as_of,
            checked_at=checked_at,
            value_present=vwap is not None,
        )
        blockers.extend(spread_blockers)
        blockers.extend(volume_blockers)
        blockers.extend(vwap_blockers)
        return {
            "price": price,
            "trading_status": trading_status or None,
            "spread_bps": spread_bps,
            "spread_source": spread_provenance.get("source"),
            "spread_as_of": spread_provenance.get("provider_timestamp"),
            "volume": volume,
            "volume_source": volume_provenance.get("source"),
            "volume_as_of": volume_provenance.get("provider_timestamp"),
            "volume_ratio": cls._number(cls._field(quote, "volume_ratio")),
            "vwap": vwap,
            "vwap_source": vwap_provenance.get("source"),
            "vwap_as_of": vwap_provenance.get("provider_timestamp"),
            "vwap_method": cls._text(cls._field(quote, "vwap_method")) or None,
            "source": source or None,
            "as_of": as_of.isoformat() if as_of is not None else None,
            "blockers": list(dict.fromkeys(blockers)),
        }

    @classmethod
    def _liquidity_provenance(
        cls,
        quote: Any,
        *,
        component: str,
        source_field: str,
        timestamp_field: str,
        default_source: str,
        default_as_of: Optional[datetime],
        checked_at: datetime,
        value_present: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        explicit_source = cls._field(quote, source_field)
        explicit_timestamp = cls._field(quote, timestamp_field)
        explicit = explicit_source is not None or explicit_timestamp is not None
        source = cls._source_text(explicit_source) if explicit else default_source
        as_of = cls._aware_datetime(explicit_timestamp) if explicit else default_as_of
        blockers: list[str] = []
        if value_present:
            if not source:
                blockers.append(f"execution_{component}_source_missing")
            if as_of is None:
                blockers.append(f"execution_{component}_timestamp_missing")
            elif as_of > checked_at:
                blockers.append(f"execution_{component}_after_check")
            elif checked_at - as_of > cls.MAX_QUOTE_AGE:
                blockers.append(f"execution_{component}_stale")
            if (
                as_of is not None
                and default_as_of is not None
                and not cls._timestamps_aligned(as_of, default_as_of)
            ):
                blockers.append(f"execution_{component}_timestamp_misaligned")
        return {
            "source": source or None,
            "provider_timestamp": as_of.isoformat() if as_of is not None else None,
        }, blockers

    @staticmethod
    def _product_blockers(
        instrument: Optional[Mapping[str, Any]],
        *,
        current: Mapping[str, Any],
        cutoff: Optional[datetime],
        product_execution_evidence: Optional[Mapping[str, Any]] = None,
    ) -> list[str]:
        instrument_type = str((instrument or {}).get("instrument_type") or "")
        daily_reset = bool(
            instrument_type == "daily_leveraged_product"
            or (instrument or {}).get("daily_reset") is True
        )
        if instrument_type != "qdii" and not daily_reset:
            return []
        blockers: list[str] = []
        frozen_ready = bool(
            cutoff is not None
            and frozen_product_evidence_is_ready(instrument or {}, cutoff=cutoff)
        )
        dynamic_qdii_ready = bool(
            instrument_type == "qdii"
            and isinstance(product_execution_evidence, Mapping)
            and product_execution_evidence.get("status") == "ready"
        )
        if not frozen_ready and not dynamic_qdii_ready:
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
    def _source_text(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip()

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
