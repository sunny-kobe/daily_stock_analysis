# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from src.config import Config
from src.repositories.portfolio_strategy_validation_repo import (
    PortfolioStrategyValidationRepository,
)
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'strategy-validation.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _manifest(**overrides):
    payload = {
        "strategy_id": "portfolio-champion",
        "strategy_version": "v1",
        "status": "draft",
        "policy_hash": "a" * 64,
        "prompt_template_hash": "b" * 64,
        "model_descriptor": {
            "identity_status": "model_identity_unverified",
            "provider": None,
            "resolved_model": None,
        },
        "generation_config_hash": "c" * 64,
        "code_commit": "e8a9ca7742e8cb2498c8f491dd76d239b3064e1a",
        "snapshot_schema_version": "portfolio-research-snapshot-v1",
        "decision_contract_version": "portfolio-decision-v1",
        "outcome_engine_version": "decision-quality-v2",
        "benchmark_mapping_version": "market-profile-v1",
        "cost_model_version": "shadow-cost-v1",
        "effective_from": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-30T00:00:00Z",
        "approved_by": None,
        "approved_at": None,
    }
    payload.update(overrides)
    return payload


def _run_manifest(**overrides):
    payload = {
        "run_id": "run-001",
        "run_status": "created",
        "eligible_universe_hash": "1" * 64,
        "cutoff_from": "2025-01-01T00:00:00Z",
        "cutoff_to": "2025-12-31T23:59:59Z",
        "split_boundaries": [{"train_end": "2025-06-30", "test_start": "2025-09-01"}],
        "purge_bars": 60,
        "embargo_bars": 60,
        "cost_model_version": "shadow-cost-v1",
        "benchmark_mapping_version": "market-profile-v1",
        "code_commit": "e8a9ca7742e8cb2498c8f491dd76d239b3064e1a",
        "strategy_hashes": ["2" * 64],
        "input_artifact_hashes": ["3" * 64],
    }
    payload.update(overrides)
    return payload


def test_strategy_versions_reject_in_place_manifest_mutation(isolated_db) -> None:
    repo = PortfolioStrategyValidationRepository(isolated_db)
    created = repo.create_strategy_version(_manifest())

    repeated = repo.create_strategy_version(_manifest())
    assert repeated.id == created.id

    with pytest.raises(ValueError, match="immutable_strategy_version"):
        repo.create_strategy_version(_manifest(policy_hash="d" * 64))


def test_validation_run_freezes_protocol_and_input_hashes(isolated_db) -> None:
    repo = PortfolioStrategyValidationRepository(isolated_db)

    row = repo.create_validation_run(_run_manifest())

    persisted = json.loads(row.manifest_json)
    assert persisted["purge_bars"] == 60
    assert persisted["strategy_hashes"] == ["2" * 64]
    assert persisted["input_artifact_hashes"] == ["3" * 64]
    with pytest.raises(ValueError, match="immutable_validation_run"):
        repo.create_validation_run(_run_manifest(purge_bars=20))


def test_validation_events_and_rule_candidates_are_append_only(isolated_db) -> None:
    repo = PortfolioStrategyValidationRepository(isolated_db)
    repo.create_validation_run(_run_manifest())
    event = repo.append_validation_event(
        run_id="run-001",
        event_id="event-001",
        material_event_fingerprint="4" * 64,
        event={"position_action": "hold", "incremental_action": "wait"},
    )
    candidate = repo.create_rule_candidate(
        candidate_id="candidate-001",
        rule_hash="5" * 64,
        evidence_summary={"status": "observed"},
    )

    assert event.event_id == "event-001"
    assert candidate.status == "observed"
    with pytest.raises(ValueError, match="immutable_validation_event"):
        repo.append_validation_event(
            run_id="run-001",
            event_id="event-001",
            material_event_fingerprint="4" * 64,
            event={"position_action": "exit", "incremental_action": "no_add"},
        )
