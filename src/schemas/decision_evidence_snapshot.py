# -*- coding: utf-8 -*-
"""Strict contract for immutable decision-time evidence snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "decision-evidence-snapshot-v1"


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically and reject non-standard numeric values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _evidence_as_of_values(value: Any):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key == "as_of":
                yield nested
            yield from _evidence_as_of_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _evidence_as_of_values(nested)


class DecisionEvidenceSnapshot(BaseModel):
    """Validated immutable evidence content for one decision signal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION]
    signal_id: int = Field(gt=0)
    quality_context_id: int | None = Field(default=None, gt=0)
    strategy_key: str = Field(min_length=1, max_length=96)
    strategy_version: str = Field(min_length=1, max_length=32)
    strategy_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_cutoff: datetime
    reporting_currency: str = Field(min_length=1, max_length=16)
    structured_inputs: dict[str, Any] = Field(min_length=1)
    decision_input_hash: str = ""
    evidence_bundle: dict[str, Any] = Field(min_length=1)
    evidence_bundle_hash: str = ""
    readiness_status: Literal["complete", "insufficient_evidence"]
    blockers: list[str]
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("strategy_key", "strategy_version")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("strategy identity must not be empty")
        return normalized

    @field_validator("reporting_currency")
    @classmethod
    def normalize_reporting_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("reporting_currency must not be empty")
        return normalized

    @field_validator("strategy_manifest_hash", "snapshot_hash", mode="before")
    @classmethod
    def normalize_sha256(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("decision_cutoff")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_cutoff must be timezone-aware")
        return value

    @field_validator("blockers", mode="before")
    @classmethod
    def normalize_blockers(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("blockers must be a list")
        normalized: set[str] = set()
        for blocker in value:
            if not isinstance(blocker, str):
                raise TypeError("blockers must contain strings")
            blocker = blocker.strip()
            if not blocker:
                raise ValueError("blocker must not be empty")
            normalized.add(blocker)
        return sorted(normalized)

    @model_validator(mode="before")
    @classmethod
    def discard_caller_body_hashes(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        normalized["decision_input_hash"] = ""
        normalized["evidence_bundle_hash"] = ""
        return normalized

    @model_validator(mode="after")
    def validate_readiness_and_compute_hashes(self) -> "DecisionEvidenceSnapshot":
        if self.readiness_status == "complete" and self.blockers:
            raise ValueError("complete snapshot blockers must be empty")
        if self.readiness_status == "insufficient_evidence" and not self.blockers:
            raise ValueError("insufficient_evidence snapshot must include blockers")

        cutoff = self.decision_cutoff.astimezone(timezone.utc)
        for raw_as_of in _evidence_as_of_values(self.evidence_bundle):
            try:
                as_of = datetime.fromisoformat(str(raw_as_of).replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise ValueError("evidence_as_of_invalid") from exc
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("evidence_as_of_invalid")
            if as_of.astimezone(timezone.utc) > cutoff:
                raise ValueError("evidence_after_decision_cutoff")

        self.decision_input_hash = canonical_json_hash(self.structured_inputs)
        self.evidence_bundle_hash = canonical_json_hash(self.evidence_bundle)
        return self

    def to_record_fields(self) -> dict[str, Any]:
        """Return the exact persistence payload; caller-owned hashes are excluded."""

        cutoff = self.decision_cutoff.astimezone(timezone.utc).replace(tzinfo=None)
        return {
            "signal_id": self.signal_id,
            "quality_context_id": self.quality_context_id,
            "schema_version": self.schema_version,
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "strategy_manifest_hash": self.strategy_manifest_hash,
            "decision_cutoff": cutoff,
            "reporting_currency": self.reporting_currency,
            "structured_inputs_json": canonical_json(self.structured_inputs),
            "decision_input_hash": self.decision_input_hash,
            "evidence_bundle_json": canonical_json(self.evidence_bundle),
            "evidence_bundle_hash": self.evidence_bundle_hash,
            "readiness_status": self.readiness_status,
            "blockers_json": canonical_json(self.blockers),
            "snapshot_hash": self.snapshot_hash,
        }
