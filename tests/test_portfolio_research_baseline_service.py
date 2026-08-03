from __future__ import annotations

from collections import Counter
from unittest.mock import patch

import pytest

from src.services.portfolio_research_baseline_service import (
    PortfolioResearchBaselineService,
)


def _complete_evidence() -> dict:
    return {
        "status": "complete",
        "display_status": "已保存",
        "reference_status": "matched",
        "unable_reasons": [],
    }


def _snapshot() -> dict:
    return {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "a" * 64,
        "cutoff": "2026-07-29T02:00:00Z",
        "positions": [
            {
                "account_id": 1,
                "symbol": "AAPL",
                "market": "us",
                "currency": "USD",
                "quantity": 10.0,
                "last_price": 210.0,
                "price_available": True,
                "price_stale": False,
                "fx": {"required": False, "available": True, "stale": False},
            },
            {
                "account_id": 2,
                "symbol": "AAPL",
                "market": "us",
                "currency": "USD",
                "quantity": 5.0,
                "last_price": 210.0,
                "price_available": True,
                "price_stale": False,
                "fx": {"required": False, "available": True, "stale": False},
            },
            {
                "account_id": 1,
                "symbol": "513870",
                "market": "cn",
                "currency": "CNY",
                "quantity": 100.0,
                "last_price": 1.2,
                "price_available": True,
                "price_stale": False,
                "fx": {"required": False, "available": True, "stale": False},
            },
        ],
        "accounts": [
            {"account_id": 1, "base_currency": "CNY"},
            {"account_id": 2, "base_currency": "USD"},
        ],
        "instruments": [
            {
                "symbol": "AAPL",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1.0,
                "requires_premium_check": False,
                "verification_status": "verified",
            },
            {
                "symbol": "513870",
                "market": "cn",
                "quote_currency": "CNY",
                "instrument_type": "qdii",
                "trade_lot_size": 100.0,
                "requires_premium_check": True,
                "verification_status": "verified",
            },
        ],
        "risk_policy": {"max_single_position_pct": 30.0},
        "risk_budget": {"evaluated": True, "breaches": [], "evidence_blockers": []},
        "decision_signals": [],
        "hard_blockers": [],
    }


def test_baseline_covers_rows_deduplicates_market_data_and_never_calls_deep_analysis() -> None:
    calls = Counter()

    def names(keys):
        calls["names"] += 1
        assert keys == [("cn", "513870"), ("us", "AAPL")]
        return {("cn", "513870"): "纳指ETF富国", ("us", "AAPL"): "苹果"}

    def quotes(keys):
        calls["quotes"] += 1
        assert keys == [("cn", "513870"), ("us", "AAPL")]
        return {
            ("cn", "513870"): {"available": True, "price": 1.22, "source": "batch", "as_of": "2026-07-29T02:01:00Z"},
            ("us", "AAPL"): {"available": True, "price": 212.0, "source": "batch", "as_of": "2026-07-29T02:01:00Z"},
        }

    def histories(keys, cutoff):
        calls["histories"] += 1
        assert cutoff == "2026-07-29T02:00:00Z"
        assert keys == [("cn", "513870"), ("us", "AAPL")]
        return {
            key: {"available": True, "source": "db_cache", "bar_count": 90, "data": key}
            for key in keys
        }

    def trends(symbol, history):
        calls[("trend", symbol)] += 1
        return {"trend_status": "震荡整理", "signal_score": 50}

    def signals(keys):
        calls["signals"] += 1
        return [
            {
                "id": 11,
                "stock_code": "AAPL",
                "stock_name": "Apple",
                "market": "us",
                "status": "active",
                "reason": "No action-changing signal",
                "metadata": {
                    "quality_context_status": "complete",
                    "decision_evidence": _complete_evidence(),
                    "portfolio_decision": {
                        "position_action": "hold",
                        "incremental_action": "wait",
                        "position_action_blockers": [],
                        "incremental_action_blockers": [],
                    },
                },
            },
            {
                "id": 12,
                "stock_code": "513870",
                "stock_name": "纳指ETF富国",
                "market": "cn",
                "status": "active",
                "reason": "Premium evidence required",
                "metadata": {
                    "quality_context_status": "complete",
                    "decision_evidence": _complete_evidence(),
                    "portfolio_decision": {
                        "position_action": "hold",
                        "incremental_action": "wait",
                        "position_action_blockers": [],
                        "incremental_action_blockers": ["nav_premium_missing"],
                    },
                },
            },
        ]

    def forbidden_deep_analysis(*_args, **_kwargs):
        raise AssertionError("baseline must not call task queue, news, analyzer, or LLM")

    service = PortfolioResearchBaselineService(
        name_loader=names,
        quote_loader=quotes,
        history_loader=histories,
        signal_loader=signals,
        trend_loader=trends,
        deep_analysis=forbidden_deep_analysis,
    )

    result = service.build(_snapshot())

    assert result["snapshot_hash"] == "a" * 64
    assert result["market_data_cutoff"] == "2026-07-29T02:00:00Z"
    assert result["ledger_position_count"] == 3
    assert result["baseline_row_count"] == 3
    assert result["coverage_reconciled"] is True
    assert calls["names"] == calls["quotes"] == calls["histories"] == calls["signals"] == 1
    assert calls[("trend", "AAPL")] == 1
    assert calls[("trend", "513870")] == 1

    aapl_rows = [row for row in result["items"] if row["symbol"] == "AAPL"]
    assert len(aapl_rows) == 2
    assert {row["account_id"] for row in aapl_rows} == {1, 2}
    assert all(row["display_label"] == "苹果（AAPL）" for row in aapl_rows)
    assert all(row["selection_key"] == "us:AAPL" for row in aapl_rows)
    assert all(row["position_action"] == "hold" for row in aapl_rows)
    assert all(row["incremental_action"] == "wait" for row in aapl_rows)
    assert all(row["user_instruction"] == "hold" for row in aapl_rows)

    qdii = next(row for row in result["items"] if row["symbol"] == "513870")
    assert qdii["display_label"] == "纳指ETF富国（513870）"
    assert qdii["user_instruction"] == "insufficient"
    assert qdii["detail_recommended"] is True
    assert "nav_premium_missing" in qdii["exception_reasons"]
    assert result["suggested_deep_analysis"][0]["display_label"] == "纳指ETF富国（513870）"


def test_missing_name_and_signal_are_visible_without_inventing_an_add_decision() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = [snapshot["positions"][0]]
    snapshot["instruments"] = [snapshot["instruments"][0]]

    service = PortfolioResearchBaselineService(
        name_loader=lambda _keys: {},
        quote_loader=lambda _keys: {("us", "AAPL"): {"available": False, "source": "none"}},
        history_loader=lambda _keys, _cutoff: {("us", "AAPL"): {"available": False, "source": "none", "bar_count": 0}},
        signal_loader=lambda _keys: [],
        trend_loader=lambda _symbol, _history: None,
    )

    result = service.build(snapshot)
    row = result["items"][0]

    assert row["display_label"] == "名称待核验（AAPL）"
    assert row["position_action"] == "hold"
    assert row["incremental_action"] == "wait"
    assert row["user_instruction"] == "insufficient"
    assert row["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert row["detail_recommended"] is True
    assert set(row["exception_reasons"]) >= {
        "instrument_name_missing",
        "baseline_quote_missing",
        "baseline_history_missing",
        "active_decision_signal_missing",
    }


def test_evidence_blocked_candidates_are_recommended_beyond_soft_limit() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = [snapshot["positions"][0], snapshot["positions"][2]]
    snapshot["instruments"] = [snapshot["instruments"][0], snapshot["instruments"][1]]
    snapshot["instruments"][0].update(
        {
            "instrument_type": "daily_leveraged_product",
            "daily_reset": True,
        }
    )

    service = PortfolioResearchBaselineService(
        name_loader=lambda _keys: {
            ("us", "AAPL"): "苹果每日杠杆产品",
            ("cn", "513870"): "纳指ETF富国",
        },
        quote_loader=lambda keys: {
            key: {"available": True, "source": "snapshot"} for key in keys
        },
        history_loader=lambda keys, _cutoff: {
            key: {"available": True, "source": "db_cache", "bar_count": 90}
            for key in keys
        },
        signal_loader=lambda _keys: [],
        trend_loader=lambda _symbol, _history: {},
        max_recommended=1,
    )

    result = service.build(snapshot)

    assert len(result["suggested_deep_analysis"]) == 2
    assert all(row["detail_recommended"] for row in result["items"])
    assert all(candidate["recommended"] for candidate in result["suggested_deep_analysis"])


def test_repeated_symbol_uses_account_bound_active_signal() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = snapshot["positions"][:2]
    snapshot["instruments"] = snapshot["instruments"][:1]

    def signals(_keys):
        return [
            {
                "id": 21,
                "stock_code": "AAPL",
                "stock_name": "苹果",
                "market": "us",
                "metadata": {
                    "quality_context_status": "complete",
                    "decision_evidence": _complete_evidence(),
                    "portfolio_decision": {
                        "account_id": 1,
                        "position_action": "hold",
                        "incremental_action": "wait",
                    },
                },
            },
            {
                "id": 22,
                "stock_code": "AAPL",
                "stock_name": "苹果",
                "market": "us",
                "metadata": {
                    "quality_context_status": "complete",
                    "decision_evidence": _complete_evidence(),
                    "portfolio_decision": {
                        "account_id": 2,
                        "position_action": "reduce",
                        "incremental_action": "no_add",
                    },
                },
            },
        ]

    service = PortfolioResearchBaselineService(
        name_loader=lambda _keys: {("us", "AAPL"): "苹果"},
        quote_loader=lambda _keys: {("us", "AAPL"): {"available": True, "source": "batch"}},
        history_loader=lambda _keys, _cutoff: {
            ("us", "AAPL"): {"available": True, "source": "db_cache", "bar_count": 90, "data": "bars"}
        },
        signal_loader=signals,
        trend_loader=lambda _symbol, _history: {},
    )

    result = service.build(snapshot)
    rows = {row["account_id"]: row for row in result["items"]}

    assert rows[1]["current_signal_id"] == 21
    assert rows[1]["position_action"] == "hold"
    assert rows[2]["current_signal_id"] == 22
    assert rows[2]["position_action"] == "reduce"
    assert rows[2]["incremental_action"] == "no_add"
    assert rows[2]["user_instruction"] == "reduce"
    assert "action_changing_position_signal" in rows[2]["exception_reasons"]


@pytest.mark.parametrize(
    "blocker",
    [
        "position_adjustment_identity_unknown",
        "benchmark_adjustment_identity_unknown",
        "benchmark_adjustment_identity_mismatch",
    ],
)
def test_adjustment_blocker_stays_on_its_exact_position_row(blocker: str) -> None:
    snapshot = _snapshot()
    snapshot["hard_blockers"] = [
        {
            "code": blocker,
            "scope": "position",
            "account_id": 2,
            "market": "us",
            "symbol": "AAPL",
            "benchmark_symbol": "SPY",
        }
    ]
    service = PortfolioResearchBaselineService(
        name_loader=lambda keys: {key: key[1] for key in keys},
        quote_loader=lambda keys: {
            key: {"available": True, "source": "snapshot"} for key in keys
        },
        history_loader=lambda keys, _cutoff: {
            key: {"available": True, "source": "db_cache", "bar_count": 90}
            for key in keys
        },
        signal_loader=lambda _keys: [],
        trend_loader=lambda _symbol, _history: {},
    )

    rows = {
        (row["account_id"], row["market"], row["symbol"]): row
        for row in service.build(snapshot)["items"]
    }

    assert blocker not in rows[(1, "us", "AAPL")]["hard_blockers"]
    assert blocker in rows[(2, "us", "AAPL")]["hard_blockers"]
    assert blocker not in rows[(1, "cn", "513870")]["hard_blockers"]


def test_confirmed_portfolio_risk_breach_is_not_mislabeled_as_missing_evidence() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = [snapshot["positions"][0]]
    snapshot["instruments"] = [snapshot["instruments"][0]]
    snapshot["accounts"] = [{"account_id": 1, "base_currency": "USD"}]
    snapshot["risk_budget"] = {
        "evaluated": True,
        "evidence_blockers": [],
        "breaches": [
            {
                "code": "cash_buffer_below_minimum",
                "scope": "currency",
                "currency": "USD",
                "actual_pct": 0.0,
                "limit_pct": 10.0,
            }
        ],
        "scopes": [],
    }

    service = PortfolioResearchBaselineService(
        name_loader=lambda _keys: {("us", "AAPL"): "苹果"},
        quote_loader=lambda _keys: {("us", "AAPL"): {"available": True, "source": "batch"}},
        history_loader=lambda _keys, _cutoff: {
            ("us", "AAPL"): {"available": True, "source": "db_cache", "bar_count": 90, "data": "bars"}
        },
        signal_loader=lambda _keys: [
            {
                "id": 31,
                "stock_code": "AAPL",
                "stock_name": "苹果",
                "market": "us",
                "metadata": {
                    "quality_context_status": "complete",
                    "decision_evidence": _complete_evidence(),
                    "portfolio_decision": {
                        "account_id": 1,
                        "position_action": "hold",
                        "incremental_action": "wait",
                    },
                },
            }
        ],
        trend_loader=lambda _symbol, _history: {},
    )

    result = service.build(snapshot)
    row = result["items"][0]

    assert row["evidence_status"] == "baseline"
    assert row["hard_blockers"] == []
    assert row["risk_flags"] == []
    assert result["portfolio_risk_flags"][0]["code"] == "cash_buffer_below_minimum"


def test_default_quote_evidence_is_snapshot_only_and_starts_no_network_worker() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = [snapshot["positions"][0]]
    snapshot["positions"][0]["cache_updated_at"] = "2026-07-29T01:59:00"
    snapshot["instruments"] = [snapshot["instruments"][0]]

    service = PortfolioResearchBaselineService(
        name_loader=lambda _keys: {("us", "AAPL"): "苹果"},
        history_loader=lambda _keys, _cutoff: {
            ("us", "AAPL"): {"available": True, "source": "db_cache", "bar_count": 90, "data": "bars"}
        },
        signal_loader=lambda _keys: [],
        trend_loader=lambda _symbol, _history: {},
    )

    with patch(
        "src.services.portfolio_service.PortfolioService._prefetch_realtime_position_prices",
        side_effect=AssertionError("fast baseline must not start realtime network workers"),
    ):
        result = service.build(snapshot)

    quote = result["items"][0]["quote"]
    assert quote == {
        "available": True,
        "price": 210.0,
        "source": "portfolio_research_snapshot",
        "as_of": "2026-07-29T01:59:00",
        "stale": False,
    }


def test_default_baseline_uses_account_specific_frozen_signal_without_current_query() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = snapshot["positions"][:2]
    snapshot["instruments"] = snapshot["instruments"][:1]
    snapshot["decision_signals"] = [
        {
            "id": 41,
            "stock_code": "AAPL",
            "stock_name": "苹果",
            "market": "us",
            "status": "active",
            "reason": "Frozen account decision",
            "metadata": {
                "quality_context_status": "complete",
                "decision_evidence": _complete_evidence(),
                "portfolio_decision": {
                    "account_id": 2,
                    "position_action": "reduce",
                    "incremental_action": "no_add",
                },
            },
        }
    ]

    with patch.object(
        PortfolioResearchBaselineService,
        "_load_signals",
        side_effect=AssertionError("current DecisionSignals must not be queried after snapshot freeze"),
    ):
        service = PortfolioResearchBaselineService(
            name_loader=lambda _keys: {("us", "AAPL"): "苹果"},
            quote_loader=lambda keys: {
                key: {"available": True, "source": "snapshot"} for key in keys
            },
            history_loader=lambda keys, _cutoff: {
                key: {"available": True, "source": "db_cache", "bar_count": 90}
                for key in keys
            },
            trend_loader=lambda _symbol, _history: {},
        )
        result = service.build(snapshot)

    account_two = next(row for row in result["items"] if row["account_id"] == 2)
    assert account_two["current_signal_id"] == 41
    assert account_two["position_action"] == "reduce"
    assert account_two["incremental_action"] == "no_add"


def test_default_baseline_does_not_query_current_signal_absent_from_snapshot() -> None:
    snapshot = _snapshot()
    snapshot["positions"] = [snapshot["positions"][0]]
    snapshot["instruments"] = [snapshot["instruments"][0]]

    with patch.object(
        PortfolioResearchBaselineService,
        "_load_signals",
        return_value=[
            {
                "id": 42,
                "stock_code": "AAPL",
                "market": "us",
                "metadata": {
                    "quality_context_status": "complete",
                    "decision_evidence": _complete_evidence(),
                    "portfolio_decision": {
                        "account_id": 1,
                        "position_action": "reduce",
                        "incremental_action": "no_add",
                    },
                },
            }
        ],
    ) as current_loader:
        service = PortfolioResearchBaselineService(
            name_loader=lambda _keys: {("us", "AAPL"): "苹果"},
            quote_loader=lambda keys: {
                key: {"available": True, "source": "snapshot"} for key in keys
            },
            history_loader=lambda keys, _cutoff: {
                key: {"available": True, "source": "db_cache", "bar_count": 90}
                for key in keys
            },
            trend_loader=lambda _symbol, _history: {},
        )
        result = service.build(snapshot)

    assert current_loader.call_count == 0
    assert result["items"][0]["current_signal_id"] is None
    assert result["items"][0]["position_action"] == "hold"
    assert "active_decision_signal_missing" in result["items"][0]["hard_blockers"]


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {
            "status": "insufficient_evidence",
            "display_status": "资料不足",
            "reference_status": "matched",
            "unable_reasons": ["benchmark_bar_missing"],
        },
        {
            "status": "complete",
            "display_status": "已保存",
            "reference_status": "mismatch",
            "unable_reasons": ["decision_evidence_reference_mismatch"],
        },
    ],
    ids=["missing-sidecar", "incomplete-sidecar", "reference-mismatch"],
)
def test_baseline_rejects_active_signal_without_verified_complete_evidence(
    evidence,
) -> None:
    snapshot = _snapshot()
    snapshot["positions"] = [snapshot["positions"][0]]
    snapshot["instruments"] = [snapshot["instruments"][0]]
    metadata = {
        "quality_context_status": "complete",
        "portfolio_decision": {
            "account_id": 1,
            "position_action": "hold",
            "incremental_action": "wait",
        },
    }
    if evidence is not None:
        metadata["decision_evidence"] = evidence
    snapshot["decision_signals"] = [
        {
            "id": 51,
            "stock_code": "AAPL",
            "stock_name": "苹果",
            "market": "us",
            "status": "active",
            "reason": "Legacy decision",
            "metadata": metadata,
        }
    ]
    service = PortfolioResearchBaselineService(
        name_loader=lambda _keys: {("us", "AAPL"): "苹果"},
        quote_loader=lambda keys: {
            key: {"available": True, "source": "snapshot"} for key in keys
        },
        history_loader=lambda keys, _cutoff: {
            key: {"available": True, "source": "db_cache", "bar_count": 90}
            for key in keys
        },
        trend_loader=lambda _symbol, _history: {},
    )

    row = service.build(snapshot)["items"][0]

    assert row["current_signal_id"] == 51
    assert row["user_instruction"] == "insufficient"
    assert row["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert "decision_evidence_not_complete" in row["hard_blockers"]
