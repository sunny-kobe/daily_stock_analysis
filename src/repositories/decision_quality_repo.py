# -*- coding: utf-8 -*-
"""Persistence boundary for portfolio decision-quality sidecars."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, select

from src.storage import (
    DatabaseManager,
    DecisionSignalAttributionRecord,
    DecisionSignalQualityContextRecord,
    DecisionSignalQualityOutcomeRecord,
    utc_naive_now,
)


_CONTEXT_FIELDS = frozenset(
    {
        "signal_id",
        "account_id",
        "market",
        "stock_code",
        "instrument_type",
        "frozen_snapshot_hash",
        "material_event_fingerprint",
        "position_action",
        "incremental_action",
        "confidence_by_horizon_json",
        "benchmark_market",
        "benchmark_code",
        "benchmark_type",
        "benchmark_evidence_url",
        "benchmark_evidence_as_of",
        "decision_cutoff",
        "context_status",
        "unable_reasons_json",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "signal_id",
        "horizon",
        "engine_version",
        "eval_status",
        "unable_reason",
        "anchor_date",
        "eval_window_days",
        "start_price",
        "end_close",
        "max_high",
        "min_low",
        "stock_return_pct",
        "benchmark_start_price",
        "benchmark_end_close",
        "benchmark_return_pct",
        "excess_return_pct",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
        "normalized_action_return_pct",
        "decision_value_vs_hold_pct",
        "hindsight_regret_pct",
        "decision_value_status",
        "position_action",
        "incremental_action",
        "market",
        "instrument_type",
        "data_quality_level",
    }
)
_ATTRIBUTION_FIELDS = frozenset(
    {
        "signal_id",
        "horizon",
        "engine_version",
        "category",
        "status",
        "summary",
        "evidence_json",
        "counterexamples_json",
        "user_note",
    }
)
_QUALITY_KEY_FIELDS = frozenset({"signal_id", "horizon", "engine_version"})


def _validated_fields(fields: Mapping[str, Any], allowed: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(fields, Mapping):
        raise TypeError(f"{label} fields must be a mapping")
    unsupported = set(fields) - set(allowed)
    if unsupported:
        raise ValueError(f"unsupported {label} fields: {sorted(unsupported)}")
    return dict(fields)


class DecisionQualityRepository:
    """Explicit repository operations for immutable contexts and mutable reviews."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_context_if_absent(
        self,
        fields: Mapping[str, Any],
    ) -> tuple[DecisionSignalQualityContextRecord, bool]:
        values = _validated_fields(fields, _CONTEXT_FIELDS, "context")
        signal_id = values["signal_id"]
        fingerprint = values["material_event_fingerprint"]

        with self.db.get_session() as session:
            by_signal = session.execute(
                select(DecisionSignalQualityContextRecord)
                .where(DecisionSignalQualityContextRecord.signal_id == signal_id)
                .limit(1)
            ).scalar_one_or_none()
            by_fingerprint = session.execute(
                select(DecisionSignalQualityContextRecord)
                .where(DecisionSignalQualityContextRecord.material_event_fingerprint == fingerprint)
                .limit(1)
            ).scalar_one_or_none()

            if by_signal is not None and by_fingerprint is not None and by_signal.id != by_fingerprint.id:
                raise ValueError("immutable context keys resolve to different records")

            existing = by_signal or by_fingerprint
            if existing is not None:
                skip_fields = (
                    {"signal_id", "frozen_snapshot_hash", "decision_cutoff"}
                    if by_signal is None
                    else set()
                )
                for field, incoming in values.items():
                    if field in skip_fields:
                        continue
                    if getattr(existing, field) != incoming:
                        raise ValueError(f"immutable context field changed: {field}")
                return existing, False

            row = DecisionSignalQualityContextRecord(**values)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row, True

    def get_context_by_signal(self, *, signal_id: int) -> DecisionSignalQualityContextRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(DecisionSignalQualityContextRecord)
                .where(DecisionSignalQualityContextRecord.signal_id == signal_id)
                .limit(1)
            ).scalar_one_or_none()

    def list_contexts_for_weekly_review(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[DecisionSignalQualityContextRecord]:
        conditions = []
        if since is not None:
            conditions.append(DecisionSignalQualityContextRecord.created_at >= since)
        if until is not None:
            conditions.append(DecisionSignalQualityContextRecord.created_at < until)
        where_clause = and_(*conditions) if conditions else True
        safe_limit = max(1, min(int(limit), 1000))
        with self.db.get_session() as session:
            rows = session.execute(
                select(DecisionSignalQualityContextRecord)
                .where(where_clause)
                .order_by(
                    desc(DecisionSignalQualityContextRecord.created_at),
                    desc(DecisionSignalQualityContextRecord.id),
                )
                .limit(safe_limit)
            ).scalars().all()
            return list(rows)

    def upsert_quality_outcome(
        self,
        fields: Mapping[str, Any],
    ) -> tuple[DecisionSignalQualityOutcomeRecord, bool]:
        values = _validated_fields(fields, _OUTCOME_FIELDS, "quality outcome")
        key = {field: values[field] for field in _QUALITY_KEY_FIELDS}
        with self.db.get_session() as session:
            existing = session.execute(
                select(DecisionSignalQualityOutcomeRecord)
                .where(
                    DecisionSignalQualityOutcomeRecord.signal_id == key["signal_id"],
                    DecisionSignalQualityOutcomeRecord.horizon == key["horizon"],
                    DecisionSignalQualityOutcomeRecord.engine_version == key["engine_version"],
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is None:
                row = DecisionSignalQualityOutcomeRecord(**values)
                session.add(row)
                session.commit()
                session.refresh(row)
                return row, True

            if existing.eval_status == "complete":
                return existing, False

            for field, value in values.items():
                if field not in _QUALITY_KEY_FIELDS:
                    setattr(existing, field, value)
            existing.updated_at = utc_naive_now()
            session.commit()
            session.refresh(existing)
            return existing, False

    def list_quality_outcomes(
        self,
        *,
        signal_id: int | None = None,
        horizon: str | None = None,
        engine_version: str | None = None,
        eval_status: str | None = None,
    ) -> list[DecisionSignalQualityOutcomeRecord]:
        conditions = []
        if signal_id is not None:
            conditions.append(DecisionSignalQualityOutcomeRecord.signal_id == signal_id)
        if horizon is not None:
            conditions.append(DecisionSignalQualityOutcomeRecord.horizon == horizon)
        if engine_version is not None:
            conditions.append(DecisionSignalQualityOutcomeRecord.engine_version == engine_version)
        if eval_status is not None:
            conditions.append(DecisionSignalQualityOutcomeRecord.eval_status == eval_status)
        where_clause = and_(*conditions) if conditions else True
        with self.db.get_session() as session:
            rows = session.execute(
                select(DecisionSignalQualityOutcomeRecord)
                .where(where_clause)
                .order_by(
                    desc(DecisionSignalQualityOutcomeRecord.updated_at),
                    desc(DecisionSignalQualityOutcomeRecord.id),
                )
            ).scalars().all()
            return list(rows)

    def upsert_attribution(
        self,
        fields: Mapping[str, Any],
    ) -> tuple[DecisionSignalAttributionRecord, bool]:
        values = _validated_fields(fields, _ATTRIBUTION_FIELDS, "attribution")
        key = {field: values[field] for field in _QUALITY_KEY_FIELDS}
        with self.db.get_session() as session:
            existing = session.execute(
                select(DecisionSignalAttributionRecord)
                .where(
                    DecisionSignalAttributionRecord.signal_id == key["signal_id"],
                    DecisionSignalAttributionRecord.horizon == key["horizon"],
                    DecisionSignalAttributionRecord.engine_version == key["engine_version"],
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is None:
                row = DecisionSignalAttributionRecord(**values)
                session.add(row)
                session.commit()
                session.refresh(row)
                return row, True

            for field, value in values.items():
                if field not in _QUALITY_KEY_FIELDS:
                    setattr(existing, field, value)
            existing.updated_at = utc_naive_now()
            session.commit()
            session.refresh(existing)
            return existing, False

    def list_confirmed_attributions(
        self,
        *,
        horizon: str | None = None,
        engine_version: str | None = None,
        limit: int = 1000,
    ) -> list[DecisionSignalAttributionRecord]:
        conditions = [DecisionSignalAttributionRecord.status == "confirmed"]
        if horizon is not None:
            conditions.append(DecisionSignalAttributionRecord.horizon == horizon)
        if engine_version is not None:
            conditions.append(DecisionSignalAttributionRecord.engine_version == engine_version)
        safe_limit = max(1, min(int(limit), 5000))
        with self.db.get_session() as session:
            rows = session.execute(
                select(DecisionSignalAttributionRecord)
                .where(and_(*conditions))
                .order_by(
                    desc(DecisionSignalAttributionRecord.updated_at),
                    desc(DecisionSignalAttributionRecord.id),
                )
                .limit(safe_limit)
            ).scalars().all()
            return list(rows)

    def get_attribution(
        self,
        *,
        signal_id: int,
        horizon: str,
        engine_version: str,
    ) -> DecisionSignalAttributionRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(DecisionSignalAttributionRecord)
                .where(
                    DecisionSignalAttributionRecord.signal_id == signal_id,
                    DecisionSignalAttributionRecord.horizon == horizon,
                    DecisionSignalAttributionRecord.engine_version == engine_version,
                )
                .limit(1)
            ).scalar_one_or_none()

    def list_attributions(self, *, signal_id: int | None = None) -> list[DecisionSignalAttributionRecord]:
        condition = (
            DecisionSignalAttributionRecord.signal_id == signal_id
            if signal_id is not None
            else True
        )
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(DecisionSignalAttributionRecord)
                    .where(condition)
                    .order_by(DecisionSignalAttributionRecord.horizon)
                ).scalars().all()
            )
