from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from data_provider.akshare_fetcher import AkshareFetcher
from src.services.portfolio_research_execution_service import (
    PortfolioResearchExecutionService,
)
from src.services.portfolio_research_product_evidence import (
    product_evidence_from_instrument,
)


NOW = datetime(2026, 8, 6, 6, 30, tzinfo=timezone.utc)


def _hash(value: dict) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _component(**values) -> dict:
    payload = {
        "available": True,
        "as_of": "2026-08-06T06:00:00Z",
        "source": "fixture",
        "source_version": "v1",
        **values,
    }
    payload["source_hash"] = _hash(payload)
    return payload


def _qdii_execution_reference_observation() -> dict:
    return _component(
        market_price={
            "value": 1.10,
            "source": "baseline-product-fixture",
            "provider_timestamp": "2026-08-06T05:59:00Z",
        },
        reference_value={
            "reference_type": "iopv",
            "value": 1.08,
            "source": "baseline-iopv-fixture",
            "provider_timestamp": "2026-08-06T05:59:20Z",
        },
        fx={
            "pair": "USD/CNY",
            "rate": 7.18,
            "source": "baseline-fx-fixture",
            "provider_timestamp": "2026-08-06T05:59:10Z",
        },
        timestamp_alignment_seconds=20.0,
    )


def _quote(
    symbol: str,
    price: float,
    *,
    pre_close: float | None = None,
    provider_timestamp: str = "2026-08-06T06:29:00Z",
) -> dict:
    return {
        "symbol": symbol,
        "price": price,
        "pre_close": pre_close if pre_close is not None else price,
        "trading_status": "open",
        "bid": price - 0.001,
        "ask": price + 0.001,
        "volume": 100000,
        "volume_ratio": 1.2,
        "vwap": price - 0.002,
        "source": "fixture-quotes",
        "provider_timestamp": provider_timestamp,
    }


def _product_snapshot(
    *,
    symbol: str,
    market: str,
    instrument_type: str,
    instrument_fields: dict,
    frozen_components: dict,
) -> dict:
    snapshot = deepcopy(_snapshot())
    snapshot["scope"] = [{"account_id": 1, "market": market, "symbol": symbol}]
    snapshot["positions"] = [
        {
            "account_id": 1,
            "market": market,
            "symbol": symbol,
            "last_price": 1.0,
            "price_available": True,
        }
    ]
    snapshot["instruments"] = [
        {
            "market": market,
            "symbol": symbol,
            "name": symbol,
            "instrument_type": instrument_type,
            "verification_status": "verified",
            **instrument_fields,
        }
    ]
    benchmark_code = {"cn": "000300", "hk": "HSI", "us": "SPY"}[market]
    snapshot["benchmarks"] = [
        {"market": market, "code": benchmark_code, "price": 4000.0}
    ]
    instrument = snapshot["instruments"][0]
    instrument["product_evidence"] = product_evidence_from_instrument(
        {
            **instrument,
            "product_evidence": {
                "schema_version": "portfolio-product-evidence-v1",
                "market": market,
                "symbol": symbol,
                "instrument_type": instrument_type,
                "evidence_cutoff": snapshot["cutoff"],
                **frozen_components,
            },
        },
        cutoff=datetime.fromisoformat(snapshot["cutoff"].replace("Z", "+00:00")),
    )
    return snapshot


def _snapshot() -> dict:
    return {
        "snapshot_hash": "a" * 64,
        "cutoff": "2026-08-06T06:00:00Z",
        "scope": [{"account_id": 1, "market": "cn", "symbol": "510980"}],
        "positions": [
            {
                "account_id": 1,
                "market": "cn",
                "symbol": "510980",
                "last_price": 1.0,
                "price_available": True,
            },
            {
                "account_id": 1,
                "market": "cn",
                "symbol": "601899",
                "last_price": 20.0,
                "price_available": True,
            },
        ],
        "instruments": [
            {
                "market": "cn",
                "symbol": "510980",
                "name": "广发中证光伏龙头30ETF",
                "instrument_type": "etf",
                "verification_status": "verified",
            },
            {
                "market": "cn",
                "symbol": "601899",
                "name": "紫金矿业",
                "instrument_type": "equity",
                "verification_status": "verified",
            },
        ],
        "benchmarks": [{"market": "cn", "code": "000300", "price": 4000.0}],
        "risk_budget": {"evaluated": False},
    }


def test_execution_check_fetches_only_scope_and_action_sensitive_benchmark() -> None:
    calls: list[str] = []

    def quotes(code: str):
        calls.append(code)
        values = {
            "510980": _quote("510980", 1.0),
            "sh000300": _quote("sh000300", 4000.0),
        }
        return values.get(code)

    result = PortfolioResearchExecutionService(
        quote_loader=quotes,
        now=lambda: NOW,
    ).check(_snapshot(), research_snapshot_hash="f" * 64)

    assert calls == ["510980", "sh000300"]
    assert result["status"] == "ready"
    assert result["research_snapshot_hash"] == "f" * 64
    assert result["requires_reconfirmation"] is False
    assert result["items"][0]["changed_fields"] == []
    assert "601899" not in json.dumps(result, ensure_ascii=False)


def test_execution_check_maps_provider_benchmarks_without_changing_research_identity() -> None:
    cases = (
        ("cn", "510980", "000300", "sh000300", NOW),
        ("hk", "HK00700", "HSI", "r_hkHSI", NOW),
        (
            "us",
            "AAPL",
            "SPY",
            "SPY",
            datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
        ),
    )

    for market, symbol, benchmark_code, provider_symbol, market_now in cases:
        snapshot = _snapshot()
        snapshot["scope"] = [{"account_id": 1, "market": market, "symbol": symbol}]
        snapshot["positions"] = [
            {
                "account_id": 1,
                "market": market,
                "symbol": symbol,
                "last_price": 1.0,
                "price_available": True,
            }
        ]
        snapshot["instruments"] = [
            {
                "market": market,
                "symbol": symbol,
                "name": symbol,
                "instrument_type": "equity",
                "verification_status": "verified",
            }
        ]
        snapshot["benchmarks"] = [
            {"market": market, "code": benchmark_code, "price": 4000.0}
        ]
        calls: list[str] = []

        def quotes(code: str):
            calls.append(code)
            if code == symbol:
                quote = _quote(symbol, 1.0)
                quote["provider_timestamp"] = (
                    market_now - timedelta(minutes=1)
                ).isoformat()
                return quote
            if code == provider_symbol:
                quote = _quote(provider_symbol, 4000.0)
                quote["provider_timestamp"] = (
                    market_now - timedelta(minutes=1)
                ).isoformat()
                return quote
            return None

        result = PortfolioResearchExecutionService(
            quote_loader=quotes,
            now=lambda: market_now,
        ).check(snapshot)

        assert calls == [symbol, provider_symbol]
        row = result["items"][0]
        assert row["status"] == "ready", (market, row["blockers"])
        assert row["reference_evidence"]["benchmark_code"] == benchmark_code
        assert row["current_evidence"]["benchmark_code"] == benchmark_code
        assert "benchmark_execution_quote_identity_mismatch" not in row["blockers"]


def test_default_execution_quote_loader_skips_unrelated_supplement_fields(monkeypatch) -> None:
    calls: list[tuple[str, bool, bool]] = []

    class Manager:
        def get_realtime_quote(
            self,
            code: str,
            *,
            log_final_failure: bool,
            supplement: bool,
            preserve_provider_symbol: bool,
        ):
            calls.append((code, log_final_failure, supplement, preserve_provider_symbol))
            return _quote(code, 4000.0 if code == "sh000300" else 1.0)

    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.DataFetcherManager",
        Manager,
    )

    result = PortfolioResearchExecutionService(now=lambda: NOW).check(_snapshot())

    assert result["status"] == "ready"
    assert calls == [
        ("510980", False, False, True),
        ("sh000300", False, False, True),
    ]


def test_execution_check_marks_only_changed_row_for_reconfirmation() -> None:
    values = {
        "510980": {
            **_quote("510980", 1.05),
            "vwap": 1.03,
        },
        "sh000300": _quote("sh000300", 4000.0),
    }

    result = PortfolioResearchExecutionService(
        quote_loader=values.get,
        now=lambda: NOW,
    ).check(_snapshot())

    row = result["items"][0]
    assert result["requires_reconfirmation"] is True
    assert row["requires_reconfirmation"] is True
    assert row["changed_fields"] == ["price"]
    assert row["current_evidence"]["spread_bps"] is not None
    assert "quantity" not in json.dumps(result)
    assert "percentage" not in json.dumps(result)


def test_execution_check_fails_closed_per_row_when_quote_is_missing() -> None:
    result = PortfolioResearchExecutionService(
        quote_loader=lambda code: (
            _quote("sh000300", 4000.0)
            if code == "sh000300"
            else None
        )
        , now=lambda: NOW
    ).check(_snapshot())

    row = result["items"][0]
    assert result["status"] == "partial"
    assert row["status"] == "insufficient"
    assert row["blockers"] == ["execution_quote_unavailable"]
    assert row["requires_reconfirmation"] is True


def test_execution_check_rejects_missing_or_stale_quote_timestamp_per_row() -> None:
    quotes = {
        "510980": {**_quote("510980", 1.0), "provider_timestamp": None},
        "sh000300": {**_quote("sh000300", 4000.0), "provider_timestamp": "2026-08-06T05:00:00Z"},
    }

    result = PortfolioResearchExecutionService(
        quote_loader=quotes.get,
        now=lambda: NOW,
    ).check(_snapshot())

    blockers = result["items"][0]["blockers"]
    assert "execution_quote_timestamp_missing" in blockers
    assert "benchmark_execution_quote_stale" in blockers


def test_execution_check_derives_open_status_when_provider_omits_it() -> None:
    quotes = {
        "510980": {**_quote("510980", 1.0), "trading_status": None},
        "sh000300": {**_quote("sh000300", 4000.0), "trading_status": None},
    }

    result = PortfolioResearchExecutionService(
        quote_loader=quotes.get,
        now=lambda: NOW,
    ).check(_snapshot())

    row = result["items"][0]
    assert row["status"] == "ready"
    assert row["current_evidence"]["trading_status"] == "open"
    assert row["blockers"] == []


def test_execution_check_rejects_quote_outside_regular_session() -> None:
    closed_at = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
    quotes = {
        "510980": {
            **_quote("510980", 1.0),
            "provider_timestamp": "2026-08-06T08:29:00Z",
        },
        "sh000300": {
            **_quote("sh000300", 4000.0),
            "provider_timestamp": "2026-08-06T08:29:00Z",
        },
    }

    result = PortfolioResearchExecutionService(
        quote_loader=quotes.get,
        now=lambda: closed_at,
    ).check(_snapshot())

    row = result["items"][0]
    assert row["status"] == "insufficient"
    assert row["current_evidence"]["trading_status"] == "closed"
    assert "execution_trading_unavailable" in row["blockers"]
    assert "benchmark_execution_trading_unavailable" in row["blockers"]


def test_execution_check_compares_each_quote_with_post_fetch_clock() -> None:
    times = iter(
        [
            datetime(2026, 8, 6, 6, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 6, 30, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 6, 30, 4, tzinfo=timezone.utc),
        ]
    )
    values = {
        "510980": {
            **_quote("510980", 1.0),
            "provider_timestamp": "2026-08-06T06:30:01Z",
        },
        "sh000300": {
            **_quote("sh000300", 4000.0),
            "provider_timestamp": "2026-08-06T06:30:03Z",
        },
    }

    result = PortfolioResearchExecutionService(
        quote_loader=values.get,
        now=lambda: next(times),
    ).check(_snapshot())

    assert result["status"] == "ready"
    assert result["checked_at"] == "2026-08-06T06:30:04+00:00"
    assert result["items"][0]["blockers"] == []


def test_execution_check_refreshes_complete_qdii_dynamic_evidence() -> None:
    snapshot = _product_snapshot(
        symbol="513870",
        market="cn",
        instrument_type="qdii",
        instrument_fields={
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "CNY",
        },
        frozen_components={
            "nav_iopv": _component(nav=1.08, iopv=1.09),
            "premium_discount": _component(premium_discount_pct=0.2),
            "underlying_fx": _component(pair="USD/CNY", rate=7.18),
            "spread": _component(spread_bps=8.0),
            "execution_reference_observation": _qdii_execution_reference_observation(),
        },
    )
    calls: list[str] = []
    values = {
        "513870": _quote("513870", 1.12, pre_close=1.10),
        "sh000300": _quote("sh000300", 4000.0),
    }

    def quotes(symbol: str) -> dict | None:
        calls.append(symbol)
        return values.get(symbol)

    result = PortfolioResearchExecutionService(
        quote_loader=quotes,
        qdii_reference_loader=lambda **_: {
            "reference_type": "iopv",
            "reference_value": 1.10,
            "source": "verified-iopv-fixture",
            "provider_timestamp": "2026-08-06T06:28:40Z",
        },
        fx_quote_loader=lambda **_: {
            "pair": "USD/CNY",
            "rate": 7.20,
            "source": "verified-fx-fixture",
            "provider_timestamp": "2026-08-06T06:29:10Z",
        },
        now=lambda: NOW,
    ).check(snapshot)

    row = result["items"][0]
    dynamic = row["product_execution_evidence"]
    assert row["status"] == "ready", row["blockers"]
    assert calls == ["513870", "sh000300"]
    assert dynamic["reference_value"]["reference_type"] == "iopv"
    assert dynamic["reference_value"]["provider_timestamp"] == "2026-08-06T06:28:40+00:00"
    assert dynamic["market_price"]["provider_timestamp"] == "2026-08-06T06:29:00+00:00"
    assert dynamic["premium_discount"]["premium_discount_pct"] == 1.818182
    assert dynamic["fx"]["pair"] == "USD/CNY"
    assert dynamic["spread"]["spread_bps"] is not None
    assert dynamic["vwap"]["value"] == 1.118
    assert dynamic["tracking"]["tracking_difference_pct"] == -0.03367
    assert dynamic["tracking"]["formula"] == "product_return-reference_return"
    assert dynamic["tracking"]["inputs"]["baseline"]["reference_value"]["source"] == (
        "baseline-iopv-fixture"
    )
    assert dynamic["tracking"]["inputs"]["current"]["reference_value"]["source"] == (
        "verified-iopv-fixture"
    )
    assert dynamic["tracking"]["fx_return_pct"] == 0.278552


def test_execution_check_rejects_close_based_qdii_tracking_without_frozen_observation() -> None:
    snapshot = _product_snapshot(
        symbol="513870",
        market="cn",
        instrument_type="qdii",
        instrument_fields={
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "CNY",
        },
        frozen_components={
            "nav_iopv": _component(nav=1.08, iopv=1.09),
            "premium_discount": _component(premium_discount_pct=0.2),
            "underlying_fx": _component(pair="USD/CNY", rate=7.18),
            "spread": _component(spread_bps=8.0),
            "tracking": _component(tracking_difference_pct=0.3),
        },
    )
    values = {
        "513870": _quote("513870", 1.12, pre_close=1.10),
        "sh000300": _quote("sh000300", 4000.0),
    }

    row = PortfolioResearchExecutionService(
        quote_loader=values.get,
        qdii_reference_loader=lambda **_: {
            "reference_type": "iopv",
            "reference_value": 1.10,
            "source": "verified-iopv-fixture",
            "provider_timestamp": "2026-08-06T06:28:40Z",
        },
        fx_quote_loader=lambda **_: {
            "pair": "USD/CNY",
            "rate": 7.20,
            "source": "verified-fx-fixture",
            "provider_timestamp": "2026-08-06T06:29:10Z",
        },
        now=lambda: NOW,
    ).check(snapshot)["items"][0]

    assert row["status"] == "insufficient"
    assert "qdii_execution_reference_observation_missing" in row["blockers"]
    assert row["product_execution_evidence"]["tracking"] is None


def test_qdii_dynamic_inputs_use_post_fetch_clock() -> None:
    snapshot = _product_snapshot(
        symbol="513870",
        market="cn",
        instrument_type="qdii",
        instrument_fields={
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "CNY",
        },
        frozen_components={
            "nav_iopv": _component(nav=1.08, iopv=1.09),
            "premium_discount": _component(premium_discount_pct=0.2),
            "underlying_fx": _component(pair="USD/CNY", rate=7.18),
            "spread": _component(spread_bps=8.0),
            "execution_reference_observation": _qdii_execution_reference_observation(),
        },
    )
    times = iter(
        datetime(2026, 8, 6, 6, 30, second, tzinfo=timezone.utc)
        for second in (0, 2, 4, 6, 8, 9)
    )
    values = {
        "513870": _quote("513870", 1.12, pre_close=1.10),
        "sh000300": _quote("sh000300", 4000.0),
    }

    result = PortfolioResearchExecutionService(
        quote_loader=values.get,
        qdii_reference_loader=lambda **_: {
            "reference_type": "iopv",
            "reference_value": 1.10,
            "source": "verified-iopv-fixture",
            "provider_timestamp": "2026-08-06T06:30:05Z",
        },
        fx_quote_loader=lambda **_: {
            "pair": "USD/CNY",
            "rate": 7.20,
            "source": "verified-fx-fixture",
            "provider_timestamp": "2026-08-06T06:30:07Z",
        },
        now=lambda: next(times),
    ).check(snapshot)

    assert result["checked_at"] == "2026-08-06T06:30:09+00:00"
    assert result["items"][0]["status"] == "ready", result["items"][0]["blockers"]


def test_execution_check_uses_default_sse_iopv_provider(monkeypatch) -> None:
    snapshot = _product_snapshot(
        symbol="513870",
        market="cn",
        instrument_type="qdii",
        instrument_fields={
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "CNY",
        },
        frozen_components={
            "nav_iopv": _component(nav=1.08, iopv=1.09),
            "premium_discount": _component(premium_discount_pct=0.2),
            "underlying_fx": _component(pair="USD/CNY", rate=7.18),
            "spread": _component(spread_bps=8.0),
            "execution_reference_observation": _qdii_execution_reference_observation(),
        },
    )
    reference_calls: list[str] = []

    def reference(_self, symbol: str):
        reference_calls.append(symbol)
        return {
            "reference_type": "iopv",
            "reference_value": 1.10,
            "source": "sse-yunhq",
            "provider_timestamp": "2026-08-06T06:29:00+00:00",
        }

    monkeypatch.setattr(AkshareFetcher, "get_sse_etf_iopv", reference)
    values = {
        "513870": _quote("513870", 1.12, pre_close=1.10),
        "sh000300": _quote("sh000300", 4000.0),
    }

    row = PortfolioResearchExecutionService(
        quote_loader=values.get,
        fx_quote_loader=lambda **_: {
            "pair": "USD/CNY",
            "rate": 7.20,
            "source": "verified-fx-fixture",
            "provider_timestamp": "2026-08-06T06:29:00Z",
        },
        now=lambda: NOW,
    ).check(snapshot)["items"][0]

    assert reference_calls == ["513870"]
    assert row["status"] == "ready", row["blockers"]


def test_default_qdii_fx_loader_prefers_tencent_provider_timestamp(monkeypatch) -> None:
    expected = object()
    monkeypatch.setattr(
        AkshareFetcher,
        "get_realtime_fx_quote",
        lambda _self, from_currency, to_currency: expected,
        raising=False,
    )
    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.YfinanceFetcher.get_realtime_fx_quote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Yahoo fallback should not run when Tencent FX is available")
        ),
    )

    result = PortfolioResearchExecutionService._fetch_fx_quote(
        from_currency="USD",
        to_currency="CNY",
    )

    assert result is expected


def test_execution_check_refreshes_complete_daily_reset_dynamic_evidence() -> None:
    snapshot = _product_snapshot(
        symbol="HK07709",
        market="hk",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "quote_currency": "HKD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://issuer.example/terms",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="kr",
                symbol="000660.KS",
                currency="KRW",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=1.8,
                underlying_return_pct=1.0,
                observed_leverage=1.8,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=False),
        },
    )
    values = {
        "HK07709": _quote("HK07709", 31.2, pre_close=30.0),
        "000660.KS": _quote(
            "000660.KS",
            204000.0,
            pre_close=200000.0,
            provider_timestamp="2026-08-06T06:28:30Z",
        ),
        "r_hkHSI": _quote("r_hkHSI", 25000.0),
    }

    result = PortfolioResearchExecutionService(
        quote_loader=values.get,
        now=lambda: NOW,
    ).check(snapshot)

    row = result["items"][0]
    dynamic = row["product_execution_evidence"]
    assert row["status"] == "ready", row["blockers"]
    assert dynamic["product"]["reference_price"] == 30.0
    assert dynamic["underlying"]["symbol"] == "000660.KS"
    assert dynamic["underlying"]["reference_price"] == 200000.0
    assert dynamic["timestamp_alignment_seconds"] == 30.0
    assert dynamic["product_return_pct"] == 4.0
    assert dynamic["underlying_return_pct"] == 2.0
    assert dynamic["observed_leverage"] == 2.0
    assert dynamic["spread_bps"] is not None
    assert dynamic["volume"] == 100000.0
    assert dynamic["vwap"] == 31.198


def test_execution_check_uses_nasdaq_execution_quotes_for_us_daily_reset(
    monkeypatch,
) -> None:
    market_now = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    snapshot = _product_snapshot(
        symbol="PTIR",
        market="us",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "PLTR",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "USD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://graniteshares.com/etfs/ptir/",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="us",
                symbol="PLTR",
                currency="USD",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=2.0,
                underlying_return_pct=1.0,
                observed_leverage=2.0,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=False),
        },
    )
    manager_calls: list[str] = []

    class Manager:
        def get_realtime_quote(self, code: str, **_kwargs):
            manager_calls.append(code)
            values = {
                "SPY": _quote(
                    "SPY",
                    600.0,
                    provider_timestamp="2026-08-06T14:59:00Z",
                ),
                "PLTR": _quote(
                    "PLTR",
                    102.0,
                    pre_close=100.0,
                    provider_timestamp="2026-08-06T14:59:20Z",
                ),
            }
            return values.get(code)

    execution_calls: list[str] = []

    def execution_quote(_self, symbol: str):
        execution_calls.append(symbol)
        return _quote(
            symbol,
            10.4,
            pre_close=10.0,
            provider_timestamp="2026-08-06T14:59:00Z",
        )

    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.DataFetcherManager",
        Manager,
    )
    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.YfinanceFetcher.get_realtime_us_execution_quote",
        execution_quote,
        raising=False,
    )
    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.YfinanceFetcher.get_realtime_execution_quote",
        lambda _self, _symbol: (_ for _ in ()).throw(
            AssertionError("Yahoo bid/ask must not be used as US execution evidence")
        ),
    )

    row = PortfolioResearchExecutionService(now=lambda: market_now).check(snapshot)[
        "items"
    ][0]

    assert execution_calls == ["PTIR", "PLTR"]
    assert manager_calls == ["SPY"]
    assert row["status"] == "ready", row["blockers"]


def test_execution_check_does_not_fallback_to_yahoo_for_us_daily_reset_liquidity(
    monkeypatch,
) -> None:
    market_now = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    snapshot = _product_snapshot(
        symbol="PTIR",
        market="us",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "PLTR",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "USD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://graniteshares.com/etfs/ptir/",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="us",
                symbol="PLTR",
                currency="USD",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=2.0,
                underlying_return_pct=1.0,
                observed_leverage=2.0,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=False),
        },
    )
    manager_calls: list[str] = []

    class Manager:
        def get_realtime_quote(self, code: str, **_kwargs):
            manager_calls.append(code)
            quote = _quote(
                code,
                {"PTIR": 19.79, "PLTR": 177.98, "SPY": 773.96}[code],
                pre_close={"PTIR": 18.49, "PLTR": 172.01, "SPY": 773.26}[code],
                provider_timestamp="2026-08-06T14:59:00Z",
            )
            quote["source"] = "yfinance"
            if code == "PTIR":
                quote["bid"] = 15.82
                quote["ask"] = 20.46
            return quote

    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.DataFetcherManager",
        Manager,
    )
    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.YfinanceFetcher.get_realtime_us_execution_quote",
        lambda _self, _symbol: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.YfinanceFetcher.get_realtime_execution_quote",
        lambda _self, _symbol: (_ for _ in ()).throw(
            AssertionError("Yahoo bid/ask must not be used as US execution evidence")
        ),
    )

    row = PortfolioResearchExecutionService(now=lambda: market_now).check(snapshot)[
        "items"
    ][0]

    assert row["status"] == "insufficient"
    assert "execution_quote_unavailable" in row["blockers"]
    assert manager_calls == ["SPY"]


def test_execution_check_preserves_field_level_us_liquidity_provenance() -> None:
    market_now = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    snapshot = _product_snapshot(
        symbol="PTIR",
        market="us",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "PLTR",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "USD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://graniteshares.com/etfs/ptir/",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="us",
                symbol="PLTR",
                currency="USD",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=2.0,
                underlying_return_pct=1.0,
                observed_leverage=2.0,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=False),
        },
    )

    def quotes(code: str):
        values = {
            "PTIR": {
                **_quote(
                    "PTIR",
                    10.4,
                    pre_close=10.0,
                    provider_timestamp="2026-08-06T14:59:00Z",
                ),
                "source": "nasdaq",
                "price_source": "nasdaq",
                "price_provider_timestamp": "2026-08-06T14:59:00Z",
                "bid_ask_source": "nasdaq",
                "bid_ask_provider_timestamp": "2026-08-06T14:59:00Z",
                "volume_source": "nasdaq",
                "volume_provider_timestamp": "2026-08-06T14:59:00Z",
                "vwap_source": "yfinance_1m_bars",
                "vwap_provider_timestamp": "2026-08-06T14:58:00Z",
                "vwap_method": "one_minute_close_volume_weighted",
            },
            "PLTR": {
                **_quote(
                    "PLTR",
                    102.0,
                    pre_close=100.0,
                    provider_timestamp="2026-08-06T14:59:20Z",
                ),
                "source": "nasdaq",
            },
            "SPY": _quote(
                "SPY",
                600.0,
                provider_timestamp="2026-08-06T14:59:00Z",
            ),
        }
        return values.get(code)

    row = PortfolioResearchExecutionService(
        quote_loader=quotes,
        now=lambda: market_now,
    ).check(snapshot)["items"][0]

    assert row["status"] == "ready", row["blockers"]
    assert row["current_evidence"]["spread_source"] == "nasdaq"
    assert row["current_evidence"]["spread_as_of"] == "2026-08-06T14:59:00+00:00"
    assert row["current_evidence"]["vwap_source"] == "yfinance_1m_bars"
    assert row["current_evidence"]["vwap_as_of"] == "2026-08-06T14:58:00+00:00"
    liquidity = row["product_execution_evidence"]["liquidity_evidence"]
    assert liquidity["spread"]["source"] == "nasdaq"
    assert liquidity["volume"]["source"] == "nasdaq"
    assert liquidity["vwap"] == {
        "value": row["product_execution_evidence"]["vwap"],
        "source": "yfinance_1m_bars",
        "provider_timestamp": "2026-08-06T14:58:00+00:00",
        "method": "one_minute_close_volume_weighted",
    }


def test_execution_check_rejects_misaligned_liquidity_component_timestamp() -> None:
    market_now = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    snapshot = _product_snapshot(
        symbol="PTIR",
        market="us",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "PLTR",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "USD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://graniteshares.com/etfs/ptir/",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="us",
                symbol="PLTR",
                currency="USD",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=2.0,
                underlying_return_pct=1.0,
                observed_leverage=2.0,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=False),
        },
    )

    def quotes(code: str):
        quote = _quote(
            code,
            10.4 if code == "PTIR" else (102.0 if code == "PLTR" else 600.0),
            pre_close=10.0 if code == "PTIR" else (100.0 if code == "PLTR" else 600.0),
            provider_timestamp="2026-08-06T14:59:00Z",
        )
        if code == "PTIR":
            quote.update(
                {
                    "source": "nasdaq",
                    "vwap_source": "yfinance_1m_bars",
                    "vwap_provider_timestamp": "2026-08-06T14:55:00Z",
                    "vwap_method": "one_minute_close_volume_weighted",
                }
            )
        return quote

    row = PortfolioResearchExecutionService(
        quote_loader=quotes,
        now=lambda: market_now,
    ).check(snapshot)["items"][0]

    assert row["status"] == "insufficient"
    assert "execution_vwap_timestamp_misaligned" in row["blockers"]


def test_execution_check_uses_naver_realtime_quote_for_kr_underlying(
    monkeypatch,
) -> None:
    market_now = datetime(2026, 8, 6, 6, 30, tzinfo=timezone.utc)
    snapshot = _product_snapshot(
        symbol="HK07709",
        market="hk",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "quote_currency": "HKD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://issuer.example/terms",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="kr",
                symbol="000660.KS",
                currency="KRW",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=1.8,
                underlying_return_pct=1.0,
                observed_leverage=1.8,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=False),
        },
    )
    manager_calls: list[str] = []

    class Manager:
        def get_realtime_quote(self, code: str, **_kwargs):
            manager_calls.append(code)
            values = {
                "HK07709": _quote(
                    "HK07709",
                    31.2,
                    pre_close=30.0,
                    provider_timestamp="2026-08-06T06:29:00Z",
                ),
                "r_hkHSI": _quote(
                    "r_hkHSI",
                    25000.0,
                    provider_timestamp="2026-08-06T06:29:10Z",
                ),
            }
            return values.get(code)

    kr_calls: list[str] = []

    def kr_quote(_self, symbol: str):
        kr_calls.append(symbol)
        return _quote(
            symbol,
            204000.0,
            pre_close=200000.0,
            provider_timestamp="2026-08-06T06:29:20Z",
        ) | {"source": "naver_finance"}

    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.DataFetcherManager",
        Manager,
    )
    monkeypatch.setattr(
        "src.services.portfolio_research_execution_service.YfinanceFetcher.get_realtime_kr_execution_quote",
        kr_quote,
        raising=False,
    )

    row = PortfolioResearchExecutionService(now=lambda: market_now).check(snapshot)[
        "items"
    ][0]

    assert kr_calls == ["000660.KS"]
    assert manager_calls == ["HK07709", "r_hkHSI"]
    assert row["status"] == "ready", row["blockers"]
    assert row["product_execution_evidence"]["underlying"]["source"] == "naver_finance"


def test_execution_check_rejects_fetched_at_and_misaligned_product_timestamps() -> None:
    snapshot = _product_snapshot(
        symbol="HK07709",
        market="hk",
        instrument_type="daily_leveraged_product",
        instrument_fields={
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "quote_currency": "HKD",
            "leverage_factor": 2.0,
            "daily_reset": True,
        },
        frozen_components={
            "official_terms": _component(
                terms_url="https://issuer.example/terms",
                daily_reset=True,
                leverage_factor=2.0,
            ),
            "underlying_same_cutoff": _component(
                completed_session=True,
                market="kr",
                symbol="000660.KS",
                currency="KRW",
            ),
            "completed_session_leverage": _component(
                leverage_factor=2.0,
                product_return_pct=1.8,
                underlying_return_pct=1.0,
                observed_leverage=1.8,
            ),
            "path_decay_rebalance": _component(
                path_dependency_disclosed=True,
                rebalance_frequency="daily",
            ),
            "liquidity": _component(spread_bps=12.0),
            "horizon_fit": _component(evaluated=True, fits_holding_period=True),
        },
    )
    product = _quote("HK07709", 31.2, pre_close=30.0)
    product["provider_timestamp"] = None
    product["fetched_at"] = "2026-08-06T06:29:50Z"
    values = {
        "HK07709": product,
        "000660.KS": _quote(
            "000660.KS",
            204000.0,
            pre_close=200000.0,
            provider_timestamp="2026-08-06T06:25:00Z",
        ),
        "r_hkHSI": _quote("r_hkHSI", 25000.0),
    }

    row = PortfolioResearchExecutionService(
        quote_loader=values.get,
        now=lambda: NOW,
    ).check(snapshot)["items"][0]

    assert row["status"] == "insufficient"
    assert "execution_quote_timestamp_missing" in row["blockers"]
    assert "daily_reset_execution_timestamp_missing" in row["blockers"]

    values["HK07709"] = _quote("HK07709", 31.2, pre_close=30.0)
    row = PortfolioResearchExecutionService(
        quote_loader=values.get,
        now=lambda: NOW,
    ).check(snapshot)["items"][0]
    assert row["status"] == "insufficient"
    assert "daily_reset_execution_timestamp_misaligned" in row["blockers"]


def test_execution_check_isolates_missing_dynamic_qdii_evidence_to_its_row() -> None:
    snapshot = _product_snapshot(
        symbol="513870",
        market="cn",
        instrument_type="qdii",
        instrument_fields={
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "quote_currency": "CNY",
        },
        frozen_components={
            "nav_iopv": _component(nav=1.08, iopv=1.09),
            "premium_discount": _component(premium_discount_pct=0.2),
            "underlying_fx": _component(pair="USD/CNY", rate=7.18),
            "spread": _component(spread_bps=8.0),
            "tracking": _component(tracking_difference_pct=0.3),
        },
    )
    snapshot["scope"].append(
        {"account_id": 1, "market": "cn", "symbol": "510980"}
    )
    snapshot["positions"].append(
        {
            "account_id": 1,
            "market": "cn",
            "symbol": "510980",
            "last_price": 1.0,
            "price_available": True,
        }
    )
    snapshot["instruments"].append(
        {
            "market": "cn",
            "symbol": "510980",
            "name": "广发中证光伏龙头30ETF",
            "instrument_type": "etf",
            "verification_status": "verified",
        }
    )
    values = {
        "513870": _quote("513870", 1.12, pre_close=1.10),
        "NDX": _quote("NDX", 20000.0, pre_close=19800.0),
        "510980": _quote("510980", 1.0),
        "sh000300": _quote("sh000300", 4000.0),
    }

    result = PortfolioResearchExecutionService(
        quote_loader=values.get,
        qdii_reference_loader=lambda **_: None,
        fx_quote_loader=lambda **_: None,
        now=lambda: NOW,
    ).check(snapshot)

    rows = {row["symbol"]: row for row in result["items"]}
    assert result["status"] == "partial"
    assert rows["513870"]["status"] == "insufficient"
    assert "qdii_execution_reference_value_missing" in rows["513870"]["blockers"]
    assert rows["510980"]["status"] == "ready"
    assert rows["510980"]["blockers"] == []
