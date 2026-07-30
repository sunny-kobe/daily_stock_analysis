# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from src.config import Config
from src.repositories.portfolio_strategy_validation_repo import (
    PortfolioStrategyValidationRepository,
)
from src.services.portfolio_strategy_version_service import PortfolioStrategyVersionService
from src.storage import DatabaseManager

from tests.test_portfolio_strategy_validation_repo import _manifest


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'strategy-version.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_strategy_transition_requires_explicit_human_confirmation(isolated_db) -> None:
    service = PortfolioStrategyVersionService(db_manager=isolated_db)
    service.register_strategy(_manifest())

    with pytest.raises(ValueError, match="human_confirmation_required"):
        service.transition_strategy(
            strategy_id="portfolio-champion",
            strategy_version="v1",
            target_status="shadow",
            human_confirmed=False,
            reason="Start a bounded prospective run.",
        )

    transitioned = service.transition_strategy(
        strategy_id="portfolio-champion",
        strategy_version="v1",
        target_status="shadow",
        human_confirmed=True,
        reason="Start a bounded prospective run.",
    )
    assert transitioned["status"] == "shadow"


def test_strategy_state_machine_rejects_skips_and_automatic_promotion(isolated_db) -> None:
    service = PortfolioStrategyVersionService(db_manager=isolated_db)
    service.register_strategy(_manifest())

    with pytest.raises(ValueError, match="invalid_strategy_transition"):
        service.transition_strategy(
            strategy_id="portfolio-champion",
            strategy_version="v1",
            target_status="champion",
            human_confirmed=True,
            reason="Skip shadow.",
        )
    with pytest.raises(ValueError, match="automatic_promotion_forbidden"):
        service.transition_strategy(
            strategy_id="portfolio-champion",
            strategy_version="v1",
            target_status="shadow",
            human_confirmed=True,
            reason="Automated result.",
            automatic=True,
        )


def test_governance_transition_does_not_change_risk_policy(isolated_db) -> None:
    repo = PortfolioStrategyValidationRepository(isolated_db)
    service = PortfolioStrategyVersionService(repo=repo)
    service.register_strategy(_manifest())
    before = repo.get_risk_policy_identity()

    service.transition_strategy(
        strategy_id="portfolio-champion",
        strategy_version="v1",
        target_status="shadow",
        human_confirmed=True,
        reason="Governance only.",
    )

    assert repo.get_risk_policy_identity() == before


def test_rule_candidate_transition_requires_observed_then_proposed(isolated_db) -> None:
    repo = PortfolioStrategyValidationRepository(isolated_db)
    repo.create_rule_candidate(
        candidate_id="candidate-001",
        rule_hash="5" * 64,
        evidence_summary={"status": "observed"},
    )
    service = PortfolioStrategyVersionService(repo=repo)

    with pytest.raises(ValueError, match="invalid_rule_transition"):
        service.transition_rule_candidate(
            candidate_id="candidate-001",
            target_status="approved",
            human_confirmed=True,
            reason="Skip proposal.",
        )

    proposed = service.transition_rule_candidate(
        candidate_id="candidate-001",
        target_status="proposed",
        human_confirmed=True,
        reason="Prepare frozen evaluation.",
    )
    assert proposed["status"] == "proposed"


def _evidence_summary(**overrides):
    payload = {
        "maturity_decision": "ELIGIBLE_FOR_HUMAN_REVIEW",
        "historical_oos": {"status": "positive", "event_count": 8},
        "prospective_shadow": {"status": "positive", "trading_days": 20},
        "hard_gate_failures": [],
        "sample_concentration": {"dominant_instrument_pct": 25.0},
        "cost_delta_pct": 0.0,
        "drawdown_delta_pct": 0.0,
        "mature_horizons": ["5d", "20d"],
    }
    payload.update(overrides)
    return payload


def test_approval_requires_frozen_evidence_reason_and_rollback_target(isolated_db) -> None:
    service = PortfolioStrategyVersionService(db_manager=isolated_db)
    service.register_strategy(_manifest(strategy_version="v0", policy_hash="0" * 64))
    service.register_strategy(_manifest(strategy_version="v1"))
    service.transition_strategy(
        strategy_id="portfolio-champion",
        strategy_version="v1",
        target_status="shadow",
        human_confirmed=True,
        reason="Start shadow.",
    )

    with pytest.raises(ValueError, match="evidence_summary_incomplete"):
        service.review_strategy(
            decision="approve",
            strategy_id="portfolio-champion",
            strategy_version="v1",
            evidence_summary={"maturity_decision": "ELIGIBLE_FOR_HUMAN_REVIEW"},
            reason="Approve limited use.",
            rollback_strategy_version="v0",
            approved_by="lan",
            human_confirmed=True,
        )

    approved = service.review_strategy(
        decision="approve",
        strategy_id="portfolio-champion",
        strategy_version="v1",
        evidence_summary=_evidence_summary(),
        reason="Approve limited use; 60d remains provisional.",
        rollback_strategy_version="v0",
        approved_by="lan",
        human_confirmed=True,
    )

    assert approved["status"] == "champion"
    assert approved["long_term_improvement_status"] == "PROVISIONAL"
    assert approved["runtime_activated"] is False


def test_missing_20d_maturity_cannot_be_approved(isolated_db) -> None:
    service = PortfolioStrategyVersionService(db_manager=isolated_db)
    service.register_strategy(_manifest(strategy_version="v0", policy_hash="0" * 64))
    service.register_strategy(_manifest(strategy_version="v1"))
    service.transition_strategy(
        strategy_id="portfolio-champion",
        strategy_version="v1",
        target_status="shadow",
        human_confirmed=True,
        reason="Start shadow.",
    )

    with pytest.raises(ValueError, match="mature_20d_evidence_required"):
        service.review_strategy(
            decision="approve",
            strategy_id="portfolio-champion",
            strategy_version="v1",
            evidence_summary=_evidence_summary(mature_horizons=["5d"]),
            reason="Too early.",
            rollback_strategy_version="v0",
            approved_by="lan",
            human_confirmed=True,
        )


def test_rollback_changes_only_selected_reference_and_keeps_audit_rows(isolated_db) -> None:
    service = PortfolioStrategyVersionService(db_manager=isolated_db)
    service.register_strategy(_manifest(strategy_version="v0", policy_hash="0" * 64))
    service.register_strategy(_manifest(strategy_version="v1"))
    service.transition_strategy(
        strategy_id="portfolio-champion",
        strategy_version="v1",
        target_status="shadow",
        human_confirmed=True,
        reason="Start shadow.",
    )
    service.review_strategy(
        decision="approve",
        strategy_id="portfolio-champion",
        strategy_version="v1",
        evidence_summary=_evidence_summary(),
        reason="Approve limited use.",
        rollback_strategy_version="v0",
        approved_by="lan",
        human_confirmed=True,
    )

    rolled_back = service.rollback_strategy(
        strategy_id="portfolio-champion",
        current_strategy_version="v1",
        rollback_strategy_version="v0",
        evidence_summary=_evidence_summary(maturity_decision="ROLLBACK_REQUIRED"),
        reason="Hard gate regression observed after approval.",
        approved_by="lan",
        human_confirmed=True,
    )

    assert rolled_back["selected_strategy_version"] == "v0"
    assert rolled_back["runtime_activated"] is False
    assert rolled_back["strategy_version_count"] == 2
    assert rolled_back["governance_event_count"] >= 2
