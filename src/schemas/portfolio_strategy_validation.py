# -*- coding: utf-8 -*-
"""Pure contracts for auditable portfolio strategy validation manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any


STRATEGY_STATUSES = frozenset({"draft", "shadow", "champion", "retired"})
MODEL_IDENTITY_STATUSES = frozenset({"verified", "model_identity_unverified"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_STRATEGY_FIELDS = (
    "strategy_id",
    "strategy_version",
    "status",
    "policy_hash",
    "prompt_template_hash",
    "model_descriptor",
    "generation_config_hash",
    "code_commit",
    "snapshot_schema_version",
    "decision_contract_version",
    "outcome_engine_version",
    "benchmark_mapping_version",
    "cost_model_version",
    "effective_from",
    "created_at",
    "approved_by",
    "approved_at",
)
_HASH_FIELDS = (
    "policy_hash",
    "prompt_template_hash",
    "generation_config_hash",
)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) != "manifest_hash"
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, datetime):
        return _format_timestamp(_parse_timestamp(value, field="timestamp"))
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field}_invalid") from exc
    else:
        raise ValueError(f"{field}_missing")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def strategy_manifest_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 for a strategy manifest without self-hashing."""

    if not isinstance(payload, Mapping):
        raise TypeError("strategy manifest must be a mapping")
    encoded = json.dumps(
        _canonicalize(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_strategy_manifest(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate a complete strategy version and return a deeply read-only copy."""

    if not isinstance(payload, Mapping):
        raise TypeError("strategy manifest must be a mapping")
    missing = [field for field in _REQUIRED_STRATEGY_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"strategy_manifest_fields_missing:{','.join(missing)}")

    normalized = deepcopy(dict(payload))
    for field in _REQUIRED_STRATEGY_FIELDS:
        if field in {"approved_by", "approved_at"}:
            continue
        if normalized[field] in (None, ""):
            raise ValueError(f"{field}_missing")

    status = str(normalized["status"]).strip().lower()
    if status not in STRATEGY_STATUSES:
        raise ValueError(f"status must be one of {sorted(STRATEGY_STATUSES)}")
    normalized["status"] = status

    for field in _HASH_FIELDS:
        value = str(normalized[field]).strip().lower()
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{field}_invalid")
        normalized[field] = value

    model = normalized["model_descriptor"]
    if not isinstance(model, Mapping):
        raise TypeError("model_descriptor must be a mapping")
    identity_status = str(model.get("identity_status") or "").strip()
    if identity_status not in MODEL_IDENTITY_STATUSES:
        raise ValueError("model_descriptor.identity_status_invalid")
    if identity_status == "verified" and not model.get("resolved_model"):
        raise ValueError("model_descriptor.resolved_model_missing")
    normalized["model_descriptor"] = dict(model)

    normalized["effective_from"] = _format_timestamp(
        _parse_timestamp(normalized["effective_from"], field="effective_from")
    )
    normalized["created_at"] = _format_timestamp(
        _parse_timestamp(normalized["created_at"], field="created_at")
    )

    approved_by = normalized.get("approved_by")
    approved_at = normalized.get("approved_at")
    if status == "champion" and (not approved_by or not approved_at):
        raise ValueError("approved_by and approved_at are required for champion status")
    if (approved_by is None) != (approved_at is None):
        raise ValueError("approved_by and approved_at must be provided together")
    if approved_at is not None:
        normalized["approved_at"] = _format_timestamp(
            _parse_timestamp(approved_at, field="approved_at")
        )

    supplied_hash = normalized.pop("manifest_hash", None)
    computed_hash = strategy_manifest_hash(normalized)
    if supplied_hash is not None and supplied_hash != computed_hash:
        raise ValueError("manifest_hash_mismatch")
    normalized["manifest_hash"] = computed_hash
    return _deep_freeze(normalized)


def validate_point_in_time_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reject evidence that was not fully known by the validation cutoff."""

    if not isinstance(payload, Mapping):
        raise TypeError("validation manifest must be a mapping")
    normalized = deepcopy(dict(payload))
    cutoff = _parse_timestamp(normalized.get("cutoff"), field="cutoff")
    artifacts = normalized.get("evidence_artifacts", [])
    if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
        raise TypeError("evidence_artifacts must be a sequence")

    validated_artifacts: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise TypeError(f"evidence_artifacts[{index}] must be a mapping")
        item = deepcopy(dict(artifact))
        observed_at = _parse_timestamp(
            item.get("observed_at"),
            field=f"evidence_artifacts[{index}].observed_at",
        )
        if observed_at > cutoff:
            artifact_id = item.get("artifact_id", index)
            raise ValueError(f"evidence_after_cutoff:{artifact_id}")
        item["observed_at"] = _format_timestamp(observed_at)
        validated_artifacts.append(item)

    normalized["cutoff"] = _format_timestamp(cutoff)
    normalized["evidence_artifacts"] = validated_artifacts
    return normalized
