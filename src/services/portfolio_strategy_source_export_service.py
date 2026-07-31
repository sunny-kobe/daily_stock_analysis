# -*- coding: utf-8 -*-
"""Export persisted portfolio decision candidates from SQLite without backfilling evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.portfolio_strategy_historical_sample_service import (
    FROZEN_SOURCE_SCHEMA_VERSION,
)


_REQUIRED_TABLES = frozenset(
    {
        "decision_signals",
        "decision_signal_quality_contexts",
    }
)

_UNRECOVERABLE_GAPS = (
    "strategy_binding_missing",
    "decision_input_hash_missing",
    "structured_inputs_missing",
    "identity_evidence_not_frozen",
    "product_evidence_not_frozen",
    "adjustment_evidence_not_frozen",
    "benchmark_evidence_incomplete",
    "fx_evidence_not_frozen",
    "cost_and_trading_evidence_not_frozen",
    "price_bars_not_frozen",
    "development_validation_partition_missing",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class PortfolioStrategySourceExportService:
    """Read the current DSA baseline as candidates, preserving all historical gaps."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def export(self, *, frozen_at: str, reporting_currency: str) -> dict[str, Any]:
        frozen_at_value = self._aware_datetime(frozen_at)
        if frozen_at_value is None:
            raise ValueError("frozen_at_invalid")
        if frozen_at_value.astimezone(timezone.utc) > datetime.now(timezone.utc):
            raise ValueError("frozen_at_in_future")
        reporting_currency_value = str(reporting_currency or "").strip().upper()
        if not reporting_currency_value:
            raise ValueError("reporting_currency_required")

        connection = self._connect_readonly()
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            self._require_tables(connection)
            rows = connection.execute(
                """
                SELECT
                    q.id AS context_id,
                    q.signal_id,
                    q.account_id,
                    q.market,
                    q.stock_code,
                    q.instrument_type,
                    q.frozen_snapshot_hash,
                    q.material_event_fingerprint,
                    q.position_action,
                    q.incremental_action,
                    q.benchmark_market,
                    q.benchmark_code,
                    q.benchmark_type,
                    q.benchmark_evidence_url,
                    q.benchmark_evidence_as_of,
                    q.decision_cutoff,
                    q.context_status,
                    q.unable_reasons_json,
                    q.created_at AS context_created_at,
                    q.updated_at AS context_updated_at,
                    s.metadata_json AS signal_metadata_json,
                    s.created_at AS signal_created_at
                FROM decision_signal_quality_contexts AS q
                LEFT JOIN decision_signals AS s ON s.id = q.signal_id
                ORDER BY q.id ASC
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ValueError("source_database_contract_missing") from exc
        finally:
            connection.close()

        candidates: list[dict[str, Any]] = []
        excluded_after_frozen_at = 0
        excluded_after_frozen_at_reasons: Counter[str] = Counter()
        invalid_decision_cutoff = 0
        for row in rows:
            row_data = dict(row)
            cutoff = self._stored_utc_datetime(row_data["decision_cutoff"])
            if cutoff is None:
                invalid_decision_cutoff += 1
            late_reasons = self._after_frozen_at_reasons(
                row_data,
                decision_cutoff=cutoff,
                frozen_at=frozen_at_value.astimezone(timezone.utc),
            )
            if late_reasons:
                excluded_after_frozen_at += 1
                excluded_after_frozen_at_reasons.update(late_reasons)
                continue
            candidates.append(self._candidate(row_data, decision_cutoff=cutoff))

        gap_counts = Counter(
            gap
            for candidate in candidates
            for gap in candidate["audit_gaps"]
        )
        payload = {
            "schema_version": FROZEN_SOURCE_SCHEMA_VERSION,
            "synthetic": False,
            "frozen_at": frozen_at_value.isoformat(),
            "reporting_currency": reporting_currency_value,
            "candidate_count": len(candidates),
            "source_audit": {
                "scope": "persisted_candidates_only",
                "historical_evidence_backfilled": False,
                "total_context_count": len(rows),
                "excluded_after_frozen_at": excluded_after_frozen_at,
                "excluded_after_frozen_at_reasons": dict(
                    sorted(excluded_after_frozen_at_reasons.items())
                ),
                "invalid_decision_cutoff": invalid_decision_cutoff,
                "gap_counts": dict(sorted(gap_counts.items())),
            },
            "candidates": candidates,
        }
        return {**payload, "source_snapshot_hash": _sha256_json(payload)}

    def _connect_readonly(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise ValueError("source_database_missing")
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        try:
            return sqlite3.connect(uri, uri=True)
        except sqlite3.DatabaseError as exc:
            raise ValueError("source_database_unreadable") from exc

    @staticmethod
    def _require_tables(connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not _REQUIRED_TABLES.issubset(tables):
            raise ValueError("source_database_contract_missing")

    @classmethod
    def _candidate(
        cls,
        row: dict[str, Any],
        *,
        decision_cutoff: datetime | None,
    ) -> dict[str, Any]:
        row_hash = _sha256_json(row)
        unable_reasons = cls._json_list(row.get("unable_reasons_json"))
        audit_gaps = list(_UNRECOVERABLE_GAPS)
        signal_created_at = cls._stored_utc_datetime(row.get("signal_created_at"))
        context_created_at = cls._stored_utc_datetime(row.get("context_created_at"))
        context_updated_at = cls._stored_utc_datetime(row.get("context_updated_at"))
        if decision_cutoff is None:
            audit_gaps.append("decision_cutoff_invalid")
        if row.get("signal_created_at") is None:
            audit_gaps.append("decision_signal_missing")
        elif signal_created_at is None:
            audit_gaps.append("decision_signal_time_invalid")
        if context_created_at is None:
            audit_gaps.append("decision_context_created_time_invalid")
        if context_updated_at is None:
            audit_gaps.append("decision_context_updated_time_invalid")
        evidence_as_of = (
            max(signal_created_at, context_created_at, context_updated_at)
            if signal_created_at is not None
            and context_created_at is not None
            and context_updated_at is not None
            else None
        )
        if decision_cutoff is not None and evidence_as_of is not None and evidence_as_of > decision_cutoff:
            audit_gaps.append("decision_persisted_after_cutoff")
        benchmark = {
            "market": row.get("benchmark_market"),
            "code": row.get("benchmark_code"),
            "type": row.get("benchmark_type"),
            "evidence_url": row.get("benchmark_evidence_url"),
            "evidence_as_of": row.get("benchmark_evidence_as_of"),
        }
        decision = {
            "decision_id": f"decision-signal-{row['signal_id']}",
            "account_id": str(row["account_id"]),
            "position_action": row.get("position_action"),
            "incremental_action": row.get("incremental_action"),
            "source": "dsa-decision-quality-context",
            "source_hash": row_hash,
        }
        if decision_cutoff is not None:
            decision["decision_cutoff"] = decision_cutoff.isoformat()
        if evidence_as_of is not None:
            decision["as_of"] = evidence_as_of.isoformat()
        return {
            "candidate_id": f"dsa-quality-context-{row['context_id']}",
            "decision": decision,
            "audit_context": {
                "market": row.get("market"),
                "symbol": row.get("stock_code"),
                "instrument_type": row.get("instrument_type"),
                "frozen_snapshot_hash": row.get("frozen_snapshot_hash"),
                "material_event_fingerprint": row.get("material_event_fingerprint"),
                "context_status": row.get("context_status"),
                "unable_reasons": unable_reasons,
                "benchmark": benchmark,
            },
            "audit_gaps": audit_gaps,
        }

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return ["persisted_unable_reasons_invalid"]
        return parsed if isinstance(parsed, list) else ["persisted_unable_reasons_invalid"]

    @classmethod
    def _after_frozen_at_reasons(
        cls,
        row: dict[str, Any],
        *,
        decision_cutoff: datetime | None,
        frozen_at: datetime,
    ) -> list[str]:
        reasons: list[str] = []
        if decision_cutoff is not None and decision_cutoff > frozen_at:
            reasons.append("decision_cutoff_after_frozen_at")
        for field, reason in (
            ("signal_created_at", "signal_created_after_frozen_at"),
            ("context_created_at", "context_created_after_frozen_at"),
            ("context_updated_at", "context_updated_after_frozen_at"),
        ):
            persisted_at = cls._stored_utc_datetime(row.get(field))
            if persisted_at is not None and persisted_at > frozen_at:
                reasons.append(reason)
        return reasons

    @staticmethod
    def _aware_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    @staticmethod
    def _stored_utc_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
