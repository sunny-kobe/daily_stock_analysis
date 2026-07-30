# -*- coding: utf-8 -*-
"""Persistence for immutable portfolio strategy validation artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from src.schemas.portfolio_strategy_validation import (
    freeze_strategy_manifest,
    strategy_manifest_hash,
)
from src.storage import (
    DatabaseManager,
    PortfolioRiskPolicy,
    PortfolioRuleCandidateRecord,
    PortfolioShadowComparisonRecord,
    PortfolioStrategyVersionRecord,
    PortfolioStrategyGovernanceEventRecord,
    PortfolioStrategySelectionRecord,
    PortfolioValidationEventRecord,
    PortfolioValidationRunRecord,
    utc_naive_now,
)


_RUN_FIELDS = (
    "run_id",
    "run_status",
    "eligible_universe_hash",
    "cutoff_from",
    "cutoff_to",
    "split_boundaries",
    "purge_bars",
    "embargo_bars",
    "cost_model_version",
    "benchmark_mapping_version",
    "code_commit",
    "strategy_hashes",
    "input_artifact_hashes",
)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        _thaw(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class PortfolioStrategyValidationRepository:
    def __init__(self, db_manager: DatabaseManager | None = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_strategy_version(
        self,
        manifest: Mapping[str, Any],
    ) -> PortfolioStrategyVersionRecord:
        frozen = freeze_strategy_manifest(manifest)
        payload = _thaw(frozen)
        manifest_json = _json(payload)
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioStrategyVersionRecord)
                .where(
                    PortfolioStrategyVersionRecord.strategy_id == payload["strategy_id"],
                    PortfolioStrategyVersionRecord.strategy_version
                    == payload["strategy_version"],
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.manifest_hash != payload["manifest_hash"] or existing.manifest_json != manifest_json:
                    raise ValueError("immutable_strategy_version")
                return existing
            row = PortfolioStrategyVersionRecord(
                strategy_id=payload["strategy_id"],
                strategy_version=payload["strategy_version"],
                status=payload["status"],
                manifest_hash=payload["manifest_hash"],
                manifest_json=manifest_json,
                approved_by=payload.get("approved_by"),
                approved_at=self._datetime(payload.get("approved_at")),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_strategy_version(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
    ) -> PortfolioStrategyVersionRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(PortfolioStrategyVersionRecord)
                .where(
                    PortfolioStrategyVersionRecord.strategy_id == strategy_id,
                    PortfolioStrategyVersionRecord.strategy_version == strategy_version,
                )
                .limit(1)
            ).scalar_one_or_none()

    def list_strategy_versions(
        self,
        *,
        strategy_id: str,
    ) -> list[PortfolioStrategyVersionRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PortfolioStrategyVersionRecord)
                    .where(PortfolioStrategyVersionRecord.strategy_id == strategy_id)
                    .order_by(PortfolioStrategyVersionRecord.created_at)
                ).scalars().all()
            )

    def update_strategy_status(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        status: str,
        reason: str,
        approved_by: str | None = None,
        approved_at: datetime | None = None,
    ) -> PortfolioStrategyVersionRecord:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioStrategyVersionRecord)
                .where(
                    PortfolioStrategyVersionRecord.strategy_id == strategy_id,
                    PortfolioStrategyVersionRecord.strategy_version == strategy_version,
                )
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("strategy_version_not_found")
            row.status = status
            row.governance_reason = reason
            row.approved_by = approved_by
            row.approved_at = approved_at
            row.updated_at = utc_naive_now()
            session.commit()
            session.refresh(row)
            return row

    def create_validation_run(self, manifest: Mapping[str, Any]) -> PortfolioValidationRunRecord:
        if not isinstance(manifest, Mapping):
            raise TypeError("validation run manifest must be a mapping")
        missing = [field for field in _RUN_FIELDS if field not in manifest]
        if missing:
            raise ValueError(f"validation_run_fields_missing:{','.join(missing)}")
        payload = _thaw(manifest)
        if not isinstance(payload["strategy_hashes"], Sequence) or not isinstance(
            payload["input_artifact_hashes"], Sequence
        ):
            raise TypeError("validation run hashes must be sequences")
        manifest_hash = strategy_manifest_hash(payload)
        manifest_json = _json(payload)
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioValidationRunRecord)
                .where(PortfolioValidationRunRecord.run_id == payload["run_id"])
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.manifest_hash != manifest_hash or existing.manifest_json != manifest_json:
                    raise ValueError("immutable_validation_run")
                return existing
            row = PortfolioValidationRunRecord(
                run_id=payload["run_id"],
                run_status=payload["run_status"],
                manifest_hash=manifest_hash,
                manifest_json=manifest_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def append_validation_event(
        self,
        *,
        run_id: str,
        event_id: str,
        material_event_fingerprint: str,
        event: Mapping[str, Any],
    ) -> PortfolioValidationEventRecord:
        event_hash = strategy_manifest_hash(event)
        event_json = _json(event)
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioValidationEventRecord)
                .where(
                    PortfolioValidationEventRecord.run_id == run_id,
                    PortfolioValidationEventRecord.event_id == event_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.event_hash != event_hash
                    or existing.event_json != event_json
                    or existing.material_event_fingerprint != material_event_fingerprint
                ):
                    raise ValueError("immutable_validation_event")
                return existing
            row = PortfolioValidationEventRecord(
                run_id=run_id,
                event_id=event_id,
                material_event_fingerprint=material_event_fingerprint,
                event_hash=event_hash,
                event_json=event_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def create_rule_candidate(
        self,
        *,
        candidate_id: str,
        rule_hash: str,
        evidence_summary: Mapping[str, Any],
    ) -> PortfolioRuleCandidateRecord:
        evidence_json = _json(evidence_summary)
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioRuleCandidateRecord)
                .where(PortfolioRuleCandidateRecord.candidate_id == candidate_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.rule_hash != rule_hash or existing.evidence_summary_json != evidence_json:
                    raise ValueError("immutable_rule_candidate")
                return existing
            row = PortfolioRuleCandidateRecord(
                candidate_id=candidate_id,
                status="observed",
                rule_hash=rule_hash,
                evidence_summary_json=evidence_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_rule_candidate(self, *, candidate_id: str) -> PortfolioRuleCandidateRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(PortfolioRuleCandidateRecord)
                .where(PortfolioRuleCandidateRecord.candidate_id == candidate_id)
                .limit(1)
            ).scalar_one_or_none()

    def update_rule_status(
        self,
        *,
        candidate_id: str,
        status: str,
        reason: str,
        approved_by: str | None = None,
        approved_at: datetime | None = None,
    ) -> PortfolioRuleCandidateRecord:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioRuleCandidateRecord)
                .where(PortfolioRuleCandidateRecord.candidate_id == candidate_id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("rule_candidate_not_found")
            row.status = status
            row.governance_reason = reason
            row.approved_by = approved_by
            row.approved_at = approved_at
            row.updated_at = utc_naive_now()
            session.commit()
            session.refresh(row)
            return row

    def get_risk_policy_identity(self) -> tuple[Any, ...] | None:
        with self.db.get_session() as session:
            row = session.get(PortfolioRiskPolicy, 1)
            if row is None:
                return None
            return tuple(getattr(row, column.name) for column in row.__table__.columns)

    def create_shadow_comparison(
        self,
        *,
        comparison_id: str,
        protocol_id: str,
        event_id: str,
        input_hash: str,
        champion_strategy_hash: str,
        challenger_strategy_hash: str,
        comparison: Mapping[str, Any],
    ) -> PortfolioShadowComparisonRecord:
        comparison_hash = strategy_manifest_hash(comparison)
        comparison_json = _json(comparison)
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioShadowComparisonRecord)
                .where(
                    PortfolioShadowComparisonRecord.protocol_id == protocol_id,
                    PortfolioShadowComparisonRecord.event_id == event_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.comparison_id != comparison_id
                    or existing.comparison_hash != comparison_hash
                    or existing.comparison_json != comparison_json
                ):
                    raise ValueError("immutable_shadow_comparison")
                return existing
            row = PortfolioShadowComparisonRecord(
                comparison_id=comparison_id,
                protocol_id=protocol_id,
                event_id=event_id,
                input_hash=input_hash,
                champion_strategy_hash=champion_strategy_hash,
                challenger_strategy_hash=challenger_strategy_hash,
                comparison_hash=comparison_hash,
                comparison_json=comparison_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list_shadow_comparisons(
        self,
        *,
        protocol_id: str | None = None,
    ) -> list[PortfolioShadowComparisonRecord]:
        query = select(PortfolioShadowComparisonRecord)
        if protocol_id is not None:
            query = query.where(PortfolioShadowComparisonRecord.protocol_id == protocol_id)
        with self.db.get_session() as session:
            return list(
                session.execute(
                    query.order_by(PortfolioShadowComparisonRecord.created_at)
                ).scalars().all()
            )

    def append_governance_event(
        self,
        *,
        event_id: str,
        strategy_id: str,
        strategy_version: str,
        decision: str,
        rollback_strategy_version: str,
        evidence_summary: Mapping[str, Any],
        reason: str,
        approved_by: str,
    ) -> PortfolioStrategyGovernanceEventRecord:
        evidence_hash = strategy_manifest_hash(evidence_summary)
        evidence_json = _json(evidence_summary)
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioStrategyGovernanceEventRecord)
                .where(PortfolioStrategyGovernanceEventRecord.event_id == event_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = PortfolioStrategyGovernanceEventRecord(
                event_id=event_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                decision=decision,
                rollback_strategy_version=rollback_strategy_version,
                evidence_hash=evidence_hash,
                evidence_json=evidence_json,
                reason=reason,
                approved_by=approved_by,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_latest_governance_event(
        self,
        *,
        strategy_id: str,
    ) -> PortfolioStrategyGovernanceEventRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(PortfolioStrategyGovernanceEventRecord)
                .where(PortfolioStrategyGovernanceEventRecord.strategy_id == strategy_id)
                .order_by(
                    PortfolioStrategyGovernanceEventRecord.created_at.desc(),
                    PortfolioStrategyGovernanceEventRecord.id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()

    def set_selected_strategy(
        self,
        *,
        strategy_id: str,
        selected_strategy_version: str,
        rollback_strategy_version: str,
        governance_event_id: str,
    ) -> PortfolioStrategySelectionRecord:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioStrategySelectionRecord)
                .where(PortfolioStrategySelectionRecord.strategy_id == strategy_id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = PortfolioStrategySelectionRecord(strategy_id=strategy_id)
                session.add(row)
            row.selected_strategy_version = selected_strategy_version
            row.rollback_strategy_version = rollback_strategy_version
            row.governance_event_id = governance_event_id
            row.updated_at = utc_naive_now()
            session.commit()
            session.refresh(row)
            return row

    def get_selected_strategy(
        self,
        *,
        strategy_id: str,
    ) -> PortfolioStrategySelectionRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(PortfolioStrategySelectionRecord)
                .where(PortfolioStrategySelectionRecord.strategy_id == strategy_id)
                .limit(1)
            ).scalar_one_or_none()

    def count_strategy_versions(self, *, strategy_id: str) -> int:
        with self.db.get_session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(PortfolioStrategyVersionRecord)
                    .where(PortfolioStrategyVersionRecord.strategy_id == strategy_id)
                )
                or 0
            )

    def count_governance_events(self, *, strategy_id: str) -> int:
        with self.db.get_session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(PortfolioStrategyGovernanceEventRecord)
                    .where(PortfolioStrategyGovernanceEventRecord.strategy_id == strategy_id)
                )
                or 0
            )

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo is not None else value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
