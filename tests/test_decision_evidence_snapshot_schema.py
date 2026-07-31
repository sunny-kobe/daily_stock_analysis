# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from src.schemas.decision_evidence_snapshot import (
    DecisionEvidenceSnapshot,
    canonical_json,
)


def _payload(**overrides):
    payload = {
        "schema_version": "decision-evidence-snapshot-v1",
        "signal_id": 101,
        "strategy_key": "portfolio-current-policy",
        "strategy_version": "1.0.0",
        "strategy_manifest_hash": "a" * 64,
        "decision_cutoff": "2026-07-31T08:00:00Z",
        "reporting_currency": "CNY",
        "structured_inputs": {
            "symbol": "600519",
            "account_id": 1,
            "market": "cn",
        },
        "evidence_bundle": {
            "source": "行情",
            "benchmark": {"symbol": "000300"},
        },
        "readiness_status": "insufficient_evidence",
        "blockers": ["benchmark_bar_missing"],
        "snapshot_hash": "b" * 64,
    }
    payload.update(overrides)
    return payload


def _sha256(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def test_snapshot_canonicalizes_json_and_recomputes_body_hashes() -> None:
    snapshot = DecisionEvidenceSnapshot.model_validate(
        _payload(
            decision_input_hash="f" * 64,
            evidence_bundle_hash="e" * 64,
        )
    )

    assert canonical_json(snapshot.structured_inputs) == (
        '{"account_id":1,"market":"cn","symbol":"600519"}'
    )
    assert canonical_json(snapshot.evidence_bundle) == (
        '{"benchmark":{"symbol":"000300"},"source":"行情"}'
    )
    assert snapshot.decision_input_hash == _sha256(snapshot.structured_inputs)
    assert snapshot.evidence_bundle_hash == _sha256(snapshot.evidence_bundle)
    assert snapshot.decision_input_hash != "f" * 64
    assert snapshot.evidence_bundle_hash != "e" * 64

    fields = snapshot.to_record_fields()
    assert fields["structured_inputs_json"] == canonical_json(snapshot.structured_inputs)
    assert fields["evidence_bundle_json"] == canonical_json(snapshot.evidence_bundle)
    assert fields["blockers_json"] == '["benchmark_bar_missing"]'
    assert fields["decision_cutoff"].tzinfo is None


def test_insufficient_blockers_are_trimmed_deduplicated_and_sorted() -> None:
    snapshot = DecisionEvidenceSnapshot.model_validate(
        _payload(
            blockers=[
                " price_missing ",
                "benchmark_bar_missing",
                "price_missing",
            ]
        )
    )

    assert snapshot.blockers == ["benchmark_bar_missing", "price_missing"]


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"unexpected": True}, "Extra inputs are not permitted"),
        ({"decision_cutoff": "2026-07-31T08:00:00"}, "timezone-aware"),
        ({"strategy_manifest_hash": "short"}, "[Ss]tring should match pattern"),
        ({"snapshot_hash": "short"}, "[Ss]tring should match pattern"),
        ({"strategy_key": "   "}, "must not be empty"),
        ({"strategy_version": "   "}, "must not be empty"),
        ({"structured_inputs": {}}, "at least 1 item"),
        ({"evidence_bundle": {}}, "at least 1 item"),
        ({"structured_inputs": {"price": float("nan")}}, "Out of range float values"),
        ({"readiness_status": "complete", "blockers": ["price_missing"]}, "must be empty"),
        ({"readiness_status": "insufficient_evidence", "blockers": []}, "must include blockers"),
        ({"blockers": [" "]}, "must not be empty"),
    ],
)
def test_snapshot_rejects_invalid_contract(overrides, error: str) -> None:
    with pytest.raises((ValidationError, ValueError), match=error):
        DecisionEvidenceSnapshot.model_validate(_payload(**overrides))


def test_complete_snapshot_requires_no_blockers() -> None:
    snapshot = DecisionEvidenceSnapshot.model_validate(
        _payload(readiness_status="complete", blockers=[])
    )

    assert snapshot.readiness_status == "complete"
    assert snapshot.blockers == []
    assert json.loads(snapshot.to_record_fields()["blockers_json"]) == []


@pytest.mark.parametrize(
    "as_of",
    [
        "2026-07-31T08:00:01Z",
        "2026-07-31T16:00:01+08:00",
    ],
)
def test_snapshot_rejects_nested_evidence_after_decision_cutoff(as_of: str) -> None:
    with pytest.raises(ValidationError, match="evidence_after_decision_cutoff"):
        DecisionEvidenceSnapshot.model_validate(
            _payload(
                evidence_bundle={
                    "benchmark": {
                        "as_of": as_of,
                        "source": "exchange",
                    }
                }
            )
        )


def test_snapshot_accepts_nested_evidence_at_or_before_decision_cutoff() -> None:
    snapshot = DecisionEvidenceSnapshot.model_validate(
        _payload(
            evidence_bundle={
                "benchmark": {
                    "as_of": "2026-07-31T08:00:00Z",
                    "source": "exchange",
                },
                "identity": [
                    {
                        "as_of": "2026-07-30T08:00:00Z",
                        "source": "issuer",
                    }
                ],
            }
        )
    )

    assert snapshot.evidence_bundle["benchmark"]["as_of"] == "2026-07-31T08:00:00Z"


@pytest.mark.parametrize("as_of", ["not-a-time", "2026-07-31T08:00:00"])
def test_snapshot_rejects_unverifiable_evidence_time(as_of: str) -> None:
    with pytest.raises(ValidationError, match="evidence_as_of_invalid"):
        DecisionEvidenceSnapshot.model_validate(
            _payload(evidence_bundle={"identity": {"as_of": as_of}})
        )


def test_snapshot_rejects_future_evidence_nested_in_tuple() -> None:
    with pytest.raises(ValidationError, match="evidence_after_decision_cutoff"):
        DecisionEvidenceSnapshot.model_validate(
            _payload(
                evidence_bundle={
                    "events": ({"as_of": "2026-07-31T08:00:01Z"},),
                }
            )
        )
