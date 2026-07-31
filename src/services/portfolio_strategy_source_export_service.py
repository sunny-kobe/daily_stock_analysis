# -*- coding: utf-8 -*-
"""Export persisted portfolio decision candidates from SQLite without backfilling evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from src.services.portfolio_strategy_historical_sample_service import (
    FROZEN_SOURCE_SCHEMA_VERSION,
)


_REQUIRED_TABLES = frozenset(
    {
        "decision_signals",
        "decision_signal_evidence_snapshots",
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

_RECOMMENDATION_TIME_GAPS = frozenset(_UNRECOVERABLE_GAPS[:-2])
_EXPECTED_BENCHMARKS = {
    "cn": "000300",
    "hk": "HSI",
    "us": "SPY",
}
_EVIDENCE_SNAPSHOT_SCHEMA_VERSION = "decision-evidence-snapshot-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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
                    s.created_at AS signal_created_at,
                    e.id AS evidence_snapshot_id,
                    e.signal_id AS evidence_signal_id,
                    e.quality_context_id AS evidence_quality_context_id,
                    e.schema_version AS evidence_schema_version,
                    e.strategy_key AS evidence_strategy_key,
                    e.strategy_version AS evidence_strategy_version,
                    e.strategy_manifest_hash AS evidence_strategy_manifest_hash,
                    e.decision_cutoff AS evidence_decision_cutoff,
                    e.reporting_currency AS evidence_reporting_currency,
                    e.structured_inputs_json,
                    e.decision_input_hash,
                    e.evidence_bundle_json,
                    e.evidence_bundle_hash,
                    e.readiness_status AS evidence_readiness_status,
                    e.blockers_json AS evidence_blockers_json,
                    e.snapshot_hash AS evidence_snapshot_hash,
                    e.created_at AS evidence_created_at
                FROM decision_signal_quality_contexts AS q
                LEFT JOIN decision_signals AS s ON s.id = q.signal_id
                LEFT JOIN decision_signal_evidence_snapshots AS e
                    ON e.signal_id = q.signal_id
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
            candidates.append(
                self._candidate(
                    row_data,
                    decision_cutoff=cutoff,
                    reporting_currency=reporting_currency_value,
                )
            )

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
        reporting_currency: str,
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
        candidate = {
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
        if row.get("evidence_snapshot_id") is None:
            candidate["audit_gaps"].append("legacy_evidence_snapshot_missing")
            return candidate

        structured_inputs = cls._json_object(row.get("structured_inputs_json"))
        evidence_bundle = cls._json_object(row.get("evidence_bundle_json"))
        snapshot_gaps = cls._evidence_snapshot_gaps(
            row,
            decision_cutoff=decision_cutoff,
            reporting_currency=reporting_currency,
            structured_inputs=structured_inputs,
            evidence_bundle=evidence_bundle,
        )
        if snapshot_gaps:
            candidate["audit_gaps"].extend(snapshot_gaps)
            return candidate

        try:
            cls._apply_evidence_snapshot(
                candidate,
                row=row,
                decision_cutoff=decision_cutoff,
                structured_inputs=structured_inputs,
                evidence_bundle=evidence_bundle,
            )
        except (KeyError, TypeError, ValueError):
            candidate["audit_gaps"].append("evidence_snapshot_mapping_invalid")
            return candidate
        candidate["audit_gaps"] = [
            gap
            for gap in candidate["audit_gaps"]
            if gap not in _RECOMMENDATION_TIME_GAPS
        ]
        return candidate

    @classmethod
    def _evidence_snapshot_gaps(
        cls,
        row: dict[str, Any],
        *,
        decision_cutoff: datetime | None,
        reporting_currency: str,
        structured_inputs: dict[str, Any] | None,
        evidence_bundle: dict[str, Any] | None,
    ) -> list[str]:
        gaps: list[str] = []
        if row.get("evidence_schema_version") != _EVIDENCE_SNAPSHOT_SCHEMA_VERSION:
            gaps.append("evidence_snapshot_schema_mismatch")
        if structured_inputs is None or evidence_bundle is None:
            gaps.append("evidence_snapshot_json_invalid")
            return gaps
        try:
            if row.get("decision_input_hash") != _sha256_json(structured_inputs):
                gaps.append("decision_input_hash_mismatch")
            if row.get("evidence_bundle_hash") != _sha256_json(evidence_bundle):
                gaps.append("evidence_bundle_hash_mismatch")
        except (TypeError, ValueError):
            gaps.append("evidence_snapshot_json_invalid")
            return gaps

        required_envelopes = (
            "account",
            "position",
            "instrument",
            "benchmark",
            "fx",
            "risk_policy",
            "risk_budget",
            "cost_model",
            "decision_rationale",
        )
        if any(
            cls._envelope(evidence_bundle, key) is None
            for key in required_envelopes
        ):
            gaps.append("evidence_bundle_incomplete")
        if not cls._structured_evidence_references_match(
            structured_inputs,
            evidence_bundle,
        ):
            gaps.append("evidence_reference_mismatch")

        blockers = cls._json_list(row.get("evidence_blockers_json"))
        if row.get("evidence_readiness_status") != "complete":
            gaps.append("evidence_snapshot_not_complete")
        if blockers:
            gaps.append("evidence_snapshot_blocked")
        if row.get("evidence_signal_id") != row.get("signal_id"):
            gaps.append("evidence_snapshot_signal_mismatch")
        if row.get("evidence_quality_context_id") not in (None, row.get("context_id")):
            gaps.append("evidence_snapshot_quality_context_mismatch")
        if (
            not cls._is_sha256(str(row.get("evidence_snapshot_hash") or ""))
            or row.get("evidence_snapshot_hash") != row.get("frozen_snapshot_hash")
        ):
            gaps.append("evidence_snapshot_hash_mismatch")
        if structured_inputs.get("research_snapshot_hash") != row.get(
            "evidence_snapshot_hash"
        ):
            gaps.append("structured_snapshot_hash_mismatch")
        if cls._stored_utc_datetime(row.get("evidence_created_at")) is None:
            gaps.append("evidence_snapshot_created_time_invalid")

        evidence_cutoff = cls._stored_utc_datetime(row.get("evidence_decision_cutoff"))
        if decision_cutoff is None or evidence_cutoff != decision_cutoff:
            gaps.append("evidence_decision_cutoff_mismatch")
        if str(row.get("evidence_reporting_currency") or "").strip().upper() != reporting_currency:
            gaps.append("evidence_reporting_currency_mismatch")

        strategy = (
            row.get("evidence_strategy_key"),
            row.get("evidence_strategy_version"),
            row.get("evidence_strategy_manifest_hash"),
        )
        if (
            not cls._is_sha256(str(strategy[2] or ""))
            or strategy
            != (
                structured_inputs.get("strategy_key"),
                structured_inputs.get("strategy_version"),
                structured_inputs.get("strategy_manifest_hash"),
            )
        ):
            gaps.append("strategy_binding_mismatch")

        cost_model = cls._envelope(evidence_bundle, "cost_model")
        if (
            cost_model is None
            or cost_model.get("source_version") != strategy[1]
            or cost_model.get("source_hash") != strategy[2]
        ):
            gaps.append("cost_model_strategy_mismatch")

        identity = structured_inputs.get("identity")
        structured_instrument = structured_inputs.get("instrument")
        instrument = cls._envelope(evidence_bundle, "instrument")
        instrument_body = instrument.get("body") if instrument is not None else None
        if (
            not isinstance(identity, dict)
            or identity.get("account_id") != row.get("account_id")
            or identity.get("market") != row.get("market")
            or identity.get("symbol") != row.get("stock_code")
        ):
            gaps.append("identity_binding_mismatch")
        if (
            not isinstance(structured_instrument, dict)
            or not isinstance(instrument_body, dict)
            or instrument_body.get("market") != row.get("market")
            or instrument_body.get("symbol") != row.get("stock_code")
            or instrument_body.get("instrument_type") != row.get("instrument_type")
            or not str(instrument_body.get("quote_currency") or "").strip()
            or not str(instrument_body.get("adjustment_identity") or "").strip()
            or instrument_body.get("trade_lot_size") is None
            or structured_instrument.get("instrument_type")
            != instrument_body.get("instrument_type")
            or structured_instrument.get("trade_lot_size")
            != instrument_body.get("trade_lot_size")
            or structured_instrument.get("adjustment_identity")
            != instrument_body.get("adjustment_identity")
        ):
            gaps.append("instrument_evidence_mismatch")

        rationale = cls._envelope(evidence_bundle, "decision_rationale")
        rationale_body = rationale.get("body") if rationale is not None else None
        if (
            not isinstance(rationale_body, dict)
            or rationale_body.get("position_action") != row.get("position_action")
            or rationale_body.get("incremental_action") != row.get("incremental_action")
        ):
            gaps.append("decision_action_mismatch")

        market = str(row.get("market") or "").strip().lower()
        expected_benchmark = _EXPECTED_BENCHMARKS.get(market)
        structured_benchmark = structured_inputs.get("benchmark")
        benchmark = cls._envelope(evidence_bundle, "benchmark")
        benchmark_body = benchmark.get("body") if benchmark is not None else None
        if (
            expected_benchmark is None
            or row.get("benchmark_market") != market
            or row.get("benchmark_code") != expected_benchmark
            or not isinstance(structured_benchmark, dict)
            or structured_benchmark.get("code") != expected_benchmark
            or not isinstance(benchmark_body, dict)
            or benchmark_body.get("market") != market
            or benchmark_body.get("code") != expected_benchmark
            or not str(benchmark_body.get("currency") or "").strip()
            or not str(benchmark_body.get("adjustment_identity") or "").strip()
            or structured_benchmark.get("price") != benchmark_body.get("price")
            or structured_benchmark.get("adjustment_identity")
            != benchmark_body.get("adjustment_identity")
        ):
            gaps.append("benchmark_strategy_mismatch")

        structured_fx = structured_inputs.get("fx")
        fx = cls._envelope(evidence_bundle, "fx")
        fx_body = fx.get("body") if fx is not None else None
        if (
            not isinstance(structured_fx, dict)
            or not isinstance(fx_body, dict)
            or not str(fx_body.get("pair") or "").strip()
            or fx_body.get("rate") is None
            or structured_fx.get("pair") != fx_body.get("pair")
            or structured_fx.get("rate") != fx_body.get("rate")
        ):
            gaps.append("fx_evidence_mismatch")

        if decision_cutoff is not None and cls._bundle_has_future_evidence(
            evidence_bundle,
            cutoff=decision_cutoff,
        ):
            gaps.append("evidence_after_decision_cutoff")
        return list(dict.fromkeys(gaps))

    @classmethod
    def _structured_evidence_references_match(
        cls,
        structured_inputs: dict[str, Any],
        evidence_bundle: dict[str, Any],
    ) -> bool:
        for key in ("account", "position", "instrument", "benchmark", "fx"):
            structured = structured_inputs.get(key)
            envelope = evidence_bundle.get(key)
            if (
                not isinstance(structured, dict)
                or not isinstance(envelope, dict)
                or structured.get("evidence_hash") != _sha256_json(envelope)
            ):
                return False

        risk = structured_inputs.get("risk")
        risk_policy = evidence_bundle.get("risk_policy")
        risk_budget = evidence_bundle.get("risk_budget")
        if (
            not isinstance(risk, dict)
            or not isinstance(risk_policy, dict)
            or not isinstance(risk_budget, dict)
            or risk.get("policy_evidence_hash") != _sha256_json(risk_policy)
            or risk.get("budget_evidence_hash") != _sha256_json(risk_budget)
        ):
            return False

        cost_model = structured_inputs.get("cost_model")
        cost_envelope = evidence_bundle.get("cost_model")
        if (
            not isinstance(cost_model, dict)
            or not isinstance(cost_envelope, dict)
            or cost_model.get("evidence_hash") != _sha256_json(cost_envelope)
        ):
            return False

        research_context = evidence_bundle.get("research_context")
        research_hashes = structured_inputs.get("research_context_hashes")
        if not isinstance(research_context, list) or not isinstance(research_hashes, list):
            return False
        if any(cls._envelope({"item": item}, "item") is None for item in research_context):
            return False
        return research_hashes == [_sha256_json(item) for item in research_context]

    @classmethod
    def _apply_evidence_snapshot(
        cls,
        candidate: dict[str, Any],
        *,
        row: dict[str, Any],
        decision_cutoff: datetime | None,
        structured_inputs: dict[str, Any],
        evidence_bundle: dict[str, Any],
    ) -> None:
        instrument = cls._envelope(evidence_bundle, "instrument")
        benchmark = cls._envelope(evidence_bundle, "benchmark")
        fx = cls._envelope(evidence_bundle, "fx")
        rationale = cls._envelope(evidence_bundle, "decision_rationale")
        cost_model = cls._envelope(evidence_bundle, "cost_model")
        instrument = cast(dict[str, Any], instrument)
        benchmark = cast(dict[str, Any], benchmark)
        fx = cast(dict[str, Any], fx)
        rationale = cast(dict[str, Any], rationale)
        cost_model = cast(dict[str, Any], cost_model)
        instrument_body = instrument["body"]
        benchmark_body = benchmark["body"]
        fx_body = fx["body"]

        candidate["decision"].update(
            {
                "strategy_key": row["evidence_strategy_key"],
                "strategy_version": row["evidence_strategy_version"],
                "strategy_manifest_hash": row["evidence_strategy_manifest_hash"],
                "decision_input_hash": row["decision_input_hash"],
                "source": rationale["source"],
                "source_hash": rationale["source_hash"],
                "as_of": cls._iso_utc(rationale["as_of"]),
            }
        )
        if decision_cutoff is not None:
            candidate["decision"]["decision_cutoff"] = decision_cutoff.isoformat()
        candidate["structured_inputs"] = structured_inputs
        candidate["identity"] = {
            "market": instrument_body["market"],
            "symbol": instrument_body["symbol"],
            "instrument_type": instrument_body["instrument_type"],
            "currency": instrument_body["quote_currency"],
            **cls._evidence_reference(instrument),
        }
        candidate["product"] = {
            "instrument_type": instrument_body["instrument_type"],
            **cls._evidence_reference(instrument),
        }
        candidate["adjustment"] = {
            "identity": instrument_body["adjustment_identity"],
            **cls._evidence_reference(instrument),
        }
        candidate["benchmark"] = {
            "symbol": benchmark_body["code"],
            "currency": benchmark_body["currency"],
            "adjusted_price_identity": benchmark_body["adjustment_identity"],
            **cls._evidence_reference(benchmark),
            "bars": [],
        }
        candidate["fx"] = {
            "pair": fx_body["pair"],
            "rate": fx_body["rate"],
            **cls._evidence_reference(fx),
        }
        candidate["cost_and_trading"] = {
            "lot_size": instrument_body["trade_lot_size"],
            "as_of": cls._iso_utc(max(instrument["as_of"], cost_model["as_of"])),
            "source": "decision-evidence-snapshot",
            "source_hash": row["evidence_bundle_hash"],
        }
        candidate["bars"] = []

    @classmethod
    def _envelope(
        cls,
        evidence_bundle: dict[str, Any],
        key: str,
    ) -> dict[str, Any] | None:
        value = evidence_bundle.get(key)
        if not isinstance(value, dict) or not isinstance(value.get("body"), dict):
            return None
        as_of = cls._aware_datetime(value.get("as_of"))
        source = str(value.get("source") or "").strip()
        source_version = str(value.get("source_version") or "").strip()
        source_hash = str(value.get("source_hash") or "").strip()
        if (
            value.get("schema_version") != "decision-source-envelope-v1"
            or as_of is None
            or not source
            or not source_version
            or not cls._is_sha256(source_hash)
        ):
            return None
        return {
            **value,
            "as_of": as_of,
            "source": source,
            "source_version": source_version,
            "source_hash": source_hash,
        }

    @staticmethod
    def _evidence_reference(envelope: dict[str, Any]) -> dict[str, Any]:
        return {
            "as_of": PortfolioStrategySourceExportService._iso_utc(envelope["as_of"]),
            "source": envelope["source"],
            "source_hash": envelope["source_hash"],
        }

    @classmethod
    def _bundle_has_future_evidence(
        cls,
        value: Any,
        *,
        cutoff: datetime,
    ) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "as_of":
                    parsed = cls._aware_datetime(nested)
                    if parsed is None or parsed.astimezone(timezone.utc) > cutoff:
                        return True
                if cls._bundle_has_future_evidence(nested, cutoff=cutoff):
                    return True
        elif isinstance(value, list):
            return any(cls._bundle_has_future_evidence(item, cutoff=cutoff) for item in value)
        return False

    @staticmethod
    def _iso_utc(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any] | None:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

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
            ("evidence_created_at", "evidence_snapshot_created_after_frozen_at"),
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
