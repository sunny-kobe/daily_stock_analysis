# -*- coding: utf-8 -*-
"""Persistence operations for immutable strategy-validation records."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from sqlalchemy import and_, desc, select

from src.storage import (
    DatabaseManager,
    PortfolioStrategyTransitionRecord,
    PortfolioStrategyValidationRunRecord,
    PortfolioStrategyVersionRecord,
)


_VERSION_FIELDS = frozenset(
    {"strategy_key", "version", "name", "initial_status", "manifest_json", "manifest_hash"}
)
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "strategy_key",
        "strategy_version",
        "validation_kind",
        "protocol_json",
        "dataset_hash",
        "engine_version",
        "status",
        "qualifying",
        "result_json",
        "run_hash",
    }
)


def _validated_fields(fields: Mapping[str, Any], allowed: frozenset[str], label: str) -> dict[str, Any]:
    unsupported = set(fields) - set(allowed)
    missing = set(allowed) - set(fields)
    if unsupported or missing:
        raise ValueError(
            f"invalid {label} fields: missing={sorted(missing)} unsupported={sorted(unsupported)}"
        )
    return dict(fields)


class StrategyValidationRepository:
    def __init__(self, db_manager: DatabaseManager | None = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_strategy_version(
        self, fields: Mapping[str, Any]
    ) -> tuple[PortfolioStrategyVersionRecord, bool]:
        values = _validated_fields(fields, _VERSION_FIELDS, "strategy version")
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioStrategyVersionRecord)
                .where(
                    PortfolioStrategyVersionRecord.strategy_key == values["strategy_key"],
                    PortfolioStrategyVersionRecord.version == values["version"],
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if any(getattr(existing, field) != value for field, value in values.items()):
                    raise ValueError("strategy_version_immutable")
                return existing, False
            row = PortfolioStrategyVersionRecord(**values)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row, True

    def get_strategy_version(
        self, *, strategy_key: str, version: str
    ) -> PortfolioStrategyVersionRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(PortfolioStrategyVersionRecord)
                .where(
                    PortfolioStrategyVersionRecord.strategy_key == strategy_key,
                    PortfolioStrategyVersionRecord.version == version,
                )
                .limit(1)
            ).scalar_one_or_none()

    def list_strategy_versions(self) -> list[PortfolioStrategyVersionRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PortfolioStrategyVersionRecord).order_by(
                        PortfolioStrategyVersionRecord.strategy_key,
                        desc(PortfolioStrategyVersionRecord.created_at),
                    )
                ).scalars().all()
            )

    def create_validation_run(
        self, fields: Mapping[str, Any]
    ) -> tuple[PortfolioStrategyValidationRunRecord, bool]:
        values = _validated_fields(fields, _RUN_FIELDS, "validation run")
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioStrategyValidationRunRecord)
                .where(
                    (
                        PortfolioStrategyValidationRunRecord.run_id == values["run_id"]
                    )
                    | (PortfolioStrategyValidationRunRecord.run_hash == values["run_hash"])
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                if any(getattr(existing, field) != value for field, value in values.items()):
                    raise ValueError("validation_run_immutable")
                return existing, False
            row = PortfolioStrategyValidationRunRecord(**values)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row, True

    def get_validation_run(self, *, run_id: str) -> PortfolioStrategyValidationRunRecord | None:
        with self.db.get_session() as session:
            return session.execute(
                select(PortfolioStrategyValidationRunRecord)
                .where(PortfolioStrategyValidationRunRecord.run_id == run_id)
                .limit(1)
            ).scalar_one_or_none()

    def list_validation_runs(
        self,
        *,
        strategy_key: str,
        strategy_version: str,
    ) -> list[PortfolioStrategyValidationRunRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PortfolioStrategyValidationRunRecord)
                    .where(
                        PortfolioStrategyValidationRunRecord.strategy_key == strategy_key,
                        PortfolioStrategyValidationRunRecord.strategy_version == strategy_version,
                    )
                    .order_by(desc(PortfolioStrategyValidationRunRecord.created_at))
                ).scalars().all()
            )

    def has_qualifying_run(
        self,
        *,
        strategy_key: str,
        strategy_version: str,
        validation_kind: str,
    ) -> bool:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PortfolioStrategyValidationRunRecord.result_json).where(
                    PortfolioStrategyValidationRunRecord.strategy_key == strategy_key,
                    PortfolioStrategyValidationRunRecord.strategy_version == strategy_version,
                    PortfolioStrategyValidationRunRecord.validation_kind == validation_kind,
                    PortfolioStrategyValidationRunRecord.status == "completed",
                    PortfolioStrategyValidationRunRecord.qualifying.is_(True),
                )
            ).scalars()
            if validation_kind != "historical_backtest":
                return next(rows, None) is not None
            for result_json in rows:
                try:
                    result = json.loads(result_json)
                except (TypeError, ValueError):
                    continue
                if isinstance(result, dict) and result.get("historical_status") == "complete":
                    return True
            return False

    def append_transition(
        self,
        *,
        strategy_key: str,
        strategy_version: str,
        from_status: str,
        to_status: str,
        human_reason: str,
    ) -> PortfolioStrategyTransitionRecord:
        with self.db.get_session() as session:
            row = PortfolioStrategyTransitionRecord(
                strategy_key=strategy_key,
                strategy_version=strategy_version,
                from_status=from_status,
                to_status=to_status,
                human_reason=human_reason,
                actor_type="human",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list_transitions(
        self, *, strategy_key: str, strategy_version: str
    ) -> list[PortfolioStrategyTransitionRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PortfolioStrategyTransitionRecord)
                    .where(
                        and_(
                            PortfolioStrategyTransitionRecord.strategy_key == strategy_key,
                            PortfolioStrategyTransitionRecord.strategy_version == strategy_version,
                        )
                    )
                    .order_by(
                        PortfolioStrategyTransitionRecord.created_at,
                        PortfolioStrategyTransitionRecord.id,
                    )
                ).scalars().all()
            )
