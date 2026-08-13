# -*- coding: utf-8 -*-
"""Prepare bounded market evidence for current portfolio research."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from importlib import metadata
import math
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sqlalchemy import select

from data_provider.base import DataFetcherManager
from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.baostock_fetcher import BaostockFetcher
from data_provider.tencent_fetcher import TencentFetcher
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.core.trading_calendar import (
    MARKET_TIMEZONE,
    get_effective_trading_date,
    get_market_for_stock,
)
from src.repositories.portfolio_market_evidence_repo import (
    PortfolioMarketEvidenceRepository,
)
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.portfolio_service import PortfolioService
from src.services.portfolio_research_scope import (
    research_scope_payload,
    resolve_research_scope,
)
from src.services.portfolio_research_product_evidence import (
    PRODUCT_EVIDENCE_SCHEMA_VERSION,
    build_product_evidence_component,
    product_evidence_from_instrument,
)
from src.storage import PortfolioPositionLot

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency path
    yf = None


_UNSET = object()


class PortfolioResearchEvidenceService:
    """Prepare research inputs without running analysis or mutating the ledger."""

    SCHEMA_VERSION = "portfolio-research-evidence-prepare-v2"
    BENCHMARK_ROUTES = {
        "cn": {
            "storage_code": "000300",
            "fetch_code": "sh000300",
            "provider": "baostock",
            "source": "BaostockFetcher",
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
    REALTIME_MAX_AGE_SECONDS = 15 * 60
    LEGACY_SOURCES = frozenset({"TencentFetcher", "YfinanceFetcher"})

    def __init__(
        self,
        portfolio_service: Optional[PortfolioService] = None,
        *,
        stock_repo: Optional[StockRepository] = None,
        market_evidence_repo: Optional[PortfolioMarketEvidenceRepository] = None,
        portfolio_repo: Optional[PortfolioRepository] = None,
        fetcher_manager: Optional[DataFetcherManager] = None,
        baostock_benchmark_fetcher: Optional[Any] = None,
        tencent_benchmark_fetcher: Optional[Any] = None,
        yfinance_benchmark_fetcher: Optional[Any] = None,
        cutoff_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        as_of_provider: Optional[Callable[[], date]] = None,
        fx_fetcher: Optional[Callable[[str, str, date], Dict[str, Any]]] = None,
        qdii_nav_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
        qdii_reference_fetcher: Optional[Callable[..., Any]] = None,
        qdii_completed_tracking_fetcher: Optional[Callable[..., Any]] = None,
        realtime_fx_quote_fetcher: Optional[Callable[..., Any]] = None,
        realtime_quote_fetcher: Optional[Callable[..., Any]] = None,
        holding_period_evaluator: Optional[Callable[..., Dict[str, Any]]] = None,
        collect_product_evidence: bool = True,
        fixed_position_fetchers: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.portfolio_service = portfolio_service or PortfolioService()
        self.stock_repo = stock_repo or StockRepository()
        self.market_evidence_repo = market_evidence_repo or (
            PortfolioMarketEvidenceRepository(self.stock_repo.db)
        )
        self.portfolio_repo = portfolio_repo or PortfolioRepository()
        self.fetcher_manager = fetcher_manager or DataFetcherManager()
        self._benchmark_fetchers = {
            "baostock": baostock_benchmark_fetcher or BaostockFetcher(),
            "tencent": tencent_benchmark_fetcher or TencentFetcher(),
            "yfinance": yfinance_benchmark_fetcher or YfinanceFetcher(),
        }
        self._fixed_position_fetchers = (
            {"cn": self._benchmark_fetchers["baostock"]}
            if fixed_position_fetchers is None
            else dict(fixed_position_fetchers)
        )
        self.cutoff_provider = cutoff_provider
        self.as_of_provider = as_of_provider
        self.fx_fetcher = fx_fetcher or self._fetch_fx_quote
        self.qdii_nav_fetcher = qdii_nav_fetcher or self._fetch_qdii_nav
        self.qdii_reference_fetcher = (
            qdii_reference_fetcher or self._fetch_qdii_reference
        )
        self.qdii_completed_tracking_fetcher = (
            qdii_completed_tracking_fetcher
            or self._fetch_qdii_completed_session_tracking
        )
        self.realtime_fx_quote_fetcher = (
            realtime_fx_quote_fetcher or self._fetch_realtime_fx_quote
        )
        self.realtime_quote_fetcher = realtime_quote_fetcher or self._fetch_realtime_quote
        self.holding_period_evaluator = (
            holding_period_evaluator or self._evaluate_holding_period
        )
        self.collect_product_evidence = bool(collect_product_evidence)

    def prepare(
        self,
        *,
        scope: Optional[Sequence[Any]] = None,
        cutoff: Optional[datetime] = None,
        establish_cutoff: bool = False,
    ) -> Dict[str, Any]:
        legacy_date_clock = cutoff is None and self.as_of_provider is not None
        requested_cutoff = self._resolve_cutoff(cutoff=cutoff)
        as_of = requested_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
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

        scope_positions = [
            {
                "account_id": int(account["account_id"]),
                "market": position.get("market"),
                "symbol": position.get("symbol"),
            }
            for account, position in positions
        ]
        resolved_scope = resolve_research_scope(
            scope,
            positive_positions=scope_positions,
        )
        scope_keys = set(resolved_scope)
        positions = [
            (account, position)
            for account, position in positions
            if (
                int(account["account_id"]),
                str(position.get("market") or "").strip().lower(),
                str(position.get("symbol") or "").strip().upper(),
            )
            in scope_keys
        ]
        scope_payload = research_scope_payload(resolved_scope)
        instruments = {
            (str(row.market or "").strip().lower(), str(row.symbol or "").strip().upper()): row
            for row in self.portfolio_repo.list_instruments()
        }
        prefetched_product_inputs = None
        cutoff_value = requested_cutoff
        if establish_cutoff:
            prefetched_product_inputs = self._prefetch_dynamic_product_inputs(
                positions=positions,
                instruments=instruments,
                cutoff=requested_cutoff,
            )
            cutoff_value = self._resolve_cutoff(cutoff=self.cutoff_provider())
            self._validate_established_cutoff(
                requested_cutoff=requested_cutoff,
                established_cutoff=cutoff_value,
            )

        if positions:
            items = self._prepare_positions(
                positions=positions,
                cutoff=None if legacy_date_clock else cutoff_value,
                as_of=as_of,
                instruments=instruments,
                prefetched_product_inputs=prefetched_product_inputs,
            )
            ready_count = sum(item["status"] == "ready" for item in items)
            insufficient_count = len(items) - ready_count
            return {
                "schema_version": self.SCHEMA_VERSION,
                "scope": scope_payload,
                "prepared_at": datetime.now(timezone.utc).isoformat(),
                "cutoff": cutoff_value.isoformat(),
                "as_of": as_of.isoformat(),
                "status": "ready" if insufficient_count == 0 else "partial",
                "position_count": len(items),
                "ready_count": ready_count,
                "insufficient_count": insufficient_count,
                "items": items,
            }
        return {
            "schema_version": self.SCHEMA_VERSION,
            "scope": scope_payload,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "cutoff": cutoff_value.isoformat(),
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
        cutoff: Optional[datetime],
        as_of: date,
        instruments: Optional[Mapping[tuple[str, str], Any]] = None,
        prefetched_product_inputs: Optional[
            Mapping[tuple[str, str], Mapping[str, Any]]
        ] = None,
    ) -> list[Dict[str, Any]]:
        benchmark_cache: Dict[str, Dict[str, Any]] = {}
        fx_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
        instrument_map = dict(instruments) if instruments is not None else {
            (str(row.market or "").strip().lower(), str(row.symbol or "").strip().upper()): row
            for row in self.portfolio_repo.list_instruments()
        }
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

            position_fetcher = self._fixed_position_fetchers.get(market)
            position_source = (
                str(getattr(position_fetcher, "name", "") or "").strip()
                if position_fetcher is not None
                else None
            )
            price = self._prepare_bar(
                fetch_code=symbol,
                storage_code=symbol,
                cutoff=cutoff,
                as_of=as_of,
                blocker_prefix="position",
                market=market,
                direct_fetcher=position_fetcher,
                expected_source=position_source,
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
                        cutoff=cutoff,
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
            product_key = (market, symbol.upper())
            instrument = instrument_map.get(product_key)
            product_cutoff = cutoff or datetime.combine(
                as_of,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            product_evidence = product_evidence_from_instrument(
                instrument,
                cutoff=product_cutoff,
            )
            if (
                self.collect_product_evidence
                and instrument is not None
                and isinstance(product_evidence, dict)
                and product_evidence.get("status") != "ready"
            ):
                collected = self._collect_product_evidence(
                    instrument=instrument,
                    account_id=int(account["account_id"]),
                    price=price,
                    cutoff=product_cutoff,
                    as_of=as_of,
                    dynamic_inputs=(
                        prefetched_product_inputs.get(product_key)
                        if prefetched_product_inputs is not None
                        else None
                    ),
                )
                if collected is not None:
                    product_evidence = product_evidence_from_instrument(
                        {
                            **self._instrument_identity(instrument),
                            "product_evidence": collected,
                        },
                        cutoff=product_cutoff,
                    )
            blockers.extend((product_evidence or {}).get("blockers") or [])
            blockers = list(dict.fromkeys(blockers))
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
                    "product_evidence": (
                        self._without_internal_blockers(product_evidence)
                        if product_evidence is not None
                        else None
                    ),
                    "blockers": blockers,
                }
            )
        return items

    def _prefetch_dynamic_product_inputs(
        self,
        *,
        positions: Sequence[tuple[Dict[str, Any], Dict[str, Any]]],
        instruments: Mapping[tuple[str, str], Any],
        cutoff: datetime,
    ) -> Dict[tuple[str, str], Dict[str, Any]]:
        prefetched: Dict[tuple[str, str], Dict[str, Any]] = {}
        if not self.collect_product_evidence:
            return prefetched
        for _account, position in positions:
            key = (
                str(position.get("market") or "").strip().lower(),
                str(position.get("symbol") or "").strip().upper(),
            )
            if key in prefetched:
                continue
            instrument = instruments.get(key)
            if instrument is None:
                continue
            current = product_evidence_from_instrument(instrument, cutoff=cutoff)
            if not isinstance(current, dict) or current.get("status") == "ready":
                continue
            identity = self._instrument_identity(instrument)
            instrument_type = identity["instrument_type"]
            if instrument_type not in {"qdii", "daily_leveraged_product"}:
                continue
            inputs = {
                "quote": self._load_product_input(
                    self.realtime_quote_fetcher,
                    symbol=identity["symbol"],
                    market=identity["market"],
                    cutoff=cutoff,
                )
            }
            if instrument_type == "qdii":
                inputs["reference"] = self._load_product_input(
                    self.qdii_reference_fetcher,
                    symbol=identity["symbol"],
                    market=identity["market"],
                    cutoff=cutoff,
                )
                inputs["fx"] = self._load_product_input(
                    self.realtime_fx_quote_fetcher,
                    from_currency=identity["underlying_currency"],
                    to_currency=identity["quote_currency"],
                    cutoff=cutoff,
                )
            prefetched[key] = inputs
        return prefetched

    @staticmethod
    def _validate_established_cutoff(
        *,
        requested_cutoff: datetime,
        established_cutoff: datetime,
    ) -> None:
        requested_utc = requested_cutoff.astimezone(timezone.utc)
        established_utc = established_cutoff.astimezone(timezone.utc)
        if established_utc < requested_utc:
            raise ValueError("research_cutoff_cannot_move_backward")
        shanghai = ZoneInfo("Asia/Shanghai")
        if requested_cutoff.astimezone(shanghai).date() != established_cutoff.astimezone(
            shanghai
        ).date():
            raise ValueError("research_cutoff_date_changed_during_preparation")

    def _prepare_bar(
        self,
        *,
        fetch_code: str,
        storage_code: str,
        cutoff: Optional[datetime] = None,
        as_of: Optional[date] = None,
        blocker_prefix: str,
        market: Optional[str] = None,
        direct_fetcher: Optional[Any] = None,
        expected_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        resolved_market = market or get_market_for_stock(storage_code)
        if cutoff is None and as_of is not None:
            timezone_name = MARKET_TIMEZONE.get(resolved_market or "")
            if timezone_name is None:
                raise ValueError("research_market_timezone_missing")
            cutoff_value = datetime.combine(
                as_of,
                datetime.min.time(),
                tzinfo=ZoneInfo(timezone_name),
            )
        else:
            cutoff_value = self._resolve_cutoff(cutoff=cutoff)
        expected_date = self._expected_daily_bar_date(
            market=resolved_market,
            cutoff=cutoff_value,
        )
        batch = (
            self.market_evidence_repo.get_latest_batch(
                code=storage_code,
                cutoff=cutoff_value,
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
                filtered = self._filter_frame_at_or_before(
                    frame,
                    target_date=expected_date,
                )
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
                    captured_at=cutoff_value,
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
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "amount": float(row.amount),
            "pct_chg": float(row.pct_chg),
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
            or str(batch.rows[-1].adjustment_identity or "").strip() == "unknown"
        ):
            return None
        if expected_source and batch.data_source != expected_source:
            return None
        return batch.rows[-1]

    @staticmethod
    def _expected_daily_bar_date(
        *,
        market: Optional[str],
        cutoff: datetime,
    ) -> Optional[date]:
        if not market:
            return None
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("research_cutoff_timezone_missing")
        return get_effective_trading_date(market, current_time=cutoff)

    def _resolve_cutoff(
        self,
        *,
        cutoff: Optional[datetime],
        legacy_as_of: Optional[date] = None,
    ) -> datetime:
        if cutoff is not None:
            value: Any = cutoff
        elif legacy_as_of is not None:
            value = legacy_as_of
        elif self.as_of_provider is not None:
            value = self.as_of_provider()
        else:
            value = self.cutoff_provider()
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("research_cutoff_timezone_missing")
            return value
        if isinstance(value, date):
            return datetime.combine(
                value,
                datetime.min.time(),
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        raise ValueError("research_cutoff_invalid")

    def _collect_product_evidence(
        self,
        *,
        instrument: Any,
        account_id: int,
        price: Dict[str, Any],
        cutoff: datetime,
        as_of: date,
        dynamic_inputs: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        identity = self._instrument_identity(instrument)
        instrument_type = identity["instrument_type"]
        if instrument_type == "qdii":
            return self._collect_qdii_product_evidence(
                identity=identity,
                price=price,
                cutoff=cutoff,
                as_of=as_of,
                dynamic_inputs=dynamic_inputs,
            )
        if instrument_type == "daily_leveraged_product":
            return self._collect_daily_reset_product_evidence(
                identity=identity,
                account_id=account_id,
                price=price,
                cutoff=cutoff,
                as_of=as_of,
                dynamic_inputs=dynamic_inputs,
            )
        return None

    def _collect_qdii_product_evidence(
        self,
        *,
        identity: Dict[str, Any],
        price: Dict[str, Any],
        cutoff: datetime,
        as_of: date,
        dynamic_inputs: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        raw = self._product_evidence_header(identity=identity, cutoff=cutoff)
        expected_date = self._coerce_optional_date(price.get("date"))
        if price.get("status") != "ready" or expected_date is None:
            return raw

        try:
            nav = self.qdii_nav_fetcher(
                symbol=identity["symbol"],
                expected_date=expected_date,
                cutoff=cutoff,
            )
        except Exception:
            nav = None
        if isinstance(nav, Mapping):
            nav_date = self._coerce_optional_date(nav.get("nav_date"))
            nav_value = self._optional_finite(nav.get("nav"))
            iopv_value = self._optional_finite(nav.get("iopv"))
            if nav_date == expected_date and (
                (nav_value is not None and nav_value > 0)
                or (iopv_value is not None and iopv_value > 0)
            ):
                values: Dict[str, Any] = {}
                if nav_value is not None and nav_value > 0:
                    values["nav"] = nav_value
                if iopv_value is not None and iopv_value > 0:
                    values["iopv"] = iopv_value
                nav_return = self._optional_finite(nav.get("nav_return_pct"))
                if nav_return is not None:
                    values["nav_return_pct"] = nav_return
                raw["nav_iopv"] = build_product_evidence_component(
                    as_of=cutoff,
                    source=nav.get("source"),
                    source_version=nav.get("source_version"),
                    effective_date=nav_date.isoformat(),
                    **values,
                )
                reference_value = iopv_value or nav_value
                price_close = self._optional_finite(price.get("close"))
                if reference_value and price_close and price_close > 0:
                    raw["premium_discount"] = build_product_evidence_component(
                        as_of=cutoff,
                        source=f"{price.get('source')}+{nav.get('source')}",
                        source_version=(
                            f"{price.get('source_version')}+{nav.get('source_version')}"
                        ),
                        premium_discount_pct=round(
                            (price_close / reference_value - 1.0) * 100.0,
                            6,
                        ),
                        market_price=price_close,
                        reference_value=reference_value,
                    )

        completed_tracking = self._load_product_input(
            self.qdii_completed_tracking_fetcher,
            symbol=identity["symbol"],
            completed_through=self._expected_daily_bar_date(
                market=identity["market"],
                cutoff=cutoff,
            ),
            cutoff=cutoff,
        )
        tracking_difference = self._optional_finite(
            self._field(completed_tracking, "tracking_difference_pct")
        )
        if (
            isinstance(completed_tracking, Mapping)
            and tracking_difference is not None
            and completed_tracking.get("formula") == "product_return-reference_return"
            and completed_tracking.get("fx_incorporated_in_reference") is True
            and str(completed_tracking.get("source") or "").strip()
            and str(completed_tracking.get("source_version") or "").strip()
        ):
            raw["tracking"] = build_product_evidence_component(
                as_of=cutoff,
                **dict(completed_tracking),
            )
        fx = self._prepare_fx(
            from_currency=identity["underlying_currency"],
            to_currency=identity["quote_currency"],
            as_of=expected_date,
        )
        fx_date = self._coerce_optional_date(fx.get("rate_date"))
        if fx.get("status") == "ready" and fx_date == expected_date:
            raw["underlying_fx"] = build_product_evidence_component(
                as_of=cutoff,
                source=fx.get("source"),
                source_version=fx.get("source_version"),
                pair=f"{identity['underlying_currency']}/{identity['quote_currency']}",
                rate=fx.get("rate"),
                return_pct=fx.get("return_pct"),
                effective_date=fx_date.isoformat(),
            )

        realtime_quote = (
            dynamic_inputs.get("quote")
            if dynamic_inputs is not None
            else self._load_product_input(
                self.realtime_quote_fetcher,
                symbol=identity["symbol"],
                market=identity["market"],
                cutoff=cutoff,
            )
        )
        spread = self._collect_spread_component(
            symbol=identity["symbol"],
            market=identity["market"],
            cutoff=cutoff,
            quote=realtime_quote,
        )
        if spread is not None:
            raw["spread"] = spread
        observation = self._collect_qdii_execution_reference_observation(
            identity=identity,
            quote=realtime_quote,
            cutoff=cutoff,
            reference=(
                dynamic_inputs.get("reference")
                if dynamic_inputs is not None
                else _UNSET
            ),
            fx=dynamic_inputs.get("fx") if dynamic_inputs is not None else _UNSET,
        )
        if observation is not None:
            raw["execution_reference_observation"] = observation
            observation_market = observation["market_price"]
            observation_reference = observation["reference_value"]
            observation_fx = observation["fx"]
            raw["nav_iopv"] = build_product_evidence_component(
                as_of=observation_reference["provider_timestamp"],
                source=observation_reference["source"],
                source_version=observation_reference["source_version"],
                iopv=observation_reference["value"],
                reference_type=observation_reference["reference_type"],
                provider_timestamp=observation_reference["provider_timestamp"],
            )
            raw["premium_discount"] = build_product_evidence_component(
                as_of=max(
                    self._coerce_optional_datetime(
                        observation_market["provider_timestamp"]
                    ),
                    self._coerce_optional_datetime(
                        observation_reference["provider_timestamp"]
                    ),
                ),
                source=(
                    f"{observation_market['source']}+{observation_reference['source']}"
                ),
                source_version=(
                    f"{observation_market['source_version']}+"
                    f"{observation_reference['source_version']}"
                ),
                premium_discount_pct=round(
                    (
                        observation_market["value"]
                        / observation_reference["value"]
                        - 1.0
                    )
                    * 100.0,
                    6,
                ),
                market_price=observation_market["value"],
                reference_value=observation_reference["value"],
                provider_timestamps=[
                    observation_market["provider_timestamp"],
                    observation_reference["provider_timestamp"],
                ],
            )
            raw["underlying_fx"] = build_product_evidence_component(
                as_of=observation_fx["provider_timestamp"],
                source=observation_fx["source"],
                source_version=observation_fx["source_version"],
                pair=observation_fx["pair"],
                rate=observation_fx["rate"],
                provider_timestamp=observation_fx["provider_timestamp"],
            )

        return raw

    def _collect_daily_reset_product_evidence(
        self,
        *,
        identity: Dict[str, Any],
        account_id: int,
        price: Dict[str, Any],
        cutoff: datetime,
        as_of: date,
        dynamic_inputs: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        raw = self._product_evidence_header(identity=identity, cutoff=cutoff)
        terms_as_of = self._coerce_optional_datetime(identity.get("evidence_as_of"))
        terms_url = str(identity.get("evidence_source") or "").strip()
        terms_verified = bool(
            identity.get("verification_status") == "verified"
            and terms_as_of is not None
            and terms_as_of <= cutoff.astimezone(timezone.utc)
            and urlparse(terms_url).scheme in {"http", "https"}
            and identity.get("daily_reset") is True
            and self._positive(identity.get("leverage_factor"))
        )
        if terms_verified:
            raw["official_terms"] = build_product_evidence_component(
                as_of=terms_as_of,
                source=terms_url,
                source_version=terms_as_of.isoformat(),
                terms_url=terms_url,
                daily_reset=True,
                leverage_factor=identity["leverage_factor"],
            )
            raw["path_decay_rebalance"] = build_product_evidence_component(
                as_of=terms_as_of,
                source=terms_url,
                source_version=terms_as_of.isoformat(),
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            )

        underlying = self._prepare_product_underlying(identity=identity, cutoff=cutoff, as_of=as_of)
        if underlying.get("status") == "ready":
            raw["underlying_same_cutoff"] = build_product_evidence_component(
                as_of=underlying.get("captured_at") or cutoff,
                source=underlying.get("source"),
                source_version=underlying.get("source_version"),
                market=identity["underlying_market"],
                symbol=identity["underlying_symbol"],
                currency=identity["underlying_currency"],
                completed_session=True,
                session_date=underlying.get("date"),
                evidence_bar_hash=underlying.get("evidence_bar_hash"),
            )
            product_return = self._optional_finite(price.get("pct_chg"))
            underlying_return = self._optional_finite(underlying.get("pct_chg"))
            if (
                price.get("status") == "ready"
                and product_return is not None
                and underlying_return is not None
                and not math.isclose(underlying_return, 0.0, rel_tol=0.0, abs_tol=1e-12)
            ):
                raw["completed_session_leverage"] = build_product_evidence_component(
                    as_of=cutoff,
                    source=f"{price.get('source')}+{underlying.get('source')}",
                    source_version=(
                        f"{price.get('source_version')}+{underlying.get('source_version')}"
                    ),
                    leverage_factor=identity["leverage_factor"],
                    product_return_pct=product_return,
                    underlying_return_pct=underlying_return,
                    observed_leverage=round(product_return / underlying_return, 6),
                )

        spread = self._collect_spread_component(
            symbol=identity["symbol"],
            market=identity["market"],
            cutoff=cutoff,
            quote=(
                dynamic_inputs.get("quote")
                if dynamic_inputs is not None
                else _UNSET
            ),
        )
        if spread is not None:
            raw["liquidity"] = spread

        session_date = self._coerce_optional_date(price.get("date"))
        if session_date is not None:
            try:
                horizon = self.holding_period_evaluator(
                    account_id=account_id,
                    market=identity["market"],
                    symbol=identity["symbol"],
                    session_date=session_date,
                )
            except Exception:
                horizon = None
            if isinstance(horizon, Mapping) and horizon.get("evaluated") is True:
                raw["horizon_fit"] = build_product_evidence_component(
                    as_of=cutoff,
                    source=horizon.get("source"),
                    source_version=horizon.get("source_version"),
                    **{
                        key: value
                        for key, value in horizon.items()
                        if key not in {"source", "source_version"}
                    },
                )
        return raw

    def _prepare_product_underlying(
        self,
        *,
        identity: Dict[str, Any],
        cutoff: datetime,
        as_of: date,
    ) -> Dict[str, Any]:
        symbol = identity.get("underlying_symbol")
        market = identity.get("underlying_market")
        if not symbol or not market:
            return {"status": "insufficient", "blockers": ["underlying_identity_missing"]}
        return self._prepare_bar(
            fetch_code=str(symbol),
            storage_code=str(symbol),
            cutoff=cutoff,
            as_of=as_of,
            blocker_prefix="underlying",
            market=str(market),
        )

    def _collect_spread_component(
        self,
        *,
        symbol: str,
        market: str,
        cutoff: datetime,
        quote: Any = _UNSET,
    ) -> Optional[Dict[str, Any]]:
        if quote is _UNSET:
            quote = self._load_product_input(
                self.realtime_quote_fetcher,
                symbol=symbol,
                market=market,
                cutoff=cutoff,
            )
        bid = self._optional_finite(self._field(quote, "bid"))
        ask = self._optional_finite(self._field(quote, "ask"))
        provider_timestamp = self._coerce_optional_datetime(
            self._field(quote, "provider_timestamp")
        )
        if (
            bid is None
            or ask is None
            or bid <= 0
            or ask < bid
            or provider_timestamp is None
            or provider_timestamp > cutoff.astimezone(timezone.utc)
            or (cutoff.astimezone(timezone.utc) - provider_timestamp).total_seconds()
            > self.REALTIME_MAX_AGE_SECONDS
            or self._field(quote, "is_stale") is True
        ):
            return None
        source = self._source_token(self._field(quote, "source"))
        source_version = str(self._field(quote, "source_version") or "").strip()
        if not source_version:
            source_version = self._installed_source_version(source)
        if not source or not source_version:
            return None
        midpoint = (ask + bid) / 2.0
        return build_product_evidence_component(
            as_of=provider_timestamp,
            source=source,
            source_version=source_version,
            spread_bps=round((ask - bid) / midpoint * 10000.0, 6),
            bid=bid,
            ask=ask,
        )

    def _collect_qdii_execution_reference_observation(
        self,
        *,
        identity: Mapping[str, Any],
        quote: Any,
        cutoff: datetime,
        reference: Any = _UNSET,
        fx: Any = _UNSET,
    ) -> Optional[Dict[str, Any]]:
        if reference is _UNSET:
            reference = self._load_product_input(
                self.qdii_reference_fetcher,
                symbol=identity["symbol"],
                market=identity["market"],
                cutoff=cutoff,
            )
        if fx is _UNSET:
            fx = self._load_product_input(
                self.realtime_fx_quote_fetcher,
                from_currency=identity["underlying_currency"],
                to_currency=identity["quote_currency"],
                cutoff=cutoff,
            )
        market_price = self._optional_finite(
            self._first_field(quote, "current_price", "price", "latest_price", "last_price")
        )
        reference_value = self._optional_finite(
            self._first_field(reference, "reference_value", "iopv", "value")
        )
        fx_rate = self._optional_finite(self._first_field(fx, "rate", "price"))
        reference_type = str(
            self._first_field(reference, "reference_type", "kind") or ""
        ).strip()
        expected_pair = f"{identity['underlying_currency']}/{identity['quote_currency']}"
        fx_pair = str(self._first_field(fx, "pair", "code") or "").strip().upper()
        values = (
            (quote, market_price),
            (reference, reference_value),
            (fx, fx_rate),
        )
        timestamps = [
            self._coerce_optional_datetime(
                self._first_field(value, "provider_timestamp", "quote_timestamp", "timestamp")
            )
            for value, _ in values
        ]
        sources = [
            self._source_token(self._first_field(value, "source", "provider"))
            for value, _ in values
        ]
        cutoff_utc = cutoff.astimezone(timezone.utc)
        if (
            market_price is None
            or market_price <= 0
            or reference_value is None
            or reference_value <= 0
            or fx_rate is None
            or fx_rate <= 0
            or not reference_type
            or fx_pair != expected_pair
            or not all(timestamps)
            or not all(sources)
        ):
            return None
        normalized_timestamps = [value.astimezone(timezone.utc) for value in timestamps if value]
        if any(
            value > cutoff_utc
            or cutoff_utc - value > timedelta(seconds=self.REALTIME_MAX_AGE_SECONDS)
            for value in normalized_timestamps
        ):
            return None
        alignment_seconds = (max(normalized_timestamps) - min(normalized_timestamps)).total_seconds()
        if alignment_seconds > 120:
            return None
        source_versions = [
            str(self._first_field(value, "source_version") or "").strip()
            or self._installed_source_version(source)
            or ("sse-yunhq-v1" if source == "sse-yunhq" else "")
            for (value, _), source in zip(values, sources)
        ]
        if not all(source_versions):
            return None
        product_ts, reference_ts, fx_ts = normalized_timestamps
        return build_product_evidence_component(
            as_of=max(normalized_timestamps),
            source="+".join(sources),
            source_version="+".join(source_versions),
            market_price={
                "value": market_price,
                "source": sources[0],
                "source_version": source_versions[0],
                "provider_timestamp": product_ts.isoformat(),
            },
            reference_value={
                "reference_type": reference_type,
                "value": reference_value,
                "source": sources[1],
                "source_version": source_versions[1],
                "provider_timestamp": reference_ts.isoformat(),
            },
            fx={
                "pair": expected_pair,
                "rate": fx_rate,
                "source": sources[2],
                "source_version": source_versions[2],
                "provider_timestamp": fx_ts.isoformat(),
            },
            timestamp_alignment_seconds=alignment_seconds,
        )

    @staticmethod
    def _load_product_input(loader: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            return loader(**kwargs)
        except Exception:
            return None

    @classmethod
    def _first_field(cls, value: Any, *keys: str) -> Any:
        for key in keys:
            candidate = cls._field(value, key)
            if candidate is not None:
                return candidate
        return None

    @staticmethod
    def _fetch_qdii_reference(*, symbol: str, **_: Any) -> Any:
        return AkshareFetcher().get_sse_etf_iopv(symbol)

    @staticmethod
    def _fetch_qdii_completed_session_tracking(
        *,
        symbol: str,
        completed_through: date,
        **_: Any,
    ) -> Any:
        return AkshareFetcher().get_sse_etf_completed_session_tracking(
            symbol,
            completed_through=completed_through,
        )

    @staticmethod
    def _fetch_realtime_fx_quote(
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
        return YfinanceFetcher().get_realtime_fx_quote(from_currency, to_currency)

    def _fetch_realtime_quote(self, **kwargs: Any) -> Any:
        return self.fetcher_manager.get_realtime_quote(
            str(kwargs.get("symbol") or ""),
            log_final_failure=False,
            supplement=False,
            preserve_provider_symbol=True,
        )

    @staticmethod
    def _fetch_qdii_nav(
        *,
        symbol: str,
        expected_date: date,
        cutoff: datetime,
    ) -> Dict[str, Any]:
        del cutoff
        endpoint = "https://api.fund.eastmoney.com/f10/lsjz"
        response = requests.get(
            endpoint,
            params={
                "fundCode": symbol,
                "pageIndex": "1",
                "pageSize": "5",
                "startDate": expected_date.isoformat(),
                "endDate": expected_date.isoformat(),
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html",
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        payload = response.json()
        rows = ((payload.get("Data") or {}).get("LSJZList") or [])
        for row in rows:
            nav_date = PortfolioResearchEvidenceService._coerce_optional_date(row.get("FSRQ"))
            if nav_date != expected_date:
                continue
            nav = PortfolioResearchEvidenceService._optional_finite(row.get("DWJZ"))
            if nav is None or nav <= 0:
                continue
            return {
                "nav": nav,
                "nav_date": nav_date,
                "nav_return_pct": PortfolioResearchEvidenceService._optional_finite(
                    row.get("JZZZL")
                ),
                "source": f"{endpoint}?fundCode={symbol}",
                "source_version": "eastmoney-f10-lsjz-v1",
            }
        raise ValueError("same-cutoff QDII NAV unavailable")

    def _evaluate_holding_period(
        self,
        *,
        account_id: int,
        market: str,
        symbol: str,
        session_date: date,
    ) -> Dict[str, Any]:
        with self.portfolio_repo.db.get_session() as session:
            rows = list(
                session.execute(
                    select(PortfolioPositionLot).where(
                        PortfolioPositionLot.account_id == account_id,
                        PortfolioPositionLot.cost_method == "fifo",
                        PortfolioPositionLot.market == market,
                        PortfolioPositionLot.symbol == symbol,
                        PortfolioPositionLot.remaining_quantity > 0,
                    )
                ).scalars().all()
            )
        if not rows:
            raise ValueError("position holding period unavailable")
        open_dates = sorted({row.open_date for row in rows if row.open_date is not None})
        if not open_dates:
            raise ValueError("position holding period unavailable")
        return {
            "evaluated": True,
            "fits_holding_period": all(value == session_date for value in open_dates),
            "first_open_date": open_dates[0].isoformat(),
            "session_date": session_date.isoformat(),
            "source": "dsa-ledger:portfolio_position_lots",
            "source_version": "portfolio-product-horizon-v1",
        }

    @staticmethod
    def _product_evidence_header(
        *,
        identity: Dict[str, Any],
        cutoff: datetime,
    ) -> Dict[str, Any]:
        return {
            "schema_version": PRODUCT_EVIDENCE_SCHEMA_VERSION,
            "instrument_type": identity["instrument_type"],
            "market": identity["market"],
            "symbol": identity["symbol"],
            "evidence_cutoff": cutoff.isoformat(),
        }

    @staticmethod
    def _instrument_identity(instrument: Any) -> Dict[str, Any]:
        fields = (
            "market",
            "symbol",
            "quote_currency",
            "instrument_type",
            "underlying_market",
            "underlying_symbol",
            "underlying_currency",
            "leverage_factor",
            "daily_reset",
            "verification_status",
            "evidence_source",
            "evidence_as_of",
        )
        values = {
            field: (
                instrument.get(field)
                if isinstance(instrument, Mapping)
                else getattr(instrument, field, None)
            )
            for field in fields
        }
        for field in ("market", "instrument_type", "underlying_market"):
            values[field] = str(values.get(field) or "").strip().lower()
        for field in ("symbol", "quote_currency", "underlying_symbol", "underlying_currency"):
            values[field] = str(values.get(field) or "").strip().upper()
        values["daily_reset"] = values.get("daily_reset") is True
        return values

    @staticmethod
    def _field(value: Any, key: str) -> Any:
        return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)

    @staticmethod
    def _source_token(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw or "").strip()

    @staticmethod
    def _installed_source_version(source: str) -> str:
        packages = {
            "yfinance": "yfinance",
            "akshare_em": "akshare",
            "akshare_qq": "akshare",
            "akshare_sina": "akshare",
            "efinance": "efinance",
        }
        package = packages.get(source.strip().lower())
        if package is None:
            return ""
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            return ""

    @staticmethod
    def _coerce_optional_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _coerce_optional_date(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value or ""))
        except ValueError:
            return None

    @staticmethod
    def _optional_finite(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _positive(value: Any) -> bool:
        number = PortfolioResearchEvidenceService._optional_finite(value)
        return number is not None and number > 0

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
            return_pct = self._optional_finite(quote.get("return_pct"))
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
        result = {
            "status": "ready",
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": rate,
            "rate_date": rate_date.isoformat(),
            "source": source,
            "source_version": source_version,
            "blockers": [],
        }
        if return_pct is not None:
            result["return_pct"] = return_pct
        return result

    @staticmethod
    def _filter_frame_at_or_before(
        frame: Any,
        *,
        target_date: Optional[date],
    ) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty or "date" not in frame.columns:
            return pd.DataFrame()
        if target_date is None:
            return pd.DataFrame()
        filtered = frame.copy()
        normalized_dates = pd.to_datetime(filtered["date"], errors="coerce")
        filtered = filtered.loc[normalized_dates.notna()].copy()
        normalized_dates = normalized_dates.loc[normalized_dates.notna()]
        filtered["date"] = normalized_dates.dt.date
        return filtered.loc[filtered["date"] <= target_date].copy()

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
        if any(
            name in normalized
            for name in ("efinance", "akshare", "baostock", "tencent")
        ):
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
        return_pct = None
        if len(close) >= 2 and float(close.iloc[-2]) > 0:
            return_pct = round(
                (float(close.iloc[-1]) / float(close.iloc[-2]) - 1.0) * 100.0,
                8,
            )
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
            "return_pct": return_pct,
            "source": "yfinance",
            "source_version": source_version,
        }
