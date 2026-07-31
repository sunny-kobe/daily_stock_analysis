# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import importlib.util


def _gate():
    module_name = "src.services.portfolio_decision_gate"
    assert importlib.util.find_spec(module_name) is not None, "portfolio decision gate module is required"
    return importlib.import_module(module_name).PortfolioDecisionGate()


def _evidence(**overrides):
    evidence = {
        "instrument": {
            "symbol": "AAPL",
            "market": "us",
            "quote_currency": "USD",
            "instrument_type": "equity",
            "trade_lot_size": 1,
            "verification_status": "verified",
        },
        "price": {"available": True, "stale": False},
        "fx": {"required": False, "available": True, "stale": False},
        "risk_policy": {"max_single_position_pct": 20},
        "premium": {"required": False, "available": True, "stale": False},
        "trade": {"quantity": 1, "lot_size": 1},
        "news": {"event_dependent": False, "primary_evidence_available": False},
    }
    evidence.update(overrides)
    return evidence


def _assert_blocked(evidence, code: str):
    result = _gate().evaluate(action="buy", evidence=evidence)
    assert result["raw_action"] == "buy"
    assert result["final_action"] == "alert"
    assert code in result["hard_blockers"]
    assert result["completeness"] == "INSUFFICIENT_EVIDENCE"


def test_gate_blocks_unverified_identity() -> None:
    instrument = {**_evidence()["instrument"], "verification_status": "provisional"}
    _assert_blocked(_evidence(instrument=instrument), "instrument_identity_unverified")


def test_gate_blocks_missing_or_stale_decision_price() -> None:
    _assert_blocked(
        _evidence(price={"available": False, "stale": False}),
        "decision_price_missing",
    )
    _assert_blocked(
        _evidence(price={"available": True, "stale": True}),
        "decision_price_stale",
    )


def test_gate_blocks_missing_fx_for_portfolio_conversion() -> None:
    _assert_blocked(
        _evidence(fx={"required": True, "available": False, "stale": False}),
        "fx_rate_missing",
    )


def test_gate_blocks_absent_portfolio_risk_policy() -> None:
    _assert_blocked(_evidence(risk_policy=None), "portfolio_risk_policy_missing")


def test_gate_exposes_unevaluated_risk_budget_without_inventing_a_breach() -> None:
    result = _gate().evaluate(
        action="buy",
        evidence=_evidence(risk_budget={"evaluated": False, "breaches": []}),
    )

    assert result["final_action"] == "buy"
    assert result["risk_budget_evaluated"] is False


def test_gate_blocks_qdii_without_fresh_premium_evidence() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "qdii",
        "requires_premium_check": True,
    }
    _assert_blocked(
        _evidence(
            instrument=instrument,
            premium={"required": True, "available": False, "stale": False},
        ),
        "nav_premium_missing",
    )


def test_gate_blocks_adr_without_underlying_conversion_data() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "adr_ads",
        "requires_premium_check": True,
        "underlying_symbol": "2330.TW",
        "underlying_market": "tw",
        "underlying_currency": "TWD",
        "conversion_ratio": None,
    }
    _assert_blocked(_evidence(instrument=instrument), "adr_conversion_ratio_missing")


def test_gate_blocks_daily_reset_product_without_reset_and_underlying_terms() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "daily_leveraged_product",
        "underlying_symbol": None,
        "underlying_market": None,
        "underlying_currency": None,
        "leverage_factor": None,
        "daily_reset": False,
    }
    result = _gate().evaluate(action="sell", evidence=_evidence(instrument=instrument))
    assert result["final_action"] == "alert"
    assert "leveraged_underlying_missing" in result["hard_blockers"]
    assert "daily_reset_terms_missing" in result["hard_blockers"]


def test_gate_blocks_trade_quantity_not_aligned_to_verified_lot() -> None:
    _assert_blocked(
        _evidence(trade={"quantity": 50, "lot_size": 100}),
        "trade_quantity_not_lot_aligned",
    )


def test_missing_news_does_not_block_quiet_hold_but_blocks_event_action() -> None:
    quiet_hold = _gate().evaluate(action="hold", evidence=_evidence())
    assert quiet_hold["final_action"] == "hold"
    assert quiet_hold["hard_blockers"] == []

    event_news = {
        "event_dependent": True,
        "primary_evidence_available": False,
    }
    result = _gate().evaluate(action="buy", evidence=_evidence(news=event_news))
    assert result["final_action"] == "alert"
    assert "event_evidence_missing" in result["hard_blockers"]
    assert result["required_capability"] == "vibe_current_evidence"


def _portfolio_decision(**overrides):
    decision = {
        "position_action": "hold",
        "incremental_action": "wait",
        "confidence_by_horizon": {"5d": 0.5, "20d": 0.6, "60d": 0.55},
        "supporting_evidence": [],
        "opposing_evidence": [],
        "invalidation": "observable invalidation",
        "watch_conditions": ["observable trigger"],
        "next_review": "next event",
    }
    decision.update(overrides)
    return decision


def test_gate_derives_canonical_action_from_valid_two_axis_decision() -> None:
    payload = {
        "action": "buy",
        "metadata": {"portfolio_decision": _portfolio_decision(incremental_action="add_in_batches")},
    }

    gated = _gate().apply_to_payload(payload, evidence=_evidence(risk_budget={"evaluated": True}))

    assert gated["action"] == "add"
    assert gated["metadata"]["portfolio_decision"]["position_action"] == "hold"
    assert gated["metadata"]["portfolio_decision"]["incremental_action"] == "add_in_batches"
    assert gated["metadata"]["portfolio_decision"]["position_action_executable"] is True
    assert gated["metadata"]["portfolio_decision"]["incremental_action_executable"] is True


def test_gate_blocks_add_axis_to_wait_without_rewriting_hold_axis() -> None:
    payload = {
        "action": "buy",
        "metadata": {"portfolio_decision": _portfolio_decision(incremental_action="add_in_batches")},
    }
    stale_price = _evidence(
        price={"available": True, "stale": True},
        risk_budget={"evaluated": True},
    )

    gated = _gate().apply_to_payload(payload, evidence=stale_price)

    decision = gated["metadata"]["portfolio_decision"]
    assert gated["action"] == "hold"
    assert decision["position_action"] == "hold"
    assert decision["incremental_action"] == "wait"
    assert decision["position_action_executable"] is True
    assert decision["incremental_action_executable"] is False
    assert "decision_price_stale" in decision["incremental_action_blockers"]


def test_gate_keeps_blocked_exit_visible_but_non_executable() -> None:
    payload = {
        "action": "sell",
        "metadata": {"portfolio_decision": _portfolio_decision(position_action="exit")},
    }

    gated = _gate().apply_to_payload(
        payload,
        evidence=_evidence(price={"available": False, "stale": False}),
    )

    decision = gated["metadata"]["portfolio_decision"]
    assert gated["action"] == "alert"
    assert decision["position_action"] == "exit"
    assert decision["position_action_executable"] is False
    assert "decision_price_missing" in decision["position_action_blockers"]


def test_quiet_hold_does_not_invent_add_permission() -> None:
    payload = {
        "action": "hold",
        "metadata": {"portfolio_decision": _portfolio_decision()},
    }

    gated = _gate().apply_to_payload(payload, evidence=_evidence())

    decision = gated["metadata"]["portfolio_decision"]
    assert gated["action"] == "hold"
    assert decision["position_action"] == "hold"
    assert decision["incremental_action"] == "wait"
    assert decision["incremental_action_executable"] is False


def test_missing_two_axis_contract_blocks_legacy_actionable_signal() -> None:
    payload = {
        "action": "buy",
        "metadata": {
            "quality_context_status": "insufficient_evidence",
            "quality_context_unable_reasons": ["portfolio_decision_missing"],
        },
    }

    gated = _gate().apply_to_payload(payload, evidence=_evidence())

    assert gated["action"] == "alert"
    assert gated["metadata"]["quality_context_status"] == "insufficient_evidence"
    assert "portfolio_decision_missing" in gated["metadata"]["portfolio_gate"]["hard_blockers"]
