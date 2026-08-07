from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone

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


def _quote(symbol: str, price: float) -> dict:
    return {
        "symbol": symbol,
        "price": price,
        "trading_status": "open",
        "bid": price - 0.001,
        "ask": price + 0.001,
        "volume": 100000,
        "volume_ratio": 1.2,
        "vwap": price - 0.002,
        "source": "fixture-quotes",
        "provider_timestamp": "2026-08-06T06:29:00Z",
    }


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


def test_execution_check_accepts_complete_qdii_and_daily_reset_fixtures() -> None:
    cases = (
        {
            "symbol": "513870",
            "instrument_type": "qdii",
            "instrument": {},
            "raw": {
                "nav_iopv": _component(nav=1.1, iopv=1.11),
                "premium_discount": _component(premium_discount_pct=0.2),
                "underlying_fx": _component(pair="USD/CNY", rate=7.2),
                "spread": _component(spread_bps=8.0),
                "tracking": _component(tracking_difference_pct=0.3),
            },
        },
        {
            "symbol": "HK07709",
            "instrument_type": "daily_leveraged_product",
            "instrument": {
                "underlying_symbol": "KWEB",
                "underlying_market": "us",
                "underlying_currency": "USD",
                "leverage_factor": 2.0,
                "daily_reset": True,
            },
            "raw": {
                "official_terms": _component(
                    terms_url="https://issuer.example/terms",
                    daily_reset=True,
                    leverage_factor=2.0,
                ),
                "underlying_same_cutoff": _component(
                    completed_session=True,
                    market="us",
                    symbol="KWEB",
                    currency="USD",
                ),
                "intraday_leverage": _component(
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
        },
    )

    for case in cases:
        snapshot = deepcopy(_snapshot())
        symbol = case["symbol"]
        snapshot["scope"][0]["symbol"] = symbol
        snapshot["positions"][0]["symbol"] = symbol
        snapshot["instruments"][0].update(
            {
                "symbol": symbol,
                "instrument_type": case["instrument_type"],
                **case["instrument"],
            }
        )
        instrument = snapshot["instruments"][0]
        instrument["product_evidence"] = product_evidence_from_instrument(
            {
                **instrument,
                "product_evidence": {
                    "schema_version": "portfolio-product-evidence-v1",
                    "market": "cn" if symbol == "513870" else "cn",
                    "symbol": symbol,
                    "instrument_type": case["instrument_type"],
                    "evidence_cutoff": snapshot["cutoff"],
                    **case["raw"],
                },
            },
            cutoff=datetime.fromisoformat(snapshot["cutoff"].replace("Z", "+00:00")),
        )
        quotes = {
            symbol: _quote(symbol, 1.0),
            "sh000300": _quote("sh000300", 4000.0),
        }

        result = PortfolioResearchExecutionService(
            quote_loader=quotes.get,
            now=lambda: NOW,
        ).check(snapshot)

        row = result["items"][0]
        assert row["status"] == "ready", (case["instrument_type"], row["blockers"])
        assert row["blockers"] == []
