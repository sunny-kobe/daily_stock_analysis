# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.schemas.portfolio_strategy_validation import (
    STRATEGY_STATUSES,
    freeze_strategy_manifest,
    strategy_manifest_hash,
    validate_point_in_time_manifest,
)


def _strategy_manifest(**overrides):
    payload = {
        "strategy_id": "portfolio-champion",
        "strategy_version": "champion-v1",
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
        "outcome_engine_version": "decision-quality-v1",
        "benchmark_mapping_version": "market-profile-v1",
        "cost_model_version": "shadow-cost-unverified-v1",
        "effective_from": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-30T00:00:00Z",
        "approved_by": None,
        "approved_at": None,
    }
    payload.update(overrides)
    return payload


def test_strategy_manifest_hash_changes_when_policy_changes() -> None:
    left = strategy_manifest_hash({"strategy_version": "v1", "policy_hash": "a" * 64})
    right = strategy_manifest_hash({"strategy_version": "v1", "policy_hash": "b" * 64})

    assert left != right


def test_strategy_manifest_hash_is_canonical_for_mapping_order() -> None:
    left = {"strategy_version": "v1", "policy_hash": "a" * 64}
    right = {"policy_hash": "a" * 64, "strategy_version": "v1"}

    assert strategy_manifest_hash(left) == strategy_manifest_hash(right)


def test_freeze_strategy_manifest_rejects_unknown_status() -> None:
    assert STRATEGY_STATUSES == frozenset({"draft", "shadow", "champion", "retired"})

    with pytest.raises(ValueError, match="status"):
        freeze_strategy_manifest(_strategy_manifest(status="provisional"))


def test_frozen_manifest_identity_cannot_be_mutated() -> None:
    frozen = freeze_strategy_manifest(_strategy_manifest())

    with pytest.raises(TypeError):
        frozen["strategy_version"] = "champion-v2"


def test_freeze_strategy_manifest_rejects_approval_without_identity() -> None:
    with pytest.raises(ValueError, match="approved_by"):
        freeze_strategy_manifest(
            _strategy_manifest(status="champion", approved_at="2026-07-30T01:00:00Z")
        )


def test_validation_manifest_rejects_future_evidence() -> None:
    payload = {
        "cutoff": "2026-07-30T08:00:00Z",
        "evidence_artifacts": [
            {"artifact_id": "late", "observed_at": "2026-07-30T08:00:01Z"},
        ],
    }

    with pytest.raises(ValueError, match="evidence_after_cutoff"):
        validate_point_in_time_manifest(payload)


def test_validation_manifest_accepts_evidence_at_or_before_cutoff() -> None:
    payload = {
        "cutoff": datetime(2026, 7, 30, 8, tzinfo=timezone.utc),
        "evidence_artifacts": [
            {"artifact_id": "known", "observed_at": "2026-07-30T08:00:00Z"},
            {"artifact_id": "older", "observed_at": "2026-07-29T08:00:00+00:00"},
        ],
    }

    validated = validate_point_in_time_manifest(payload)

    assert validated["cutoff"] == "2026-07-30T08:00:00Z"
    assert [item["artifact_id"] for item in validated["evidence_artifacts"]] == [
        "known",
        "older",
    ]


def test_champion_v1_manifest_is_a_provisional_read_only_baseline() -> None:
    path = Path("strategies/validation/champion-v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    frozen = freeze_strategy_manifest(payload)

    assert frozen["strategy_version"] == "champion-v1"
    assert frozen["status"] == "draft"
    assert frozen["validation_label"] == "PROVISIONAL_BASELINE"
    assert frozen["model_descriptor"]["identity_status"] == "model_identity_unverified"
