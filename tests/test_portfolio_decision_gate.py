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


def test_gate_requires_complete_qdii_product_evidence() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "qdii",
        "requires_premium_check": True,
    }
    result = _gate().evaluate(
        action="buy",
        evidence=_evidence(
            instrument=instrument,
            premium={"required": True, "available": True, "stale": False},
            product={
                "nav_iopv_available": True,
                "premium_discount_available": True,
                "underlying_fx_available": False,
                "spread_available": True,
                "tracking_available": True,
            },
        ),
    )

    assert result["final_action"] == "alert"
    assert "qdii_underlying_fx_missing" in result["hard_blockers"]


def test_gate_accepts_complete_qdii_product_evidence() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "qdii",
        "requires_premium_check": True,
    }
    result = _gate().evaluate(
        action="buy",
        evidence=_evidence(
            instrument=instrument,
            premium={"required": True, "available": True, "stale": False},
            product={
                "nav_iopv_available": True,
                "premium_discount_available": True,
                "underlying_fx_available": True,
                "spread_available": True,
                "tracking_available": True,
            },
        ),
    )

    assert result["final_action"] == "buy"
    assert result["hard_blockers"] == []


def test_snapshot_product_evidence_cannot_be_overridden_by_caller_context() -> None:
    incomplete = {
        "instrument_type": "qdii",
        "nav_iopv_available": True,
        "premium_discount_available": True,
        "underlying_fx_available": False,
        "spread_available": True,
        "tracking_available": True,
    }
    complete = {**incomplete, "underlying_fx_available": True}
    snapshot = {
        "accounts": [{"account_id": 1, "base_currency": "USD"}],
        "positions": [
            {
                "account_id": 1,
                "symbol": "AAPL",
                "market": "us",
                "price_available": True,
                "price_stale": False,
                "fx": {"required": False, "available": True, "stale": False},
            }
        ],
        "instruments": [
            {
                **_evidence()["instrument"],
                "instrument_type": "qdii",
                "requires_premium_check": True,
                "product_evidence": incomplete,
            }
        ],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True},
    }

    evidence = _gate().evidence_from_snapshot(
        payload={"stock_code": "AAPL", "market": "us", "metadata": {}},
        research_snapshot=snapshot,
        portfolio_context={
            "account_id": 1,
            "product_evidence": complete,
            "premium_evidence": {"available": True, "stale": False},
        },
    )
    result = _gate().evaluate(action="buy", evidence=evidence)

    assert evidence["product"] == incomplete
    assert "qdii_underlying_fx_missing" in result["hard_blockers"]


def test_snapshot_product_evidence_is_selected_for_the_bound_account() -> None:
    ready = {
        "instrument_type": "qdii",
        "status": "ready",
        "nav_iopv_available": True,
        "premium_discount_available": True,
        "underlying_fx_available": True,
        "spread_available": True,
        "tracking_available": True,
        "blockers": [],
    }
    insufficient = {
        **ready,
        "status": "insufficient",
        "underlying_fx_available": False,
        "blockers": ["qdii_underlying_fx_missing"],
    }
    snapshot = {
        "accounts": [{"account_id": 2, "base_currency": "USD"}],
        "positions": [
            {
                "account_id": 2,
                "symbol": "AAPL",
                "market": "us",
                "price_available": True,
                "price_stale": False,
                "fx": {"required": False, "available": True, "stale": False},
            }
        ],
        "instruments": [
            {
                **_evidence()["instrument"],
                "instrument_type": "qdii",
                "requires_premium_check": True,
                "product_evidence": ready,
                "product_evidence_by_account": {"1": ready, "2": insufficient},
            }
        ],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True},
    }

    evidence = _gate().evidence_from_snapshot(
        payload={"stock_code": "AAPL", "market": "us", "metadata": {}},
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )
    result = _gate().evaluate(action="buy", evidence=evidence)

    assert evidence["product"] == insufficient
    assert "qdii_underlying_fx_missing" in result["hard_blockers"]


def test_complete_frozen_qdii_evidence_supplies_same_cutoff_premium_gate() -> None:
    product_evidence = {
        "instrument_type": "qdii",
        "status": "ready",
        "nav_iopv_available": True,
        "premium_discount_available": True,
        "underlying_fx_available": True,
        "spread_available": True,
        "tracking_available": True,
        "blockers": [],
    }
    snapshot = {
        "accounts": [{"account_id": 1, "base_currency": "USD"}],
        "positions": [
            {
                "account_id": 1,
                "symbol": "AAPL",
                "market": "us",
                "price_available": True,
                "price_stale": False,
                "fx": {"required": False, "available": True, "stale": False},
            }
        ],
        "instruments": [
            {
                **_evidence()["instrument"],
                "instrument_type": "qdii",
                "requires_premium_check": True,
                "product_evidence": product_evidence,
            }
        ],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True},
    }

    evidence = _gate().evidence_from_snapshot(
        payload={"stock_code": "AAPL", "market": "us", "metadata": {}},
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
    )
    result = _gate().evaluate(action="buy", evidence=evidence)

    assert evidence["premium"] == {
        "required": True,
        "available": True,
        "stale": False,
    }
    assert result["hard_blockers"] == []


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


def test_gate_requires_daily_reset_execution_evidence_even_with_registry_terms() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "daily_leveraged_product",
        "underlying_symbol": "000660.KS",
        "underlying_market": "kr",
        "underlying_currency": "KRW",
        "leverage_factor": 2.0,
        "daily_reset": True,
    }
    result = _gate().evaluate(
        action="sell",
        evidence=_evidence(
            instrument=instrument,
            product={
                "official_terms_available": True,
                "underlying_identity_available": True,
                "underlying_same_cutoff_available": False,
                "intraday_leverage_available": False,
                "path_decay_rebalance_available": True,
                "liquidity_available": True,
                "horizon_fit_evaluated": True,
            },
        ),
    )

    assert result["final_action"] == "alert"
    assert "daily_reset_underlying_same_cutoff_missing" in result["hard_blockers"]
    assert "daily_reset_intraday_leverage_missing" in result["hard_blockers"]


def test_incompatible_daily_reset_horizon_blocks_add_but_not_risk_reduction() -> None:
    instrument = {
        **_evidence()["instrument"],
        "instrument_type": "daily_leveraged_product",
        "underlying_symbol": "000660.KS",
        "underlying_market": "kr",
        "underlying_currency": "KRW",
        "leverage_factor": 2.0,
        "daily_reset": True,
    }
    product = {
        "official_terms_available": True,
        "underlying_identity_available": True,
        "underlying_same_cutoff_available": True,
        "intraday_leverage_available": True,
        "path_decay_rebalance_available": True,
        "liquidity_available": True,
        "horizon_fit_evaluated": True,
        "components": {
            "horizon_fit": {
                "evaluated": True,
                "fits_holding_period": False,
            }
        },
    }
    evidence = _evidence(instrument=instrument, product=product)

    add_result = _gate().evaluate(action="add", evidence=evidence)
    reduce_result = _gate().evaluate(action="reduce", evidence=evidence)

    assert "daily_reset_holding_period_incompatible" in add_result["hard_blockers"]
    assert reduce_result["hard_blockers"] == []


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
    assert decision["incremental_action_executable"] is False
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

    assert gated["action"] == "hold"


def test_unevaluated_risk_budget_removes_sizing_from_signal_metadata() -> None:
    payload = {
        "action": "buy",
        "metadata": {
            "suggested_trade_quantity": 100,
            "suggested_trade_ratio": 0.1,
            "target_position_pct": 20,
            "portfolio_decision": _portfolio_decision(incremental_action="add_in_batches"),
        },
    }

    gated = _gate().apply_to_payload(
        payload,
        evidence=_evidence(risk_budget={"evaluated": False}),
    )

    metadata = gated["metadata"]
    assert metadata["sizing_allowed"] is False
    assert "suggested_trade_quantity" not in metadata
    assert "suggested_trade_ratio" not in metadata
    assert "target_position_pct" not in metadata


def test_unevaluated_risk_budget_removes_nested_quantities_ratios_and_batch_amounts() -> None:
    decision = _portfolio_decision(incremental_action="add_in_batches")
    decision.update(
        {
            "proposed_trade_quantity": 200,
            "suggested_position": "20%-30%",
            "target_position_pct": 15,
            "batch_plan": {
                "batch_amount": 5000,
                "allocation_ratio": 0.25,
                "trigger": "price confirmation",
            },
            "portfolio_impact": {
                "target_allocation_pct": 15,
                "remaining_quantity": 100,
                "summary": "cash remains adequate",
            },
        }
    )
    payload = {
        "action": "buy",
        "suggested_trade_quantity": 100,
        "metadata": {
            "portfolio_decision": decision,
            "portfolio_impact": {
                "position_size_pct": 15,
                "tranche_amount": 2500,
                "summary": "portfolio impact retained without sizing",
            },
        },
    }

    gated = _gate().apply_to_payload(
        payload,
        evidence=_evidence(risk_budget={"evaluated": False}),
    )

    serialized = str(gated)
    for forbidden in (
        "suggested_trade_quantity",
        "proposed_trade_quantity",
        "suggested_position",
        "remaining_quantity",
        "target_position_pct",
        "target_allocation_pct",
        "position_size_pct",
        "allocation_ratio",
        "batch_amount",
        "tranche_amount",
    ):
        assert forbidden not in serialized
    assert gated["metadata"]["portfolio_decision"]["batch_plan"]["trigger"] == "price confirmation"
    assert gated["metadata"]["portfolio_impact"]["summary"] == (
        "portfolio impact retained without sizing"
    )


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
