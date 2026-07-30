# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.config import Config
from src.schemas.portfolio_strategy_validation import freeze_strategy_manifest
from src.services.portfolio_shadow_validation_service import PortfolioShadowValidationService
from src.storage import DatabaseManager, DecisionSignalRecord


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'shadow-validation.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _strategies():
    champion = json.loads(
        Path("strategies/validation/champion-v1.json").read_text(encoding="utf-8")
    )
    challenger = deepcopy(champion)
    challenger.update(
        {
            "strategy_id": "portfolio-challenger",
            "strategy_version": "challenger-v1",
            "policy_hash": "d" * 64,
        }
    )
    return champion, challenger


def _frozen_input():
    return {
        "snapshot_hash": "a" * 64,
        "evidence_artifact_hash": "b" * 64,
        "cutoff": "2026-07-30T08:00:00Z",
        "account_id": 2,
        "market": "us",
        "symbol": "AAPL",
    }


def _decision(strategy, frozen_input, **overrides):
    frozen_strategy = freeze_strategy_manifest(strategy)
    from src.schemas.portfolio_strategy_validation import strategy_manifest_hash

    payload = {
        "input_hash": strategy_manifest_hash(frozen_input),
        "decision_status": "eligible",
        "position_action": "hold",
        "incremental_action": "wait",
        "blockers": [],
        "triggers": ["verified catalyst"],
        "invalidation": "thesis evidence is invalidated",
        "confidence": {"5d": 0.5, "20d": 0.6, "60d": 0.55},
        "strategy_version": frozen_strategy["strategy_version"],
        "strategy_manifest_hash": frozen_strategy["manifest_hash"],
    }
    payload.update(overrides)
    return payload


def _protocol(**overrides):
    payload = {
        "protocol_id": "shadow-protocol-001",
        "frozen_at": "2026-07-29T08:00:00Z",
        "shadow_start": "2026-07-30T08:00:00Z",
        "minimum_trading_days": 20,
        "minimum_independent_events": 5,
        "max_cost_delta_pct": 0.2,
        "max_drawdown_delta_pct": 1.0,
    }
    payload.update(overrides)
    return payload


def test_champion_and_challenger_must_use_identical_frozen_input(isolated_db) -> None:
    champion, challenger = _strategies()
    frozen_input = _frozen_input()
    champion_decision = _decision(champion, frozen_input)
    challenger_decision = _decision(challenger, frozen_input, input_hash="f" * 64)

    with pytest.raises(ValueError, match="shadow_input_hash_mismatch"):
        PortfolioShadowValidationService(db_manager=isolated_db).record_comparison(
            event_id="event-001",
            frozen_input=frozen_input,
            champion_strategy=champion,
            challenger_strategy=challenger,
            champion_decision=champion_decision,
            challenger_decision=challenger_decision,
            protocol=_protocol(),
        )


def test_shadow_outputs_are_immutable_and_do_not_create_production_signals(isolated_db) -> None:
    champion, challenger = _strategies()
    frozen_input = _frozen_input()
    service = PortfolioShadowValidationService(db_manager=isolated_db)
    with isolated_db.get_session() as session:
        before = session.scalar(select(func.count()).select_from(DecisionSignalRecord))

    first = service.record_comparison(
        event_id="event-001",
        frozen_input=frozen_input,
        champion_strategy=champion,
        challenger_strategy=challenger,
        champion_decision=_decision(champion, frozen_input),
        challenger_decision=_decision(challenger, frozen_input),
        protocol=_protocol(),
    )
    repeated = service.record_comparison(
        event_id="event-001",
        frozen_input=frozen_input,
        champion_strategy=champion,
        challenger_strategy=challenger,
        champion_decision=_decision(champion, frozen_input),
        challenger_decision=_decision(challenger, frozen_input),
        protocol=_protocol(),
    )

    assert repeated["comparison_id"] == first["comparison_id"]
    with pytest.raises(ValueError, match="immutable_shadow_comparison"):
        service.record_comparison(
            event_id="event-001",
            frozen_input=frozen_input,
            champion_strategy=champion,
            challenger_strategy=challenger,
            champion_decision=_decision(champion, frozen_input),
            challenger_decision=_decision(
                challenger, frozen_input, position_action="exit", incremental_action="no_add"
            ),
            protocol=_protocol(),
        )
    with isolated_db.get_session() as session:
        after = session.scalar(select(func.count()).select_from(DecisionSignalRecord))
    assert after == before


def test_abstention_is_explicit_and_order_fields_are_forbidden(isolated_db) -> None:
    champion, challenger = _strategies()
    frozen_input = _frozen_input()
    service = PortfolioShadowValidationService(db_manager=isolated_db)
    result = service.record_comparison(
        event_id="event-abstain",
        frozen_input=frozen_input,
        champion_strategy=champion,
        challenger_strategy=challenger,
        champion_decision=_decision(champion, frozen_input),
        challenger_decision=_decision(
            challenger,
            frozen_input,
            decision_status="abstain",
            blockers=["evidence_missing"],
        ),
        protocol=_protocol(),
    )
    assert result["challenger_decision"]["decision_status"] == "abstain"

    with pytest.raises(ValueError, match="order_field_forbidden"):
        service.record_comparison(
            event_id="event-order",
            frozen_input=frozen_input,
            champion_strategy=champion,
            challenger_strategy=challenger,
            champion_decision=_decision(champion, frozen_input),
            challenger_decision=_decision(challenger, frozen_input, order_quantity=10),
            protocol=_protocol(),
        )


def test_maturity_gate_requires_20_days_paired_outcomes_and_both_evidence_routes(
    isolated_db,
) -> None:
    service = PortfolioShadowValidationService(db_manager=isolated_db)
    immature = service.assess_maturity(
        protocol=_protocol(),
        trading_days=19,
        independent_event_count=5,
        mature_20d_paired_count=5,
        hard_gate_violations=[],
        cost_delta_pct=0.0,
        drawdown_delta_pct=0.0,
        historical_oos_positive=True,
        prospective_shadow_positive=True,
    )
    eligible = service.assess_maturity(
        protocol=_protocol(),
        trading_days=20,
        independent_event_count=5,
        mature_20d_paired_count=5,
        hard_gate_violations=[],
        cost_delta_pct=0.0,
        drawdown_delta_pct=0.0,
        historical_oos_positive=True,
        prospective_shadow_positive=True,
    )

    assert immature["decision"] == "CONTINUE_SHADOW"
    assert "minimum_trading_days_not_met" in immature["blockers"]
    assert eligible["decision"] == "ELIGIBLE_FOR_HUMAN_REVIEW"
    assert eligible["automatic_promotion"] is False


def test_weekly_review_without_protocol_uses_only_the_latest_protocol(isolated_db) -> None:
    champion, challenger = _strategies()
    frozen_input = _frozen_input()
    service = PortfolioShadowValidationService(db_manager=isolated_db)
    service.record_comparison(
        event_id="event-old",
        frozen_input=frozen_input,
        champion_strategy=champion,
        challenger_strategy=challenger,
        champion_decision=_decision(champion, frozen_input),
        challenger_decision=_decision(challenger, frozen_input),
        protocol=_protocol(protocol_id="shadow-protocol-old"),
    )
    service.record_comparison(
        event_id="event-latest",
        frozen_input=frozen_input,
        champion_strategy=champion,
        challenger_strategy=challenger,
        champion_decision=_decision(champion, frozen_input),
        challenger_decision=_decision(challenger, frozen_input),
        protocol=_protocol(protocol_id="shadow-protocol-latest"),
    )

    review = service.weekly_review()

    assert review["protocol_id"] == "shadow-protocol-latest"
    assert review["comparison_count"] == 1
    assert review["comparisons"][0]["event_id"] == "event-latest"
