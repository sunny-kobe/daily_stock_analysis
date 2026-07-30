# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Config
from src.schemas.portfolio_strategy_validation import (
    freeze_strategy_manifest,
    strategy_manifest_hash,
)
from src.services.portfolio_shadow_validation_service import (
    PortfolioShadowValidationService,
)
from src.services.portfolio_strategy_version_service import (
    PortfolioStrategyVersionService,
)
from src.storage import DatabaseManager


@pytest.fixture()
def client_and_db(tmp_path):
    old_env = os.environ.get("ENV_FILE")
    old_db = os.environ.get("DATABASE_PATH")
    env_path = tmp_path / ".env"
    db_path = tmp_path / "strategy-validation-api.db"
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    env_path.write_text(
        f"ADMIN_AUTH_ENABLED=false\nDATABASE_PATH={db_path}\nGEMINI_API_KEY=test\n",
        encoding="utf-8",
    )
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    app = create_app(static_dir=static_dir)
    db = DatabaseManager.get_instance()
    try:
        yield TestClient(app), db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_env is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = old_env
        if old_db is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_db


def _strategies():
    champion = json.loads(
        Path("strategies/validation/champion-v1.json").read_text(encoding="utf-8")
    )
    challenger = deepcopy(champion)
    challenger.update(
        {
            "strategy_id": champion["strategy_id"],
            "strategy_version": "challenger-v1",
            "status": "shadow",
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
        "market": "cn",
        "symbol": "512480",
        "instrument_type": "etf",
    }


def _decision(strategy, frozen_input, **overrides):
    frozen = freeze_strategy_manifest(strategy)
    payload = {
        "input_hash": strategy_manifest_hash(frozen_input),
        "decision_status": "eligible",
        "position_action": "hold",
        "incremental_action": "wait",
        "blockers": [],
        "triggers": ["verified trigger"],
        "invalidation": "evidence invalidated",
        "confidence": {"5d": 0.5, "20d": 0.6, "60d": 0.55},
        "strategy_version": frozen["strategy_version"],
        "strategy_manifest_hash": frozen["manifest_hash"],
    }
    payload.update(overrides)
    return payload


def _protocol():
    return {
        "protocol_id": "shadow-protocol-001",
        "frozen_at": "2026-07-29T08:00:00Z",
        "shadow_start": "2026-07-30T08:00:00Z",
        "minimum_trading_days": 20,
        "minimum_independent_events": 5,
        "max_cost_delta_pct": 0.2,
        "max_drawdown_delta_pct": 1.0,
    }


def _evidence_summary():
    return {
        "maturity_decision": "ELIGIBLE_FOR_HUMAN_REVIEW",
        "historical_oos": {"status": "positive", "event_count": 8},
        "prospective_shadow": {"status": "positive", "trading_days": 20},
        "hard_gate_failures": [],
        "sample_concentration": {"dominant_instrument_pct": 25.0},
        "cost_delta_pct": 0.0,
        "drawdown_delta_pct": 0.0,
        "mature_horizons": ["5d", "20d"],
    }


def test_shadow_comparison_api_records_and_exposes_weekly_review(client_and_db):
    client, _ = client_and_db
    champion, challenger = _strategies()
    frozen_input = _frozen_input()

    response = client.post(
        "/api/v1/decision-signals/strategy-validation/shadow-comparisons",
        json={
            "event_id": "event-001",
            "frozen_input": frozen_input,
            "champion_strategy": champion,
            "challenger_strategy": challenger,
            "champion_decision": _decision(champion, frozen_input),
            "challenger_decision": _decision(
                challenger,
                frozen_input,
                position_action="reduce",
                incremental_action="no_add",
            ),
            "protocol": _protocol(),
        },
    )
    weekly = client.get(
        "/api/v1/decision-signals/strategy-validation/shadow-review",
        params={"protocol_id": "shadow-protocol-001"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["production_signal_written"] is False
    assert response.json()["order_capability"] is False
    assert weekly.status_code == 200, weekly.text
    assert weekly.json()["comparison_count"] == 1
    assert len(weekly.json()["paired_disagreements"]) == 1
    assert weekly.json()["automatic_promotion"] is False


def test_review_summary_is_fail_closed_when_validation_evidence_is_missing(client_and_db):
    client, db = client_and_db
    champion, challenger = _strategies()
    frozen_input = _frozen_input()
    PortfolioStrategyVersionService(db_manager=db).register_strategy(champion)
    PortfolioStrategyVersionService(db_manager=db).register_strategy(challenger)
    PortfolioShadowValidationService(db_manager=db).record_comparison(
        event_id="event-001",
        frozen_input=frozen_input,
        champion_strategy=champion,
        challenger_strategy=challenger,
        champion_decision=_decision(champion, frozen_input),
        challenger_decision=_decision(challenger, frozen_input),
        protocol=_protocol(),
    )

    response = client.get(
        "/api/v1/decision-signals/strategy-validation/review-summary",
        params={
            "strategy_id": champion["strategy_id"],
            "protocol_id": "shadow-protocol-001",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["champion"]["strategy_version"] == champion["strategy_version"]
    assert payload["challenger"]["strategy_version"] == "challenger-v1"
    assert payload["historical_oos"]["status"] == "unable"
    assert payload["mature_horizons"] == []
    assert "validation_evidence_not_recorded" in payload["unable_reasons"]
    assert payload["long_term_improvement_status"] == "PROVISIONAL"
    assert payload["automatic_promotion"] is False
    assert payload["runtime_activated"] is False


def test_human_review_api_records_evidence_without_runtime_activation(client_and_db):
    client, db = client_and_db
    champion, challenger = _strategies()
    service = PortfolioStrategyVersionService(db_manager=db)
    rollback = deepcopy(champion)
    rollback.update({"strategy_version": "champion-v0", "policy_hash": "0" * 64})
    service.register_strategy(rollback)
    service.register_strategy(challenger)

    response = client.post(
        "/api/v1/decision-signals/strategy-validation/reviews",
        json={
            "decision": "approve",
            "strategy_id": challenger["strategy_id"],
            "strategy_version": challenger["strategy_version"],
            "evidence_summary": _evidence_summary(),
            "reason": "Approve bounded use; 60d remains provisional.",
            "rollback_strategy_version": "champion-v0",
            "approved_by": "lan",
            "human_confirmed": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["review_decision"] == "approve"
    assert response.json()["long_term_improvement_status"] == "PROVISIONAL"
    assert response.json()["runtime_activated"] is False


def test_rollback_api_requires_explicit_human_confirmation(client_and_db):
    client, db = client_and_db
    champion, challenger = _strategies()
    service = PortfolioStrategyVersionService(db_manager=db)
    service.register_strategy(champion)
    service.register_strategy(challenger)

    response = client.post(
        "/api/v1/decision-signals/strategy-validation/rollbacks",
        json={
            "strategy_id": champion["strategy_id"],
            "current_strategy_version": challenger["strategy_version"],
            "rollback_strategy_version": champion["strategy_version"],
            "evidence_summary": _evidence_summary(),
            "reason": "Hard gate regression.",
            "approved_by": "lan",
            "human_confirmed": False,
        },
    )

    assert response.status_code == 400, response.text
    assert "human_confirmation_required" in response.text
