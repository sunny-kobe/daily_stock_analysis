# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.config import Config
from src.repositories.decision_evidence_snapshot_repo import (
    DecisionEvidenceSnapshotRepository,
)
from src.schemas.decision_evidence_snapshot import DecisionEvidenceSnapshot
from src.storage import DatabaseManager, DecisionSignalEvidenceSnapshotRecord


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'evidence_snapshot.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _snapshot(**overrides) -> DecisionEvidenceSnapshot:
    payload = {
        "schema_version": "decision-evidence-snapshot-v1",
        "signal_id": 101,
        "quality_context_id": None,
        "strategy_key": "portfolio-current-policy",
        "strategy_version": "1.0.0",
        "strategy_manifest_hash": "a" * 64,
        "decision_cutoff": "2026-07-31T08:00:00Z",
        "reporting_currency": "CNY",
        "structured_inputs": {"account_id": 1, "market": "cn", "symbol": "600519"},
        "evidence_bundle": {"benchmark": {"symbol": "000300"}},
        "readiness_status": "insufficient_evidence",
        "blockers": ["benchmark_bar_missing"],
        "snapshot_hash": "b" * 64,
    }
    payload.update(overrides)
    return DecisionEvidenceSnapshot.model_validate(payload)


def test_additive_table_has_only_the_fixed_snapshot_columns(isolated_db) -> None:
    db_inspector = inspect(isolated_db._engine)
    columns = {
        column["name"]: column
        for column in db_inspector.get_columns(
            DecisionSignalEvidenceSnapshotRecord.__tablename__
        )
    }

    assert set(columns) == {
        "id",
        "signal_id",
        "quality_context_id",
        "schema_version",
        "strategy_key",
        "strategy_version",
        "strategy_manifest_hash",
        "decision_cutoff",
        "reporting_currency",
        "structured_inputs_json",
        "decision_input_hash",
        "evidence_bundle_json",
        "evidence_bundle_hash",
        "readiness_status",
        "blockers_json",
        "snapshot_hash",
        "created_at",
    }
    assert all(
        column["nullable"] is False
        for name, column in columns.items()
        if name != "quality_context_id"
    )
    assert columns["quality_context_id"]["nullable"] is True
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in db_inspector.get_unique_constraints(
            DecisionSignalEvidenceSnapshotRecord.__tablename__
        )
    }
    unique_columns.update(
        tuple(index["column_names"])
        for index in db_inspector.get_indexes(
            DecisionSignalEvidenceSnapshotRecord.__tablename__
        )
        if index["unique"]
    )
    assert ("signal_id",) in unique_columns
    assert "updated_at" not in columns


def test_create_if_absent_is_idempotent_for_identical_snapshot(isolated_db) -> None:
    repo = DecisionEvidenceSnapshotRepository(isolated_db)
    fields = _snapshot().to_record_fields()

    first, created = repo.create_if_absent(fields)
    repeated, repeated_created = repo.create_if_absent(fields)

    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert repeated.signal_id == 101
    assert isinstance(repeated.created_at, datetime)
    assert repo.get_by_signal_id(signal_id=101).id == first.id


def test_create_if_absent_rejects_any_change_for_same_signal(isolated_db) -> None:
    repo = DecisionEvidenceSnapshotRepository(isolated_db)
    original, _ = repo.create_if_absent(_snapshot().to_record_fields())

    changed_fields = _snapshot(
        evidence_bundle={"benchmark": {"symbol": "SPY"}}
    ).to_record_fields()
    with pytest.raises(ValueError, match="^immutable_evidence_snapshot_changed$"):
        repo.create_if_absent(changed_fields)

    persisted = repo.get_by_signal_id(signal_id=101)
    assert persisted.id == original.id
    assert persisted.snapshot_hash == original.snapshot_hash


def test_repository_rejects_missing_or_unsupported_fields(isolated_db) -> None:
    repo = DecisionEvidenceSnapshotRepository(isolated_db)
    fields = _snapshot().to_record_fields()

    with pytest.raises(ValueError, match="invalid evidence snapshot fields"):
        repo.create_if_absent({key: value for key, value in fields.items() if key != "signal_id"})
    with pytest.raises(ValueError, match="invalid evidence snapshot fields"):
        repo.create_if_absent({**fields, "updated_at": datetime.now()})


def test_repository_exposes_no_mutation_or_delete_operations(isolated_db) -> None:
    repo = DecisionEvidenceSnapshotRepository(isolated_db)

    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
    assert not hasattr(repo, "upsert")


def test_database_rejects_direct_snapshot_update_and_delete(isolated_db) -> None:
    repo = DecisionEvidenceSnapshotRepository(isolated_db)
    original, _ = repo.create_if_absent(_snapshot().to_record_fields())

    with pytest.raises(IntegrityError, match="decision_evidence_immutable"):
        with isolated_db.get_session() as session:
            session.execute(
                text(
                    "UPDATE decision_signal_evidence_snapshots "
                    "SET readiness_status = 'complete' WHERE id = :id"
                ),
                {"id": original.id},
            )
            session.commit()

    with pytest.raises(IntegrityError, match="decision_evidence_immutable"):
        with isolated_db.get_session() as session:
            session.execute(
                text("DELETE FROM decision_signal_evidence_snapshots WHERE id = :id"),
                {"id": original.id},
            )
            session.commit()

    persisted = repo.get_by_signal_id(signal_id=101)
    assert persisted is not None
    assert persisted.readiness_status == "insufficient_evidence"
