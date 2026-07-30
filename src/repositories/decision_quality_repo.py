# -*- coding: utf-8 -*-
"""Persistence boundary for portfolio decision-quality sidecars."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, select

from src.storage import (
    DatabaseManager,
    DecisionSignalAttributionRecord,
    DecisionSignalExecutionLinkRecord,
    DecisionSignalQualityContextRecord,
    DecisionSignalQualityOutcomeRecord,
    PortfolioTrade,
    utc_naive_now,
)


_CONTEXT_FIELDS = frozenset(
    {
        "signal_id",
        "account_id",
        "market",
        "stock_code",
        "instrument_type",
        "frozen_position_quantity",
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
        "decision_market_phase",
        "strategy_version",
        "context_status",
        "unable_reasons_json",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "signal_id",
        "horizon",
        "engine_version",
        "data_revision_hash",
        "input_bar_hash",
        "computed_at",
        "eval_status",
        "unable_reason",
        "anchor_date",
        "observation_anchor_date",
        "shadow_execution_date",
        "execution_anchor_type",
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
_QUALITY_KEY_FIELDS = frozenset(
    {"signal_id", "horizon", "engine_version", "data_revision_hash"}
)
_ATTRIBUTION_KEY_FIELDS = frozenset({"signal_id", "horizon", "engine_version"})
_EXECUTION_LINK_FIELDS = frozenset(
    {
        "signal_id",
        "trade_id",
        "link_status",
        "temporal_relation",
        "linked_by",
        "note",
    }
)
_LEGACY_DATA_REVISION_HASH = hashlib.sha256(b"legacy-unversioned-data").hexdigest()


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
        values.setdefault("decision_market_phase", None)
        values.setdefault("strategy_version", "legacy-unversioned")

        with self.db.get_session() as session:
            by_signal = session.execute(
                select(DecisionSignalQualityContextRecord)
                .where(DecisionSignalQualityContextRecord.signal_id == signal_id)
                .limit(1)
            ).scalar_one_or_none()
            existing = by_signal
            if existing is not None:
                for field, incoming in values.items():
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
        values.setdefault("data_revision_hash", _LEGACY_DATA_REVISION_HASH)
        values.setdefault("input_bar_hash", _LEGACY_DATA_REVISION_HASH)
        values.setdefault("computed_at", utc_naive_now())
        values.setdefault("observation_anchor_date", values.get("anchor_date"))
        values.setdefault("shadow_execution_date", None)
        values.setdefault("execution_anchor_type", "legacy_unverified")
        key = {field: values[field] for field in _QUALITY_KEY_FIELDS}
        with self.db.get_session() as session:
            existing = session.execute(
                select(DecisionSignalQualityOutcomeRecord)
                .where(
                    DecisionSignalQualityOutcomeRecord.signal_id == key["signal_id"],
                    DecisionSignalQualityOutcomeRecord.horizon == key["horizon"],
                    DecisionSignalQualityOutcomeRecord.engine_version == key["engine_version"],
                    DecisionSignalQualityOutcomeRecord.data_revision_hash
                    == key["data_revision_hash"],
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
                changed = [
                    field
                    for field, value in values.items()
                    if field not in _QUALITY_KEY_FIELDS | {"computed_at"}
                    and getattr(existing, field) != value
                ]
                if changed:
                    raise ValueError(
                        f"completed_outcome_immutable:{','.join(sorted(changed))}"
                    )
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
        data_revision_hash: str | None = None,
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
        if data_revision_hash is not None:
            conditions.append(
                DecisionSignalQualityOutcomeRecord.data_revision_hash == data_revision_hash
            )
        where_clause = and_(*conditions) if conditions else True
        with self.db.get_session() as session:
            rows = session.execute(
                select(DecisionSignalQualityOutcomeRecord)
                .where(where_clause)
                .order_by(
                    desc(DecisionSignalQualityOutcomeRecord.computed_at),
                    desc(DecisionSignalQualityOutcomeRecord.id),
                )
            ).scalars().all()
            return list(rows)

    def upsert_attribution(
        self,
        fields: Mapping[str, Any],
    ) -> tuple[DecisionSignalAttributionRecord, bool]:
        values = _validated_fields(fields, _ATTRIBUTION_FIELDS, "attribution")
        key = {field: values[field] for field in _ATTRIBUTION_KEY_FIELDS}
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
                if field not in _ATTRIBUTION_KEY_FIELDS:
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

    def get_trade(self, *, trade_id: int) -> PortfolioTrade | None:
        with self.db.get_session() as session:
            row = session.get(PortfolioTrade, trade_id)
            if row is not None:
                session.expunge(row)
            return row

    def upsert_execution_link(
        self,
        fields: Mapping[str, Any],
    ) -> tuple[DecisionSignalExecutionLinkRecord, bool]:
        values = _validated_fields(fields, _EXECUTION_LINK_FIELDS, "execution link")
        with self.db.get_session() as session:
            existing = session.execute(
                select(DecisionSignalExecutionLinkRecord)
                .where(
                    DecisionSignalExecutionLinkRecord.signal_id == values["signal_id"],
                    DecisionSignalExecutionLinkRecord.trade_id == values["trade_id"],
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is None:
                row = DecisionSignalExecutionLinkRecord(**values)
                session.add(row)
                session.commit()
                session.refresh(row)
                return row, True
            if existing.link_status == "confirmed":
                changed = [
                    field
                    for field, value in values.items()
                    if getattr(existing, field) != value
                ]
                if changed:
                    raise ValueError(
                        f"confirmed_execution_link_immutable:{','.join(sorted(changed))}"
                    )
                return existing, False
            for field, value in values.items():
                if field not in {"signal_id", "trade_id"}:
                    setattr(existing, field, value)
            existing.updated_at = utc_naive_now()
            session.commit()
            session.refresh(existing)
            return existing, False

    def get_confirmed_execution_link_by_trade(
        self,
        *,
        trade_id: int,
        exclude_signal_id: int | None = None,
    ) -> DecisionSignalExecutionLinkRecord | None:
        conditions = [
            DecisionSignalExecutionLinkRecord.trade_id == trade_id,
            DecisionSignalExecutionLinkRecord.link_status == "confirmed",
        ]
        if exclude_signal_id is not None:
            conditions.append(DecisionSignalExecutionLinkRecord.signal_id != exclude_signal_id)
        with self.db.get_session() as session:
            return session.execute(
                select(DecisionSignalExecutionLinkRecord)
                .where(and_(*conditions))
                .limit(1)
            ).scalar_one_or_none()

    def list_execution_links(
        self,
        *,
        signal_id: int,
    ) -> list[DecisionSignalExecutionLinkRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(DecisionSignalExecutionLinkRecord)
                    .where(DecisionSignalExecutionLinkRecord.signal_id == signal_id)
                    .order_by(DecisionSignalExecutionLinkRecord.id)
                ).scalars().all()
            )
