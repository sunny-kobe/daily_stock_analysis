# -*- coding: utf-8 -*-
"""Persistence boundary for immutable decision evidence snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.storage import (
    DatabaseManager,
    DecisionSignalEvidenceSnapshotRecord,
    to_utc_naive_datetime,
)


_SNAPSHOT_FIELDS = frozenset(
    {
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
    }
)


def _validated_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(fields, Mapping):
        raise TypeError("evidence snapshot fields must be a mapping")
    missing = set(_SNAPSHOT_FIELDS) - set(fields)
    unsupported = set(fields) - set(_SNAPSHOT_FIELDS)
    if missing or unsupported:
        raise ValueError(
            "invalid evidence snapshot fields: "
            f"missing={sorted(missing)} unsupported={sorted(unsupported)}"
        )
    values = dict(fields)
    values["decision_cutoff"] = to_utc_naive_datetime(values["decision_cutoff"])
    return values


def _assert_identical(
    existing: DecisionSignalEvidenceSnapshotRecord,
    values: Mapping[str, Any],
) -> None:
    if any(getattr(existing, field) != value for field, value in values.items()):
        raise ValueError("immutable_evidence_snapshot_changed")


class DecisionEvidenceSnapshotRepository:
    """Create and read one immutable evidence sidecar per signal."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_if_absent(
        self,
        fields: Mapping[str, Any],
    ) -> tuple[DecisionSignalEvidenceSnapshotRecord, bool]:
        values = _validated_fields(fields)
        with self.db.get_session() as session:
            existing = session.execute(
                select(DecisionSignalEvidenceSnapshotRecord)
                .where(DecisionSignalEvidenceSnapshotRecord.signal_id == values["signal_id"])
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                _assert_identical(existing, values)
                return existing, False

            row = DecisionSignalEvidenceSnapshotRecord(**values)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(DecisionSignalEvidenceSnapshotRecord)
                    .where(
                        DecisionSignalEvidenceSnapshotRecord.signal_id == values["signal_id"]
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                _assert_identical(existing, values)
                return existing, False
            session.refresh(row)
            return row, True

    def get_by_signal_id(
        self,
        *,
        signal_id: int,
    ) -> DecisionSignalEvidenceSnapshotRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(DecisionSignalEvidenceSnapshotRecord)
                .where(DecisionSignalEvidenceSnapshotRecord.signal_id == signal_id)
                .limit(1)
            ).scalar_one_or_none()
