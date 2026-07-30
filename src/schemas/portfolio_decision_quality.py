# -*- coding: utf-8 -*-
"""Pure contracts for frozen portfolio decision-quality events."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from numbers import Real
from typing import Any


POSITION_ACTIONS = frozenset({"hold", "reduce", "exit"})
INCREMENTAL_ACTIONS = frozenset({"add_in_batches", "wait", "no_add"})
QUALITY_HORIZONS = {"5d": 5, "20d": 20, "60d": 60}
ATTRIBUTION_CATEGORIES = frozenset(
    {
        "fact_error",
        "evidence_error",
        "thesis_error",
        "valuation_error",
        "timing_error",
        "risk_error",
        "execution_error",
        "unattributed",
    }
)
ATTRIBUTION_STATUSES = frozenset({"proposed", "confirmed", "rejected"})


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SET_LIKE_FIELDS = frozenset({"opposing_evidence", "supporting_evidence", "watch_conditions"})
_MATERIAL_FIELDS = (
    "account_id",
    "market",
    "stock_code",
    "instrument_type",
    "position_action",
    "incremental_action",
    "confidence_by_horizon",
    "supporting_evidence",
    "opposing_evidence",
    "invalidation",
    "watch_conditions",
    "trigger_contract",
    "next_review",
    "benchmark",
    "evidence_version",
    "decision_profile",
    "decision_version",
    "strategy_version",
)


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    return normalized or None


def _action(payload: Mapping[str, Any], field: str, allowed: frozenset[str]) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return value


def _sequence(payload: Mapping[str, Any], field: str) -> list[Any]:
    value = payload.get(field)
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence")
    return deepcopy(list(value))


def _confidence(payload: Mapping[str, Any]) -> dict[str, float]:
    value = payload.get("confidence_by_horizon")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("confidence_by_horizon must be a mapping")

    unknown_horizons = set(value) - set(QUALITY_HORIZONS)
    if unknown_horizons:
        raise ValueError(f"confidence_by_horizon contains unknown horizons: {sorted(unknown_horizons)}")

    normalized: dict[str, float] = {}
    for horizon in QUALITY_HORIZONS:
        if horizon not in value:
            continue
        confidence = value[horizon]
        if isinstance(confidence, bool) or not isinstance(confidence, Real):
            raise TypeError(f"confidence_by_horizon[{horizon}] must be numeric")
        numeric = float(confidence)
        if not math.isfinite(numeric) or not 0 <= numeric <= 1:
            raise ValueError(f"confidence_by_horizon[{horizon}] must be between 0 and 1")
        normalized[horizon] = numeric
    return normalized


def _benchmark(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    value = payload.get("benchmark")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("benchmark must be a mapping")
    return deepcopy(dict(value))


def normalize_portfolio_decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize known decision fields without inferring missing evidence."""

    if not isinstance(payload, Mapping):
        raise TypeError("portfolio decision payload must be a mapping")

    snapshot_hash = _optional_text(payload, "frozen_snapshot_hash")
    instrument_type = _optional_text(payload, "instrument_type")
    normalized = {
        "account_id": deepcopy(payload.get("account_id")),
        "market": _optional_text(payload, "market"),
        "stock_code": _optional_text(payload, "stock_code"),
        "instrument_type": instrument_type.lower() if instrument_type else None,
        "frozen_snapshot_hash": snapshot_hash.lower() if snapshot_hash else None,
        "position_action": _action(payload, "position_action", POSITION_ACTIONS),
        "incremental_action": _action(payload, "incremental_action", INCREMENTAL_ACTIONS),
        "confidence_by_horizon": _confidence(payload),
        "supporting_evidence": _sequence(payload, "supporting_evidence"),
        "opposing_evidence": _sequence(payload, "opposing_evidence"),
        "invalidation": _optional_text(payload, "invalidation"),
        "watch_conditions": _sequence(payload, "watch_conditions"),
        "trigger_contract": deepcopy(payload.get("trigger_contract")),
        "next_review": _optional_text(payload, "next_review"),
        "benchmark": _benchmark(payload),
        "evidence_cutoff": _optional_text(payload, "evidence_cutoff"),
        "evidence_version": _optional_text(payload, "evidence_version"),
        "decision_profile": _optional_text(payload, "decision_profile"),
        "decision_version": _optional_text(payload, "decision_version"),
        "strategy_version": _optional_text(payload, "strategy_version"),
    }
    return normalized


def is_materially_evaluable(payload: Mapping[str, Any]) -> list[str]:
    """Return deterministic blocker codes for missing material context."""

    decision = normalize_portfolio_decision(payload)
    blockers: list[str] = []

    if decision["account_id"] in (None, ""):
        blockers.append("account_id_missing")
    if not decision["market"] or not decision["stock_code"]:
        blockers.append("instrument_identity_missing")
    if not decision["instrument_type"]:
        blockers.append("instrument_type_missing")

    snapshot_hash = decision["frozen_snapshot_hash"]
    if not snapshot_hash:
        blockers.append("frozen_snapshot_hash_missing")
    elif not _SHA256_RE.fullmatch(snapshot_hash):
        blockers.append("frozen_snapshot_hash_invalid")

    if not decision["position_action"]:
        blockers.append("position_action_missing")
    if not decision["incremental_action"]:
        blockers.append("incremental_action_missing")

    benchmark = decision["benchmark"]
    if not benchmark or not benchmark.get("market") or not benchmark.get("code") or not benchmark.get("type"):
        blockers.append("benchmark_identity_missing")
    if not decision["evidence_cutoff"]:
        blockers.append("evidence_cutoff_missing")
    if not decision["evidence_version"]:
        blockers.append("evidence_version_missing")
    if not decision["decision_profile"]:
        blockers.append("decision_profile_missing")
    if not decision["decision_version"]:
        blockers.append("decision_version_missing")
    if not decision["strategy_version"]:
        blockers.append("strategy_version_missing")
    if not decision["invalidation"]:
        blockers.append("invalidation_missing")
    if not decision["next_review"]:
        blockers.append("next_review_missing")
    if set(decision["confidence_by_horizon"]) != set(QUALITY_HORIZONS):
        blockers.append("confidence_horizons_incomplete")
    return blockers


def _canonicalize(value: Any, *, field: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_canonicalize(item) for item in value]
        if field in _SET_LIKE_FIELDS:
            keyed = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False): item
                for item in items
            }
            return [keyed[key] for key in sorted(keyed)]
        return items
    return value


def material_event_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash only recommendation-time fields that define a material event."""

    decision = normalize_portfolio_decision(payload)
    material = {field: decision[field] for field in _MATERIAL_FIELDS}
    canonical = _canonicalize(material)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def format_portfolio_decision_prompt_contract(
    portfolio_context: Mapping[str, Any],
    *,
    report_language: str = "zh",
) -> str:
    """Describe the explicit two-axis output without inventing sizing authority."""

    sizing_allowed = (
        portfolio_context.get("risk_budget_evaluated") is True
        and isinstance(portfolio_context.get("sizing_contract"), Mapping)
        and portfolio_context.get("trade_lot_size") is not None
    )
    schema = (
        'dashboard.portfolio_decision = {"position_action":"hold|reduce|exit",'
        '"incremental_action":"add_in_batches|wait|no_add",'
        '"confidence_by_horizon":{"5d":0.0,"20d":0.0,"60d":0.0},'
        '"supporting_evidence":[],"opposing_evidence":[],"invalidation":"",'
        '"watch_conditions":[],"next_review":""}'
    )
    if report_language == "zh":
        lines = [
            "[持仓决策质量输出契约]",
            schema,
            "必须分别判断当前持仓处理与新增资金动作，不得从旧 action 推断 add_in_batches。",
            "5d/20d/60d 置信度、支持/反对证据、失效条件、观察条件和下次复查点均须基于当前证据。",
            "分析师自行设定的阈值必须标为 PROVISIONAL。",
        ]
        if not sizing_allowed:
            lines.append("风险预算或既有 sizing/unit contract 不完整：不得给出仓位比例或建议数量。")
    else:
        lines = [
            "[Portfolio decision-quality output contract]",
            schema,
            "Evaluate position handling and incremental capital independently; never infer add_in_batches from legacy action.",
            "Ground 5d/20d/60d confidence, supporting/opposing evidence, invalidation, watch conditions, and next review in current evidence.",
            "Label every analyst-defined threshold PROVISIONAL.",
        ]
        if not sizing_allowed:
            lines.append("Risk budget or the existing sizing/unit contract is incomplete: do not output position percentages or quantities.")
    return "\n".join(lines)
