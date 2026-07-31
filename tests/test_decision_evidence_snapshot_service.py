# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.config import Config
from src.repositories.decision_evidence_snapshot_repo import (
    DecisionEvidenceSnapshotRepository,
)
from src.services.decision_evidence_snapshot_service import (
    DecisionEvidenceSnapshotService,
)
from src.services.strategy_registry_service import StrategyRegistryService
from src.storage import DatabaseManager


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'evidence_service.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _manifest() -> dict:
    return json.loads((ROOT / "strategies/portfolio_current_policy_v1.json").read_text())


def _snapshot() -> dict:
    as_of = "2026-07-31T08:00:00Z"
    market_as_of = "2026-07-30T20:00:00Z"
    payload = {
        "schema_version": "portfolio-research-snapshot-v1",
        "cutoff": as_of,
        "point_in_time": {"prospective_decision_eligible": True},
        "accounts": [
            {
                "account_id": 1,
                "base_currency": "CNY",
                "snapshot_date": "2026-07-31",
                "total_cash": 100000.0,
                "total_equity": 500000.0,
                "evidence_source": "portfolio_daily_snapshot",
                "evidence_version": "portfolio-account-v1",
                "evidence_hash": "1" * 64,
            }
        ],
        "positions": [
            {
                "account_id": 1,
                "market": "cn",
                "symbol": "600519",
                "currency": "CNY",
                "quantity": 100.0,
                "last_price": 1500.0,
                "price_available": True,
                "price_stale": False,
                "price_source": "frozen-quote",
                "price_source_version": "quote-v1",
                "price_as_of": market_as_of,
                "price_source_hash": "b" * 64,
                "fx": {
                    "required": False,
                    "available": True,
                    "pair": "CNY/CNY",
                    "rate": 1.0,
                    "as_of": as_of,
                    "source": "identity",
                    "source_version": "fx-identity-v1",
                    "source_hash": "c" * 64,
                    "stale": False,
                },
            }
        ],
        "instruments": [
            {
                "market": "cn",
                "symbol": "600519",
                "quote_currency": "CNY",
                "instrument_type": "equity",
                "trade_lot_size": 100.0,
                "verification_status": "verified",
                "evidence_source": "exchange",
                "evidence_as_of": as_of,
                "evidence_hash": "d" * 64,
                "evidence_version": "instrument-registry-v1",
                "adjustment_identity": "qfq",
            }
        ],
        "benchmarks": [
            {
                "market": "cn",
                "code": "000300",
                "type": "market_index",
                "price": 4100.0,
                "adjustment_identity": "qfq",
                "evidence_source": "exchange",
                "evidence_as_of": market_as_of,
                "evidence_hash": "e" * 64,
                "evidence_version": "benchmark-bar-v1",
            }
        ],
        "risk_policy": {
            "max_single_position_pct": 20.0,
            "updated_at": as_of,
            "evidence_source": "portfolio_risk_policy",
            "evidence_version": "portfolio-risk-v1",
            "evidence_hash": "f" * 64,
        },
        "risk_budget": {
            "evaluated": True,
            "breaches": [],
            "as_of": as_of,
            "evidence_source": "portfolio_risk_budget",
            "evidence_version": "portfolio-risk-budget-v1",
            "evidence_hash": "0" * 64,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["snapshot_hash"] = hashlib.sha256(encoded).hexdigest()
    return payload


def _rehash_snapshot(snapshot: dict) -> None:
    body = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    encoded = json.dumps(
        body,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    snapshot["snapshot_hash"] = hashlib.sha256(encoded).hexdigest()


def _decision() -> dict:
    return {
        "position_action": "hold",
        "incremental_action": "wait",
        "confidence_by_horizon": {"5d": 0.55, "20d": 0.6, "60d": 0.65},
        "supporting_evidence": ["盈利能力稳定"],
        "opposing_evidence": ["估值偏高"],
        "watch_conditions": ["估值回落"],
        "invalidation": "盈利趋势反转",
        "next_review": "下一次财报",
    }


def _signal() -> dict:
    return {
        "id": 101,
        "market": "cn",
        "stock_code": "600519",
        "created_at": "2026-07-31T08:00:00Z",
    }


def _register_strategy(isolated_db) -> dict:
    return StrategyRegistryService(isolated_db).create_version(_manifest())


def _context_snapshot(**body_overrides) -> dict:
    return {
        "decision_evidence": [
            {
                "schema_version": "decision-source-envelope-v1",
                "as_of": "2026-07-31T08:00:00Z",
                "source": "frozen-analysis",
                "source_version": "analysis-v1",
                "source_hash": "7" * 64,
                "body": {"summary": "冻结研究证据", **body_overrides},
            }
        ]
    }


def test_freeze_complete_evidence_requires_registered_matching_strategy(isolated_db) -> None:
    registered = _register_strategy(isolated_db)
    service = DecisionEvidenceSnapshotService(db_manager=isolated_db)

    first = service.freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=_snapshot(),
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )
    repeated = service.freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=_snapshot(),
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert first["status"] == "complete", first["unable_reasons"]
    assert first["display_status"] == "已保存"
    assert first["strategy_key"] == "portfolio-current-policy"
    assert first["strategy_version"] == "1.0.0"
    assert len(first["decision_input_hash"]) == 64
    assert len(first["evidence_hash"]) == 64
    assert first["snapshot_hash"] == _snapshot()["snapshot_hash"]
    assert repeated["snapshot_hash"] == first["snapshot_hash"]
    assert first["created"] is True
    assert repeated["created"] is False

    row = DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(signal_id=101)
    assert row is not None
    assert row.readiness_status == "complete"
    assert registered["status"] == "draft"
    assert registered["manifest_hash"] == row.strategy_manifest_hash
    structured_inputs = json.loads(row.structured_inputs_json)
    evidence_bundle = json.loads(row.evidence_bundle_json)
    assert "position_action" not in structured_inputs
    assert structured_inputs["position"]["quantity"] == 100.0
    assert structured_inputs["benchmark"]["code"] == "000300"
    assert structured_inputs["fx"]["rate"] == 1.0
    for key in (
        "account",
        "position",
        "instrument",
        "benchmark",
        "fx",
        "risk_policy",
        "risk_budget",
        "cost_model",
    ):
        assert set(evidence_bundle[key]) == {
            "schema_version",
            "as_of",
            "source",
            "source_version",
            "source_hash",
            "body",
        }


def test_freeze_missing_strategy_is_insufficient_and_does_not_register_it(isolated_db) -> None:
    service = DecisionEvidenceSnapshotService(db_manager=isolated_db)

    result = service.freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=_snapshot(),
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert result["unable_reasons"] == ["strategy_version_missing"]
    assert StrategyRegistryService(isolated_db).list_versions() == []


def test_freeze_rejects_registered_strategy_manifest_hash_drift(
    isolated_db,
    tmp_path,
) -> None:
    _register_strategy(isolated_db)
    changed_manifest = _manifest()
    changed_manifest["change_summary"] = "未经登记的同版本策略内容"
    changed_path = tmp_path / "changed-strategy.json"
    changed_path.write_text(json.dumps(changed_manifest), encoding="utf-8")

    result = DecisionEvidenceSnapshotService(
        db_manager=isolated_db,
        strategy_manifest_path=changed_path,
    ).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=_snapshot(),
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert result["unable_reasons"] == ["strategy_manifest_hash_mismatch"]


def test_freeze_missing_benchmark_is_persisted_as_insufficient(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["benchmarks"] = []

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert result["display_status"] == "资料不足"
    assert "benchmark_evidence_missing" in result["unable_reasons"]


def test_freeze_uses_manifest_benchmark_and_rejects_caller_strategy_override(isolated_db) -> None:
    _register_strategy(isolated_db)
    decision = _decision()
    decision["strategy_key"] = "ai-invented-policy"
    snapshot = _snapshot()
    snapshot["benchmarks"][0]["code"] = "000001"

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=decision,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["strategy_key"] == _manifest()["strategy_key"]
    assert "benchmark_identity_mismatch" in result["unable_reasons"]


def test_freeze_rejects_non_identity_fx_rate_for_same_currency(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["positions"][0]["fx"]["rate"] = 0.99

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "fx_identity_rate_invalid" in result["unable_reasons"]


@pytest.mark.parametrize(
    ("pair", "rate", "expected_blocker"),
    [
        ("USD/CNY", 7.2, "fx_pair_mismatch"),
        ("CNY/CNY", 0.0, "fx_rate_invalid"),
    ],
)
def test_freeze_rejects_invalid_fx_contract(
    isolated_db,
    pair: str,
    rate: float,
    expected_blocker: str,
) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["positions"][0]["fx"].update({"pair": pair, "rate": rate})

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert expected_blocker in result["unable_reasons"]


def test_freeze_requires_daily_reset_product_terms(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["instruments"][0]["instrument_type"] = "daily_leveraged_product"

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "daily_reset_product_evidence_incomplete" in result["unable_reasons"]


def test_freeze_does_not_inherit_unrelated_instrument_blockers(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["point_in_time"] = {
        "prospective_decision_eligible": False,
        "blockers": ["instrument_registry_after_cutoff"],
    }
    snapshot["hard_blockers"] = [
        {
            "code": "instrument_identity_missing",
            "scope": "instrument",
            "market": "us",
            "symbol": "MSFT",
        }
    ]
    _rehash_snapshot(snapshot)

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "complete", result["unable_reasons"]


def test_freeze_drops_future_market_bars_from_structured_inputs(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["positions"][0]["price_as_of"] = "2026-08-01T08:00:00Z"
    snapshot["benchmarks"][0]["evidence_as_of"] = "2026-08-01T08:00:00Z"
    _rehash_snapshot(snapshot)

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    row = DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(signal_id=101)
    structured_inputs = json.loads(row.structured_inputs_json)
    evidence_bundle = json.loads(row.evidence_bundle_json)
    assert structured_inputs["position"]["price"] is None
    assert structured_inputs["benchmark"]["price"] is None
    assert evidence_bundle["position"]["body"] == {}
    assert evidence_bundle["benchmark"]["body"] == {}


def test_freeze_drops_unfinalized_same_day_market_bars(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["positions"][0]["price_as_of"] = "2026-07-31T00:01:00Z"
    snapshot["benchmarks"][0]["evidence_as_of"] = "2026-07-31T00:01:00Z"
    _rehash_snapshot(snapshot)

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "position_evidence_not_final" in result["unable_reasons"]
    assert "benchmark_evidence_not_final" in result["unable_reasons"]
    row = DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(signal_id=101)
    structured_inputs = json.loads(row.structured_inputs_json)
    evidence_bundle = json.loads(row.evidence_bundle_json)
    assert structured_inputs["position"]["price"] is None
    assert structured_inputs["benchmark"]["price"] is None
    assert evidence_bundle["position"]["body"] == {}
    assert evidence_bundle["benchmark"]["body"] == {}


def test_freeze_rejects_stale_benchmark_even_if_snapshot_flag_is_missing(
    isolated_db,
) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["benchmarks"][0]["evidence_as_of"] = "2026-07-27T08:00:00Z"
    _rehash_snapshot(snapshot)

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "benchmark_evidence_stale" in result["unable_reasons"]


def test_freeze_rejects_stale_fx_even_if_cached_flag_is_false(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["positions"][0]["fx"]["as_of"] = "2026-07-23T08:00:00Z"
    _rehash_snapshot(snapshot)

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "fx_evidence_stale" in result["unable_reasons"]


def test_freeze_persists_invalid_research_hash_as_stable_blocker(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["snapshot_hash"] = "invalid"

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "research_snapshot_hash_invalid" in result["unable_reasons"]
    row = DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(signal_id=101)
    assert row is not None
    assert len(row.snapshot_hash) == 64


def test_assess_is_read_only_and_matches_freeze_readiness(isolated_db) -> None:
    _register_strategy(isolated_db)
    service = DecisionEvidenceSnapshotService(db_manager=isolated_db)

    assessment = service.assess(
        signal={key: value for key, value in _signal().items() if key != "id"},
        portfolio_decision=_decision(),
        research_snapshot=_snapshot(),
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert assessment["status"] == "complete"
    assert assessment["id"] is None
    assert DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(
        signal_id=1
    ) is None


def test_freeze_rejects_future_research_source_without_persisting_future_body(
    isolated_db,
) -> None:
    _register_strategy(isolated_db)

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=_snapshot(),
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(published_at="2026-07-31T08:00:01Z"),
    )

    assert result["status"] == "insufficient_evidence"
    assert "research_context_after_cutoff" in result["unable_reasons"]
    row = DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(signal_id=101)
    assert "published_at" not in row.evidence_bundle_json


def test_freeze_rejects_research_snapshot_hash_drift(isolated_db) -> None:
    _register_strategy(isolated_db)
    snapshot = _snapshot()
    snapshot["accounts"][0]["total_cash"] = 1.0

    result = DecisionEvidenceSnapshotService(db_manager=isolated_db).freeze(
        signal=_signal(),
        portfolio_decision=_decision(),
        research_snapshot=snapshot,
        portfolio_context={"account_id": 1},
        context_snapshot=_context_snapshot(),
    )

    assert result["status"] == "insufficient_evidence"
    assert "research_snapshot_hash_mismatch" in result["unable_reasons"]
