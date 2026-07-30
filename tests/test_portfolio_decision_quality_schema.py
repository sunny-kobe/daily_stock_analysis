# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from src.schemas.portfolio_decision_quality import (
    ATTRIBUTION_CATEGORIES,
    ATTRIBUTION_STATUSES,
    INCREMENTAL_ACTIONS,
    POSITION_ACTIONS,
    QUALITY_HORIZONS,
    is_materially_evaluable,
    material_event_fingerprint,
    normalize_portfolio_decision,
)


def _complete_decision(**overrides):
    payload = {
        "account_id": 2,
        "market": "us",
        "stock_code": "AAPL",
        "instrument_type": "equity",
        "frozen_snapshot_hash": "a" * 64,
        "position_action": "hold",
        "incremental_action": "wait",
        "confidence_by_horizon": {"5d": 0.55, "20d": 0.68, "60d": 0.61},
        "supporting_evidence": ["cash flow remains positive"],
        "opposing_evidence": ["valuation remains elevated"],
        "invalidation": "verified guidance is withdrawn",
        "watch_conditions": ["valuation enters the approved range"],
        "next_review": "next earnings release",
        "benchmark": {"market": "us", "code": "SPY", "type": "market_index"},
        "evidence_cutoff": "2026-07-25T08:00:00Z",
        "evidence_version": "portfolio-research-snapshot-v1",
        "decision_profile": "balanced",
        "decision_version": "portfolio-decision-v1",
        "strategy_version": "champion-v1",
    }
    payload.update(overrides)
    return payload


def test_public_contract_constants_are_stable() -> None:
    assert POSITION_ACTIONS == frozenset({"hold", "reduce", "exit"})
    assert INCREMENTAL_ACTIONS == frozenset({"add_in_batches", "wait", "no_add"})
    assert QUALITY_HORIZONS == {"5d": 5, "20d": 20, "60d": 60}
    assert ATTRIBUTION_CATEGORIES == frozenset(
        {
            "fact_error",
            "evidence_error",
            "thesis_error",
            "valuation_error",
            "timing_error",
            "risk_error",
            "execution_error",
            "unattributed",
        }
    )
    assert ATTRIBUTION_STATUSES == frozenset({"proposed", "confirmed", "rejected"})


def test_normalizes_independent_action_axes() -> None:
    decision = normalize_portfolio_decision(
        {
            "position_action": "hold",
            "incremental_action": "add_in_batches",
            "confidence_by_horizon": {"5d": 0.55, "20d": 0.68, "60d": 0.61},
        }
    )

    assert decision["position_action"] == "hold"
    assert decision["incremental_action"] == "add_in_batches"
    assert decision["confidence_by_horizon"] == {"5d": 0.55, "20d": 0.68, "60d": 0.61}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_action", "buy"),
        ("position_action", "HOLD"),
        ("incremental_action", "add"),
        ("incremental_action", "WAIT"),
    ],
)
def test_rejects_invalid_action_values(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        normalize_portfolio_decision({field: value})


@pytest.mark.parametrize(
    "confidence_by_horizon",
    [
        {"5d": -0.01},
        {"20d": 1.01},
        {"60d": True},
        {"10d": 0.5},
        [0.5, 0.6, 0.7],
    ],
)
def test_rejects_invalid_confidence_contract(confidence_by_horizon) -> None:
    with pytest.raises((TypeError, ValueError), match="confidence_by_horizon"):
        normalize_portfolio_decision({"confidence_by_horizon": confidence_by_horizon})


def test_missing_material_evidence_returns_explicit_blockers() -> None:
    normalized = normalize_portfolio_decision(
        {
            "position_action": "hold",
            "incremental_action": "wait",
            "confidence_by_horizon": {"5d": 0.5},
        }
    )

    assert normalized["benchmark"] is None
    assert normalized["frozen_snapshot_hash"] is None
    assert is_materially_evaluable(normalized) == [
        "account_id_missing",
        "instrument_identity_missing",
        "instrument_type_missing",
        "frozen_snapshot_hash_missing",
        "benchmark_identity_missing",
        "evidence_cutoff_missing",
        "evidence_version_missing",
        "decision_profile_missing",
        "decision_version_missing",
        "strategy_version_missing",
        "invalidation_missing",
        "next_review_missing",
        "confidence_horizons_incomplete",
    ]


def test_complete_material_decision_has_no_blockers() -> None:
    assert is_materially_evaluable(_complete_decision()) == []


def test_invalid_snapshot_hash_is_not_treated_as_complete_evidence() -> None:
    assert is_materially_evaluable(_complete_decision(frozen_snapshot_hash="not-a-sha256")) == [
        "frozen_snapshot_hash_invalid"
    ]


def test_material_fingerprint_is_order_independent_for_set_like_evidence() -> None:
    left = _complete_decision(
        watch_conditions=["b", "a"],
        supporting_evidence=["support-b", "support-a"],
        opposing_evidence=["opp-b", "opp-a"],
    )
    right = _complete_decision(
        opposing_evidence=["opp-a", "opp-b"],
        supporting_evidence=["support-a", "support-b"],
        watch_conditions=["a", "b"],
    )

    assert material_event_fingerprint(left) == material_event_fingerprint(right)


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("position_action", "reduce"),
        ("incremental_action", "no_add"),
        ("instrument_type", "daily_leveraged_product"),
        ("invalidation", "verified thesis evidence is invalidated"),
        ("watch_conditions", ["new observable trigger"]),
        ("benchmark", {"market": "us", "code": "QQQ", "type": "market_index"}),
        ("evidence_version", "portfolio-research-snapshot-v2"),
        ("decision_profile", "conservative"),
        ("decision_version", "portfolio-decision-v2"),
    ],
)
def test_material_contract_changes_create_distinct_fingerprints(field: str, changed_value) -> None:
    baseline = _complete_decision()
    changed = _complete_decision(**{field: changed_value})

    assert material_event_fingerprint(baseline) != material_event_fingerprint(changed)


def test_non_material_display_refresh_does_not_change_fingerprint() -> None:
    baseline = _complete_decision(display_refreshed_at="2026-07-25T09:00:00Z")
    refreshed = _complete_decision(display_refreshed_at="2026-07-25T10:00:00Z")

    assert material_event_fingerprint(baseline) == material_event_fingerprint(refreshed)


def test_snapshot_and_cutoff_refresh_alone_do_not_change_sample_identity() -> None:
    baseline = _complete_decision(
        frozen_snapshot_hash="a" * 64,
        evidence_cutoff="2026-07-25T08:00:00Z",
    )
    refreshed = _complete_decision(
        frozen_snapshot_hash="b" * 64,
        evidence_cutoff="2026-07-26T08:00:00Z",
    )

    assert material_event_fingerprint(baseline) == material_event_fingerprint(refreshed)


def test_ordered_trigger_plan_remains_order_sensitive() -> None:
    first = _complete_decision(trigger_contract={"steps": ["confirm", "review"]})
    second = _complete_decision(trigger_contract={"steps": ["review", "confirm"]})

    assert material_event_fingerprint(first) != material_event_fingerprint(second)
