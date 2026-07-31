# -*- coding: utf-8 -*-
"""Service tests for DecisionSignal P1."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from math import inf, nan
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.repositories.decision_evidence_snapshot_repo import (
    DecisionEvidenceSnapshotRepository,
)
from src.repositories.decision_signal_repo import DecisionSignalCreateResult
from src.services.decision_signal_service import DecisionSignalService, DecisionSignalStorageError
from src.storage import AnalysisHistory, DatabaseManager, DecisionSignalRecord, utc_naive_now
from src.utils.sanitize import sanitize_decision_signal_text, sanitize_diagnostic_text


def test_service_imports_without_api_bootstrap() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.services.decision_signal_service import DecisionSignalService; "
            "print(DecisionSignalService.__name__)",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "DecisionSignalService" in result.stdout


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    db_path = tmp_path / "decision_signal_service.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_database_path


def _payload(**overrides):
    payload = {
        "stock_code": "SH600519",
        "stock_name": "贵州茅台",
        "market": "cn",
        "source_type": "analysis",
        "source_report_id": 101,
        "trace_id": "trace-101",
        "market_phase": "intraday",
        "trigger_source": "api",
        "action": "buy",
        "confidence": 0.72,
        "score": 83,
        "horizon": "3d",
        "reason": "放量突破",
    }
    payload.update(overrides)
    return payload


def _portfolio_decision(**overrides):
    decision = {
        "position_action": "hold",
        "incremental_action": "wait",
        "confidence_by_horizon": {"5d": 0.55, "20d": 0.68, "60d": 0.61},
        "supporting_evidence": ["持仓逻辑未变"],
        "opposing_evidence": ["估值仍高"],
        "invalidation": "基本面证据被证伪",
        "watch_conditions": ["估值进入已验证区间"],
        "next_review": "下一次财报",
        "benchmark": {"market": "cn", "code": "000300", "type": "market_index"},
        "decision_version": "portfolio-decision-v1",
    }
    decision.update(overrides)
    return decision


class _CompleteEvidenceService:
    @staticmethod
    def _summary(snapshot_id):
        return {
            "id": snapshot_id,
            "status": "complete",
            "display_status": "已保存",
            "strategy_key": "portfolio-current-policy",
            "strategy_version": "1.0.0",
            "snapshot_hash": "a" * 64,
            "evidence_hash": "b" * 64,
            "decision_input_hash": "c" * 64,
            "unable_reasons": [],
        }

    def assess(self, **_kwargs):
        return self._summary(None)

    def freeze(self, **_kwargs):
        return self._summary(1)

    def get_summary(self, *, signal_id):
        return self._summary(signal_id)


def test_create_gated_signal_persists_frozen_snapshot_identity(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    research_snapshot = {
        "snapshot_hash": "a" * 64,
        "cutoff": "2026-07-22T09:00:00",
        "accounts": [],
        "positions": [],
        "instruments": [
            {
                "symbol": "600519",
                "market": "cn",
                "quote_currency": "CNY",
                "instrument_type": "equity",
                "trade_lot_size": 100,
                "verification_status": "verified",
                "requires_premium_check": False,
            }
        ],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(metadata={"suggested_trade_quantity": 100}),
        research_snapshot=research_snapshot,
        context_snapshot={
            "analysis_context_pack_overview": {
                "blocks": [{"key": "quote", "status": "available"}],
            }
        },
    )

    item = created["item"]
    assert item["action"] == "buy"
    assert item["metadata"]["portfolio_snapshot_hash"] == "a" * 64
    assert item["metadata"]["portfolio_snapshot_cutoff"] == "2026-07-22T09:00:00"
    assert item["metadata"]["portfolio_gate"]["completeness"] == "COMPLETE"


def test_create_gated_signal_fails_closed_before_persistence(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    research_snapshot = {
        "snapshot_hash": "b" * 64,
        "cutoff": "2026-07-22T09:00:00",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "risk_policy": None,
    }

    created = service.create_gated_signal(
        _payload(source_report_id=102, trace_id="trace-102"),
        research_snapshot=research_snapshot,
    )

    item = created["item"]
    assert item["action"] == "alert"
    assert item["metadata"]["raw_action"] == "buy"
    assert "instrument_identity_missing" in item["metadata"]["portfolio_gate"]["hard_blockers"]
    assert "decision_price_missing" in item["metadata"]["portfolio_gate"]["hard_blockers"]


def test_create_gated_signal_freezes_quality_context_after_signal_persistence(isolated_db) -> None:
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=_CompleteEvidenceService(),
    )
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "c" * 64,
        "cutoff": "2026-07-25T09:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [
            {
                "symbol": "600519",
                "market": "cn",
                "instrument_type": "equity",
            }
        ],
        "benchmarks": [
            {"market": "cn", "code": "000300", "type": "market_index"}
        ],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(
            source_report_id=103,
            trace_id="trace-quality-103",
            metadata={
                "portfolio_decision": _portfolio_decision(),
                "quality_context_status": "pending",
            },
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2, "quantity": 100},
    )

    item = created["item"]
    assert item["action"] == "hold"
    assert item["metadata"]["quality_context_status"] == "complete"
    assert item["metadata"]["quality_context_created"] is True
    context = DecisionQualityRepository(isolated_db).get_context_by_signal(signal_id=item["id"])
    assert context is not None
    assert context.frozen_snapshot_hash == "c" * 64
    assert context.instrument_type == "equity"
    assert context.material_event_fingerprint == item["metadata"]["quality_context_fingerprint"]


def test_create_gated_signal_freezes_decision_evidence_sidecar(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "9" * 64,
        "cutoff": "2026-07-31T08:00:00Z",
        "point_in_time": {"prospective_decision_eligible": True},
        "accounts": [
            {
                "account_id": 2,
                "base_currency": "CNY",
                "snapshot_date": "2026-07-31",
                "total_cash": 1000.0,
                "total_equity": 10000.0,
            }
        ],
        "positions": [],
        "instruments": [],
        "benchmarks": [],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(
            source_report_id=107,
            trace_id="trace-evidence-107",
            metadata={"portfolio_decision": _portfolio_decision()},
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2, "quantity": 100},
        context_snapshot={"evidence_sources": ["frozen-analysis"]},
    )

    item = created["item"]
    row = DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(
        signal_id=item["id"]
    )
    assert row is not None
    assert row.readiness_status == "insufficient_evidence"
    assert item["metadata"]["decision_evidence_status"] == "insufficient_evidence"
    assert item["metadata"]["decision_evidence_display_status"] == "资料不足"
    assert item["metadata"]["decision_evidence_snapshot_id"] == row.id
    assert item["metadata"]["quality_context_status"] == "insufficient_evidence"
    assert DecisionQualityRepository(isolated_db).get_context_by_signal(
        signal_id=item["id"]
    ) is None


def test_create_gated_signal_persists_evidence_before_complete_quality_context(
    isolated_db,
) -> None:
    calls: list[str] = []

    class CompleteEvidenceService:
        def assess(self, **_kwargs):
            calls.append("assess")
            return {
                "id": None,
                "status": "complete",
                "display_status": "已保存",
                "strategy_key": "portfolio-current-policy",
                "strategy_version": "1.0.0",
                "snapshot_hash": "a" * 64,
                "evidence_hash": "b" * 64,
                "unable_reasons": [],
            }

        def freeze(self, **_kwargs):
            calls.append("freeze")
            return {
                "id": 9,
                "status": "complete",
                "display_status": "已保存",
                "strategy_key": "portfolio-current-policy",
                "strategy_version": "1.0.0",
                "snapshot_hash": "a" * 64,
                "evidence_hash": "b" * 64,
                "unable_reasons": [],
            }

    class QualityService:
        def freeze_context(self, **kwargs):
            calls.append("quality")
            assert kwargs["evidence_unable_reasons"] == []
            return {
                "context_id": 10,
                "signal_id": kwargs["signal"]["id"],
                "material_event_fingerprint": "c" * 64,
                "created": True,
                "status": "complete",
                "unable_reasons": [],
            }

    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=CompleteEvidenceService(),
        decision_quality_service=QualityService(),
    )

    created = service.create_gated_signal(
        _payload(
            source_report_id=109,
            trace_id="trace-evidence-order-109",
            metadata={"portfolio_decision": _portfolio_decision()},
        ),
        research_snapshot={
            "schema_version": "portfolio-research-snapshot-v1",
            "snapshot_hash": "8" * 64,
            "cutoff": "2026-07-31T08:00:00Z",
            "accounts": [],
            "positions": [],
            "instruments": [],
            "benchmarks": [],
            "risk_policy": None,
        },
        portfolio_context={"account_id": 2},
    )

    assert calls == ["assess", "freeze", "quality"]
    assert created["item"]["metadata"]["quality_context_status"] == "complete"


def test_create_gated_signal_surfaces_decision_evidence_write_failure(isolated_db) -> None:
    class FailingEvidenceService:
        def assess(self, **_kwargs):
            return {
                "id": None,
                "status": "complete",
                "display_status": "已保存",
                "strategy_key": "portfolio-current-policy",
                "strategy_version": "1.0.0",
                "snapshot_hash": "a" * 64,
                "evidence_hash": "b" * 64,
                "unable_reasons": [],
            }

        def freeze(self, **_kwargs):
            raise RuntimeError("evidence store unavailable")

    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=FailingEvidenceService(),
    )
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "8" * 64,
        "cutoff": "2026-07-31T08:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "risk_policy": None,
    }

    created = service.create_gated_signal(
        _payload(
            source_report_id=108,
            trace_id="trace-evidence-108",
            metadata={"portfolio_decision": _portfolio_decision()},
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2},
    )

    metadata = created["item"]["metadata"]
    assert metadata["decision_evidence_status"] == "failed"
    assert metadata["decision_evidence_display_status"] == "资料不足"
    assert metadata["decision_evidence_unable_reasons"] == [
        "decision_evidence_write_failed"
    ]
    assert metadata["quality_context_status"] == "insufficient_evidence"
    assert "decision_evidence_write_failed" in metadata[
        "quality_context_unable_reasons"
    ]
    assert DecisionEvidenceSnapshotRepository(isolated_db).get_by_signal_id(
        signal_id=created["item"]["id"]
    ) is None


def test_create_gated_signal_does_not_claim_evidence_saved_before_sidecar_write(
    isolated_db,
) -> None:
    persisted_statuses: list[object] = []

    class FailingEvidenceService:
        def assess(self, **_kwargs):
            return _CompleteEvidenceService._summary(None)

        def freeze(self, **kwargs):
            persisted_statuses.append(
                kwargs["signal"].get("metadata", {}).get("decision_evidence_status")
            )
            raise RuntimeError("evidence store unavailable")

    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=FailingEvidenceService(),
    )

    service.create_gated_signal(
        _payload(
            source_report_id=110,
            trace_id="trace-evidence-window-110",
            metadata={"portfolio_decision": _portfolio_decision()},
        ),
        research_snapshot={
            "schema_version": "portfolio-research-snapshot-v1",
            "snapshot_hash": "7" * 64,
            "cutoff": "2026-07-31T08:00:00Z",
            "accounts": [],
            "positions": [],
            "instruments": [],
            "benchmarks": [],
            "risk_policy": None,
        },
        portfolio_context={"account_id": 2},
    )

    assert persisted_statuses == ["pending"]


def test_duplicate_gated_signal_does_not_backfill_or_rewrite_evidence(isolated_db) -> None:
    class RecordingEvidenceService(_CompleteEvidenceService):
        def __init__(self):
            self.freeze_calls = 0

        def freeze(self, **_kwargs):
            self.freeze_calls += 1
            return self._summary(self.freeze_calls)

    evidence_service = RecordingEvidenceService()
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=evidence_service,
    )
    payload = _payload(
        source_report_id=111,
        trace_id="trace-evidence-duplicate-111",
        metadata={"portfolio_decision": _portfolio_decision()},
    )
    snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "6" * 64,
        "cutoff": "2026-07-31T08:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [
            {
                "symbol": "600519",
                "market": "cn",
                "instrument_type": "equity",
            }
        ],
        "benchmarks": [],
        "risk_policy": None,
    }

    first = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )
    second = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )

    assert first["created"] is True
    assert second["created"] is False
    assert first["item"]["id"] == second["item"]["id"]
    assert evidence_service.freeze_calls == 1


def test_duplicate_retries_matching_failed_evidence_write(isolated_db) -> None:
    class RetryEvidenceService(_CompleteEvidenceService):
        def __init__(self):
            self.freeze_calls = 0

        def freeze(self, **_kwargs):
            self.freeze_calls += 1
            if self.freeze_calls == 1:
                raise RuntimeError("temporary evidence store failure")
            return self._summary(1)

        def get_summary(self, *, signal_id):
            return {
                "signal_id": signal_id,
                "status": "missing",
            }

    evidence_service = RetryEvidenceService()
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=evidence_service,
    )
    payload = _payload(
        source_report_id=112,
        trace_id="trace-evidence-retry-112",
        metadata={"portfolio_decision": _portfolio_decision()},
    )
    snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "5" * 64,
        "cutoff": "2026-07-31T08:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "benchmarks": [],
        "risk_policy": None,
    }

    first = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )
    second = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )

    assert first["item"]["metadata"]["decision_evidence_status"] == "failed"
    assert second["created"] is False
    assert second["item"]["metadata"]["decision_evidence_status"] == "complete"
    assert evidence_service.freeze_calls == 2


def test_expired_failed_evidence_write_is_not_retried(isolated_db) -> None:
    class RetryEvidenceService(_CompleteEvidenceService):
        def __init__(self):
            self.freeze_calls = 0

        def freeze(self, **_kwargs):
            self.freeze_calls += 1
            raise RuntimeError("temporary evidence store failure")

        def get_summary(self, *, signal_id):
            return {"signal_id": signal_id, "status": "missing"}

    evidence_service = RetryEvidenceService()
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=evidence_service,
    )
    payload = _payload(
        source_report_id=114,
        trace_id="trace-evidence-expired-retry-114",
        expires_at=utc_naive_now() + timedelta(days=1),
        metadata={"portfolio_decision": _portfolio_decision()},
    )
    snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "3" * 64,
        "cutoff": "2026-07-31T08:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "benchmarks": [],
        "risk_policy": None,
    }
    first = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )
    with isolated_db.get_session() as session:
        row = session.get(DecisionSignalRecord, first["item"]["id"])
        row.status = "expired"
        row.expires_at = utc_naive_now() - timedelta(minutes=1)
        session.commit()

    second = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )

    assert second["created"] is False
    assert second["item"]["status"] == "expired"
    assert second["item"]["metadata"]["decision_evidence_status"] == "failed"
    assert evidence_service.freeze_calls == 1


def test_expired_evidence_bound_signal_is_not_refreshed_with_old_sidecar(
    isolated_db,
) -> None:
    class RecordingEvidenceService(_CompleteEvidenceService):
        def __init__(self):
            self.freeze_calls = 0

        def freeze(self, **_kwargs):
            self.freeze_calls += 1
            return self._summary(1)

    evidence_service = RecordingEvidenceService()
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=evidence_service,
    )
    payload = _payload(
        source_report_id=113,
        trace_id="trace-evidence-expired-113",
        expires_at=utc_naive_now() + timedelta(days=1),
        metadata={"portfolio_decision": _portfolio_decision()},
    )
    snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "4" * 64,
        "cutoff": "2026-07-31T08:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "benchmarks": [],
        "risk_policy": None,
    }
    first = service.create_gated_signal(
        payload,
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )
    with isolated_db.get_session() as session:
        row = session.get(DecisionSignalRecord, first["item"]["id"])
        row.status = "expired"
        row.expires_at = utc_naive_now() - timedelta(minutes=1)
        session.commit()

    second = service.create_gated_signal(
        {**payload, "expires_at": utc_naive_now() + timedelta(days=2)},
        research_snapshot=snapshot,
        portfolio_context={"account_id": 2},
    )

    assert second["created"] is False
    assert second["item"]["status"] == "expired"
    assert evidence_service.freeze_calls == 1


def test_create_gated_signal_materializes_authoritative_quality_identity(isolated_db) -> None:
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=_CompleteEvidenceService(),
    )
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "e" * 64,
        "cutoff": "2026-07-27T02:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [
            {
                "symbol": "600519",
                "market": "cn",
                "instrument_type": "equity",
            }
        ],
        "benchmarks": [
            {
                "market": "cn",
                "code": "000001",
                "type": "market_index",
                "evidence_source": "dsa_market_profile",
                "evidence_version": "market-profile-v1",
            }
        ],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }
    incomplete_decision = _portfolio_decision(
        benchmark=None,
        decision_version=None,
        frozen_snapshot_hash="stale",
    )

    created = service.create_gated_signal(
        _payload(
            source_report_id=106,
            trace_id="trace-quality-materialized",
            metadata={
                "portfolio_decision": incomplete_decision,
                "quality_context_status": "pending",
            },
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2, "quantity": 100},
    )

    item = created["item"]
    decision = item["metadata"]["portfolio_decision"]
    assert decision["frozen_snapshot_hash"] == "e" * 64
    assert decision["evidence_cutoff"] == "2026-07-27T02:00:00Z"
    assert decision["evidence_version"] == "portfolio-research-snapshot-v1"
    assert decision["decision_version"] == "portfolio-decision-v1"
    assert decision["benchmark"] == research_snapshot["benchmarks"][0]
    assert item["metadata"]["quality_context_status"] == "complete"
    assert item["metadata"]["quality_context_unable_reasons"] == []


def test_materialized_decision_uses_frozen_snapshot_benchmark() -> None:
    snapshot_benchmark = {
        "market": "us",
        "code": "SPY",
        "type": "market_index",
        "evidence_source": "frozen-snapshot",
    }
    snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "e" * 64,
        "cutoff": "2026-07-27T02:00:00Z",
        "instruments": [
            {
                "symbol": "AAPL",
                "market": "us",
                "instrument_type": "equity",
            }
        ],
        "benchmarks": [snapshot_benchmark],
    }
    payload = _payload(
        stock_code="AAPL",
        market="us",
        metadata={
            "portfolio_decision": _portfolio_decision(
                benchmark={"market": "us", "code": "IWM", "type": "market_index"},
            )
        },
    )

    result = DecisionSignalService._materialize_portfolio_decision(
        payload,
        snapshot=snapshot,
        portfolio_context={
            "account_id": 2,
            "benchmark": {"market": "us", "code": "QQQ", "type": "market_index"},
        },
    )

    assert result["metadata"]["portfolio_decision"]["benchmark"] == snapshot_benchmark


def test_create_gated_signal_does_not_reuse_model_benchmark_when_snapshot_has_none(
    isolated_db,
) -> None:
    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_evidence_service=_CompleteEvidenceService(),
    )
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "f" * 64,
        "cutoff": "2026-07-27T02:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [
            {
                "symbol": "AAPL",
                "market": "us",
                "instrument_type": "equity",
            }
        ],
        "benchmarks": [],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(
            stock_code="AAPL",
            market="us",
            source_report_id=111,
            trace_id="trace-quality-missing-benchmark",
            metadata={
                "portfolio_decision": _portfolio_decision(
                    benchmark={"market": "us", "code": "IWM", "type": "market_index"},
                ),
            },
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2, "quantity": 100},
    )

    item = created["item"]
    decision = item["metadata"]["portfolio_decision"]
    assert decision["benchmark"] is None
    assert item["metadata"]["quality_context_status"] == "insufficient_evidence"
    assert "benchmark_identity_missing" in item["metadata"][
        "quality_context_unable_reasons"
    ]
    context = DecisionQualityRepository(isolated_db).get_context_by_signal(
        signal_id=item["id"]
    )
    assert context is not None
    assert context.context_status == "insufficient_evidence"
    assert context.benchmark_code is None


def test_create_gated_signal_does_not_infer_quality_context_from_legacy_action(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "d" * 64,
        "cutoff": "2026-07-25T09:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(
            source_report_id=104,
            trace_id="trace-quality-104",
            metadata={
                "quality_context_status": "insufficient_evidence",
                "quality_context_unable_reasons": ["portfolio_decision_missing"],
            },
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2, "quantity": 100},
    )

    item = created["item"]
    assert item["action"] == "alert"
    assert item["metadata"]["quality_context_status"] == "insufficient_evidence"
    assert DecisionQualityRepository(isolated_db).get_context_by_signal(signal_id=item["id"]) is None


def test_create_gated_signal_marks_invalid_snapshot_hash_as_insufficient_evidence(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "invalid",
        "cutoff": "2026-07-25T09:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(
            source_report_id=105,
            trace_id="trace-quality-invalid-hash",
            metadata={
                "portfolio_decision": _portfolio_decision(),
                "quality_context_status": "pending",
            },
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2, "quantity": 100},
    )

    item = created["item"]
    assert item["metadata"]["quality_context_status"] == "insufficient_evidence"
    assert "frozen_snapshot_hash_invalid" in item["metadata"]["quality_context_unable_reasons"]
    assert DecisionQualityRepository(isolated_db).get_context_by_signal(signal_id=item["id"]) is None


def test_quality_sidecar_failure_does_not_roll_back_saved_signal(isolated_db) -> None:
    class _FailingQualityService:
        def freeze_context(self, **_kwargs):
            raise RuntimeError("sidecar unavailable")

    service = DecisionSignalService(
        db_manager=isolated_db,
        decision_quality_service=_FailingQualityService(),
    )
    research_snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "e" * 64,
        "cutoff": "2026-07-25T09:00:00Z",
        "accounts": [],
        "positions": [],
        "instruments": [],
        "risk_policy": {"max_single_position_pct": 20},
        "risk_budget": {"evaluated": True, "breaches": []},
    }

    created = service.create_gated_signal(
        _payload(
            source_report_id=105,
            trace_id="trace-quality-105",
            metadata={
                "portfolio_decision": _portfolio_decision(),
                "quality_context_status": "pending",
            },
        ),
        research_snapshot=research_snapshot,
        portfolio_context={"account_id": 2},
    )

    item = created["item"]
    assert item["metadata"]["quality_context_status"] == "failed"
    assert service.get_signal(item["id"])["metadata"]["quality_context_status"] == "failed"
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).filter_by(id=item["id"]).count() == 1


def _history_result(**overrides):
    from src.analyzer import AnalysisResult

    result = AnalysisResult(
        code="600519",
        name="贵州茅台",
        sentiment_score=68,
        trend_prediction="震荡偏强",
        operation_advice="持有观察",
        decision_type="hold",
        confidence_level="中",
        analysis_summary="趋势仍在，但等待量能确认。",
        report_language="zh",
    )
    result.dashboard = {
        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "1680",
                "stop_loss": "1600",
            },
            "action_checklist": ["回踩不破支撑"],
        }
    }
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_service_normalizes_fields_and_partial_plan_quality(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    result = service.create_signal(
        _payload(
            entry_low="1680.5",
            stop_loss="1600",
        )
    )

    item = result["item"]
    assert result["created"] is True
    assert item["stock_code"] == "600519"
    assert item["market"] == "cn"
    assert item["action"] == "buy"
    assert item["action_label"] == "买入"
    assert item["confidence"] == 0.72
    assert item["score"] == 83
    assert item["entry_low"] == 1680.5
    assert item["stop_loss"] == 1600.0
    assert item["plan_quality"] == "partial"
    assert item["decision_profile"] == "balanced"
    assert item["metadata"]["decision_profile"] == "balanced"


def test_service_canonicalizes_decision_profile_and_rejects_non_object_metadata(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    caller_metadata = {"decision_profile": "balanced", "task_id": "conflict"}
    top_level = service.create_signal(
        _payload(
            source_report_id=1111,
            trace_id="trace-profile-top-level",
            decision_profile="aggressive",
            metadata=caller_metadata,
        )
    )["item"]
    assert top_level["decision_profile"] == "aggressive"
    assert top_level["metadata"]["decision_profile"] == "aggressive"
    assert top_level["metadata"]["task_id"] == "conflict"
    assert caller_metadata == {
        "decision_profile": "balanced",
        "task_id": "conflict",
    }

    legacy_metadata = service.create_signal(
        _payload(
            source_report_id=1112,
            trace_id="trace-profile-metadata",
            metadata={"decision_profile": "conservative"},
        )
    )["item"]
    assert legacy_metadata["decision_profile"] == "conservative"
    assert legacy_metadata["metadata"]["decision_profile"] == "conservative"

    invalid_metadata_profile = service.create_signal(
        _payload(
            source_report_id=1113,
            trace_id="trace-profile-invalid-metadata",
            metadata={"decision_profile": "balanced-v2"},
        )
    )["item"]
    assert invalid_metadata_profile["decision_profile"] == "balanced"
    assert invalid_metadata_profile["metadata"]["decision_profile"] == "balanced"

    with pytest.raises(ValueError, match="decision_profile"):
        service.create_signal(
            _payload(
                source_report_id=1116,
                trace_id="trace-profile-null-top-level",
                decision_profile=None,
            )
        )

    with pytest.raises(ValueError, match="decision_profile"):
        service.create_signal(
            _payload(
                source_report_id=1117,
                trace_id="trace-profile-empty-top-level",
                decision_profile="",
            )
        )

    with pytest.raises(ValueError, match="decision_profile"):
        service.create_signal(
            _payload(
                source_report_id=1114,
                trace_id="trace-profile-invalid-top-level",
                decision_profile="balanced-v2",
            )
        )

    with pytest.raises(ValueError, match="metadata must be an object"):
        service.create_signal(
            _payload(
                source_report_id=1115,
                trace_id="trace-profile-non-object-metadata",
                metadata=["legacy"],
            )
        )

    null_metadata = service.create_signal(
        _payload(
            source_report_id=1118,
            trace_id="trace-profile-null-metadata",
            metadata=None,
        )
    )["item"]
    assert null_metadata["decision_profile"] == "balanced"
    assert null_metadata["metadata"] == {"decision_profile": "balanced"}


def test_service_defaults_lifecycle_and_preserves_explicit_values(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    intraday_payload = _payload(
        source_report_id=151,
        trace_id="trace-lifecycle-intraday",
        market_phase="intraday",
        metadata={"market_phase_summary": {"minutes_to_close": 45}},
    )
    intraday_payload.pop("horizon")
    before_intraday = utc_naive_now()
    intraday = service.create_signal(intraday_payload)["item"]
    intraday_expiry = datetime.fromisoformat(intraday["expires_at"])
    assert intraday["horizon"] == "intraday"
    assert before_intraday + timedelta(minutes=44) <= intraday_expiry
    assert intraday_expiry <= utc_naive_now() + timedelta(minutes=46)

    opening_payload = _payload(
        source_report_id=157,
        trace_id="trace-lifecycle-opening",
        market_phase="premarket",
        metadata={"market_phase_summary": {"minutes_to_open": 10}},
    )
    opening_payload.pop("horizon")
    before_opening = utc_naive_now()
    opening = service.create_signal(opening_payload)["item"]
    opening_expiry = datetime.fromisoformat(opening["expires_at"])
    assert opening["horizon"] == "intraday"
    assert before_opening + timedelta(hours=4, minutes=9) <= opening_expiry
    assert opening_expiry <= utc_naive_now() + timedelta(hours=4, minutes=11)

    hk_alert_payload = _payload(
        source_report_id=152,
        trace_id="trace-lifecycle-hk-alert",
        stock_code="00700",
        stock_name="Tencent",
        market="hk",
        action="alert",
    )
    hk_alert_payload.pop("horizon")
    hk_alert_payload.pop("market_phase")
    before_alert = utc_naive_now()
    hk_alert = service.create_signal(hk_alert_payload)["item"]
    hk_alert_expiry = datetime.fromisoformat(hk_alert["expires_at"])
    assert hk_alert["horizon"] == "intraday"
    assert before_alert + timedelta(hours=5, minutes=29) <= hk_alert_expiry
    assert hk_alert_expiry <= utc_naive_now() + timedelta(hours=5, minutes=31)

    postmarket_payload = _payload(
        source_report_id=153,
        trace_id="trace-lifecycle-postmarket",
        market_phase="postmarket",
    )
    postmarket_payload.pop("horizon")
    before_postmarket = utc_naive_now()
    postmarket = service.create_signal(postmarket_payload)["item"]
    postmarket_expiry = datetime.fromisoformat(postmarket["expires_at"])
    assert postmarket["horizon"] == "3d"
    assert before_postmarket + timedelta(days=3, seconds=-1) <= postmarket_expiry
    assert postmarket_expiry <= utc_naive_now() + timedelta(days=3, seconds=1)

    null_lifecycle_payload = _payload(
        source_report_id=158,
        trace_id="trace-lifecycle-null-values",
        horizon=None,
        expires_at=None,
        market_phase="intraday",
        metadata={"market_phase_summary": {"minutes_to_close": 30}},
    )
    before_null_lifecycle = utc_naive_now()
    null_lifecycle = service.create_signal(null_lifecycle_payload)["item"]
    null_lifecycle_expiry = datetime.fromisoformat(null_lifecycle["expires_at"])
    assert null_lifecycle["horizon"] == "intraday"
    assert before_null_lifecycle + timedelta(minutes=29) <= null_lifecycle_expiry
    assert null_lifecycle_expiry <= utc_naive_now() + timedelta(minutes=31)

    swing = service.create_signal(
        _payload(
            source_report_id=154,
            trace_id="trace-lifecycle-swing",
            horizon="swing",
        )
    )["item"]
    assert swing["horizon"] == "swing"
    assert swing["expires_at"] is None

    explicit_expires_at = "2099-01-01T00:00:00Z"
    explicit = service.create_signal(
        _payload(
            source_report_id=155,
            trace_id="trace-lifecycle-explicit",
            horizon="1d",
            expires_at=explicit_expires_at,
        )
    )["item"]
    assert explicit["horizon"] == "1d"
    assert explicit["expires_at"] == "2099-01-01T00:00:00"

    past = service.create_signal(
        _payload(
            source_report_id=156,
            trace_id="trace-lifecycle-past",
            expires_at=(utc_naive_now() - timedelta(minutes=1)).isoformat(),
        )
    )["item"]
    assert past["status"] == "expired"


def test_list_signals_lazily_backfills_analysis_history_signal(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(),
        query_id="query-lazy-signal",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        save_snapshot=True,
    )
    with isolated_db.get_session() as session:
        row = session.query(AnalysisHistory).filter(AnalysisHistory.id == record_id).one()
        report_created_at = datetime(2024, 1, 5, 14, 30)
        row.created_at = report_created_at
        session.commit()
    service = DecisionSignalService(db_manager=isolated_db)
    expected_created_at = service._coerce_history_created_at_to_utc_naive(report_created_at)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["source_report_id"] == record_id
    assert item["source_type"] == "analysis"
    assert item["trace_id"] == "query-lazy-signal"
    assert item["trigger_source"] == "history"
    assert item["action"] == "hold"
    assert item["action_label"] == "持有"
    assert item["reason"] == "趋势仍在，但等待量能确认。"
    assert item["watch_conditions"] == '["回踩不破支撑"]'
    assert item["status"] == "expired"
    assert item["metadata"]["decision_profile"] == "balanced"
    assert item["metadata"]["profile_source"] == "backfill_defaulted"
    assert item["metadata"]["profile_policy_version"] == "decision-profile-v1"
    assert item["metadata"]["signal_generation_version"] == "legacy-report-extractor-v1"
    assert item["metadata"]["decision_signal_metadata_version"] == "decision-signal-metadata-v1"
    assert "scoring_version" not in item["metadata"]
    assert "scoring_breakdown" not in item["metadata"]
    assert datetime.fromisoformat(item["created_at"]) == expected_created_at

    listed_again = service.list_signals(source_type="analysis", source_report_id=record_id)
    assert listed_again["total"] == 1
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 1


def test_list_signals_profile_filter_controls_lazy_backfill(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(),
        query_id="query-lazy-signal-profile-filter",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        save_snapshot=True,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    unknown = service.list_signals(
        source_type="analysis",
        source_report_id=record_id,
        decision_profile="unknown",
    )
    conservative = service.list_signals(
        source_type="analysis",
        source_report_id=record_id,
        decision_profile="conservative",
    )
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 0

    balanced = service.list_signals(
        source_type="analysis",
        source_report_id=record_id,
        decision_profile="balanced",
    )

    assert unknown == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert conservative == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert balanced["total"] == 1
    assert balanced["items"][0]["decision_profile"] == "balanced"
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 1
def test_list_signals_backfill_uses_raw_result_market_structure_without_snapshot(isolated_db) -> None:
    market_structure = {
        "schema_version": "market-structure-v1",
        "status": "partial",
        "market": "cn",
        "market_theme_context": {
            "schema_version": "market-theme-v1",
            "status": "partial",
            "market": "cn",
        },
        "stock_market_position": {
            "schema_version": "stock-market-position-v1",
            "status": "partial",
            "stock_code": "300024",
            "market": "cn",
            "primary_theme": {"name": "机器人概念"},
            "theme_phase": "accelerating",
            "stock_role": "follower",
            "risk_tags": [{"code": "theme_data_partial"}],
        },
    }
    record_id = isolated_db.save_analysis_history(
        result=_history_result(code="300024", name="机器人", market_structure_context=market_structure),
        query_id="query-lazy-signal-market-structure",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_structure_context": {"ignored": True}},
        save_snapshot=False,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 1
    metadata = listed["items"][0]["metadata"]
    assert metadata["market_structure_version"] == "market-structure-v1"
    assert metadata["market_theme_version"] == "market-theme-v1"
    assert metadata["stock_market_position_version"] == "stock-market-position-v1"
    assert metadata["market_structure_status"] == "partial"
    assert metadata["primary_theme"] == "机器人概念"
    assert metadata["theme_phase"] == "accelerating"
    assert metadata["stock_role"] == "follower"
    assert metadata["market_structure_risk_tags"] == ["theme_data_partial"]


@pytest.mark.parametrize(
    ("market_phase_summary", "created_offset", "expected_ttl"),
    (
        ({"phase": "intraday", "minutes_to_close": 5}, timedelta(minutes=10), timedelta(minutes=5)),
        ({"phase": "premarket", "minutes_to_open": 10}, timedelta(hours=5), timedelta(hours=4, minutes=10)),
    ),
)
def test_list_signals_backfill_uses_saved_intraday_ttl_metadata(
    isolated_db,
    market_phase_summary,
    created_offset,
    expected_ttl,
) -> None:
    report_created_at = utc_naive_now().replace(microsecond=0) - created_offset
    record_id = isolated_db.save_analysis_history(
        result=_history_result(),
        query_id=f"query-lazy-signal-ttl-{market_phase_summary['phase']}",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": market_phase_summary},
        save_snapshot=True,
    )
    with isolated_db.get_session() as session:
        row = session.query(AnalysisHistory).filter(AnalysisHistory.id == record_id).one()
        row.created_at = report_created_at
        session.commit()
    service = DecisionSignalService(db_manager=isolated_db)
    expected_report_created_at = service._coerce_history_created_at_to_utc_naive(report_created_at)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["horizon"] == "intraday"
    assert item["status"] == "expired"
    assert datetime.fromisoformat(item["expires_at"]) == expected_report_created_at + expected_ttl


def test_list_signals_backfill_converts_naive_history_created_at_for_invalidation_ordering(
    monkeypatch,
    isolated_db,
) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(
            operation_advice="买入",
            decision_type="buy",
            action="buy",
            action_label="买入",
        ),
        query_id="query-lazy-signal-local-tz",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        save_snapshot=True,
    )
    report_created_at = utc_naive_now() - timedelta(hours=1)
    with isolated_db.get_session() as session:
        row = session.query(AnalysisHistory).filter(AnalysisHistory.id == record_id).one()
        row.created_at = report_created_at
        session.commit()
    service = DecisionSignalService(db_manager=isolated_db)

    def fake_coerce_history_created_at_to_utc_naive(value: datetime) -> datetime:
        assert value == report_created_at
        return value - timedelta(hours=8)

    monkeypatch.setattr(
        service,
        "_coerce_history_created_at_to_utc_naive",
        fake_coerce_history_created_at_to_utc_naive,
    )

    newer_sell = service.create_signal(
        _payload(
            source_report_id=record_id + 1000,
            trace_id="trace-local-tz-opposing-sell",
            action="sell",
            _created_at_override=report_created_at + timedelta(hours=13),
        )
    )["item"]

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 1
    item = listed["items"][0]
    assert datetime.fromisoformat(item["created_at"]) == report_created_at - timedelta(hours=8)
    assert item["action"] == "buy"
    assert item["status"] == "invalidated"
    assert item["metadata"]["invalidated_by_signal_id"] == newer_sell["id"]


def test_list_signals_invalidates_stale_backfill_when_newer_opposing_signal_exists(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(
            operation_advice="买入",
            decision_type="buy",
            action="buy",
            action_label="买入",
            analysis_summary="旧报告建议买入。",
        ),
        query_id="query-stale-backfill-buy",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        save_snapshot=True,
    )
    report_created_at = utc_naive_now() - timedelta(hours=1)
    with isolated_db.get_session() as session:
        row = session.query(AnalysisHistory).filter(AnalysisHistory.id == record_id).one()
        row.created_at = report_created_at
        session.commit()

    service = DecisionSignalService(db_manager=isolated_db)
    newer_sell = service.create_signal(
        _payload(
            source_report_id=record_id + 1000,
            trace_id="trace-newer-opposing-sell",
            action="sell",
        )
    )["item"]

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 1
    backfilled = listed["items"][0]
    assert backfilled["source_report_id"] == record_id
    assert backfilled["action"] == "buy"
    assert backfilled["status"] == "invalidated"
    assert backfilled["metadata"]["decision_profile"] == "balanced"
    assert backfilled["metadata"]["invalidated_by_signal_id"] == newer_sell["id"]
    assert backfilled["metadata"]["invalidated_reason"] == "opposite_active_signal:buy->sell"
    assert service.get_signal(newer_sell["id"])["status"] == "active"

    latest = service.get_latest_active(stock_code="600519", limit=5)
    assert [item["id"] for item in latest["items"]] == [newer_sell["id"]]


def test_list_signals_stale_backfill_invalidation_does_not_cross_profile(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(
            operation_advice="买入",
            decision_type="buy",
            action="buy",
            action_label="买入",
            analysis_summary="旧报告建议买入。",
        ),
        query_id="query-stale-backfill-profile-buy",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        save_snapshot=True,
    )
    report_created_at = utc_naive_now() - timedelta(days=1)
    with isolated_db.get_session() as session:
        row = session.query(AnalysisHistory).filter(AnalysisHistory.id == record_id).one()
        row.created_at = report_created_at
        session.commit()

    service = DecisionSignalService(db_manager=isolated_db)
    newer_sell = service.create_signal(
        _payload(
            source_report_id=record_id + 1000,
            trace_id="trace-newer-opposing-sell-aggressive",
            action="sell",
            decision_profile="aggressive",
        )
    )["item"]

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 1
    backfilled = listed["items"][0]
    assert backfilled["decision_profile"] == "balanced"
    assert backfilled["status"] == "active"
    assert service.get_signal(newer_sell["id"])["status"] == "active"


def test_list_signals_does_not_backfill_market_review_history(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(code="MARKET", name="大盘复盘", operation_advice="查看复盘"),
        query_id="query-lazy-market-review",
        report_type="market_review",
        news_content="复盘正文",
        context_snapshot=None,
        save_snapshot=False,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 0
    assert listed["items"] == []
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 0


def test_list_signals_does_not_backfill_ambiguous_history_advice(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(operation_advice="", decision_type="", action=None, action_label=None),
        query_id="query-lazy-ambiguous-signal",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot=None,
        save_snapshot=False,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 0
    assert listed["items"] == []
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 0


def test_list_signals_does_not_backfill_ambiguous_history_default_decision_type_hold(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(operation_advice="", action=None, action_label=None),
        query_id="query-lazy-ambiguous-hold",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot=None,
        save_snapshot=False,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 0
    assert listed["items"] == []
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 0


def test_list_signals_does_not_backfill_ambiguous_history_default_decision_type_hold_with_noisy_advice(
    isolated_db,
) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(
            operation_advice="买盘增强，继续观察",
            decision_type="hold",
            action=None,
            action_label=None,
        ),
        query_id="query-lazy-ambiguous-noisy-hold",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot=None,
        save_snapshot=False,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    listed = service.list_signals(source_type="analysis", source_report_id=record_id)

    assert listed["total"] == 0
    assert listed["items"] == []
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 0


def test_list_signals_explicit_stock_identities_override_holding_only_and_intersect_filters(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    service.create_signal(
        _payload(
            source_report_id=171501,
            trace_id="trace-explicit-identity-000001",
            stock_code="000001",
            stock_name="平安银行",
            action="sell",
        )
    )
    service.create_signal(
        _payload(
            source_report_id=171502,
            trace_id="trace-explicit-identity-600519",
            stock_code="600519",
            action="reduce",
        )
    )

    listed = service.list_signals(
        stock_identities=[("cn", "000001")],
        holding_only=True,
        status="active",
    )

    assert listed["total"] == 1
    assert listed["items"][0]["stock_code"] == "000001"
    assert listed["items"][0]["action"] == "sell"

    mismatched_stock_filter = service.list_signals(
        stock_code="600519",
        market="cn",
        stock_identities=[("cn", "000001")],
        status="active",
    )

    assert mismatched_stock_filter == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_list_signals_explicit_empty_stock_identities_returns_empty_without_widening(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    service.create_signal(
        _payload(
            source_report_id=171503,
            trace_id="trace-empty-identity-600519",
            stock_code="600519",
            action="sell",
        )
    )

    listed = service.list_signals(stock_identities=[], status="active")

    assert listed == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_list_signals_explicit_stock_identities_do_not_trigger_history_backfill(isolated_db) -> None:
    record_id = isolated_db.save_analysis_history(
        result=_history_result(operation_advice="卖出", decision_type="sell", action="sell", action_label="卖出"),
        query_id="query-explicit-identity-no-backfill",
        report_type="simple",
        news_content="新闻摘要",
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        save_snapshot=True,
    )
    service = DecisionSignalService(db_manager=isolated_db)

    listed = service.list_signals(
        source_type="analysis",
        source_report_id=record_id,
        stock_identities=[("cn", "600519")],
    )

    assert listed == {"items": [], "total": 0, "page": 1, "page_size": 20}
    with isolated_db.get_session() as session:
        assert session.query(DecisionSignalRecord).count() == 0


def test_service_plan_quality_slots_and_explicit_override(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    minimal = service.create_signal(_payload(source_report_id=201, trace_id="trace-201", entry_low=1680))
    assert minimal["item"]["plan_quality"] == "minimal"

    complete = service.create_signal(
        _payload(
            source_report_id=202,
            trace_id="trace-202",
            entry_low=1680,
            entry_high=1700,
            stop_loss=1600,
            target_price=1850,
            invalidation="跌破 1600",
        )
    )
    assert complete["item"]["plan_quality"] == "complete"

    explicit = service.create_signal(
        _payload(
            source_report_id=203,
            trace_id="trace-203",
            plan_quality="unknown",
            entry_low=1680,
            stop_loss=1600,
            target_price=1850,
            invalidation="跌破 1600",
        )
    )
    assert explicit["item"]["plan_quality"] == "unknown"


def test_service_rejects_invalid_enums_and_ranges(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    with pytest.raises(ValueError, match="market"):
        service.create_signal(_payload(market="global"))
    with pytest.raises(ValueError, match="action"):
        service.create_signal(_payload(action="strong buy"))
    with pytest.raises(ValueError, match="confidence"):
        service.create_signal(_payload(confidence=1.1))
    with pytest.raises(ValueError, match="score"):
        service.create_signal(_payload(score=101))
    with pytest.raises(ValueError, match="trigger_source"):
        service.create_signal(_payload(trigger_source="x" * 65))
    with pytest.raises(ValueError, match="trace_id"):
        service.create_signal(_payload(trace_id="x" * 65))
    with pytest.raises(ValueError, match="source_agent"):
        service.create_signal(_payload(source_agent="x" * 65))
    with pytest.raises(ValueError, match="stock_name"):
        service.create_signal(_payload(stock_name="x" * 65))
    with pytest.raises(ValueError, match="action_label"):
        service.create_signal(_payload(action_label="x" * 33))


def test_service_rejects_invalid_price_plan_values(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    invalid_cases = [
        {"entry_low": -1},
        {"entry_high": 0},
        {"stop_loss": nan},
        {"target_price": inf},
        {"entry_low": "not-a-number"},
    ]
    for index, overrides in enumerate(invalid_cases, start=1):
        with pytest.raises(ValueError):
            service.create_signal(_payload(source_report_id=300 + index, trace_id=f"trace-price-{index}", **overrides))

    with pytest.raises(ValueError, match="entry_low"):
        service.create_signal(_payload(source_report_id=306, trace_id="trace-price-range", entry_low=1700, entry_high=1600))


def test_decision_signal_sanitizer_redacts_sensitive_url_queries_without_url_tail_leaks() -> None:
    sanitized = sanitize_decision_signal_text(
        "plain https://news.example.com/article?id=1 "
        "signed https://news.example.com/article?token=abc&id=1 "
        "auth https://news.example.com/article?auth_token=abc&id=2 "
        "api https://news.example.com/article?api-token=abc&id=3 "
        "userinfo https://user:pass@example.com/path "
        "fragment https://news.example.com/cb#access_token=abc "
        "slack https://hooks.slack.com/services/T000/B000/abc123 "
        "feishu https://open.feishu.cn/open-apis/bot/v2/hook/abcdef123456 "
        "wecom https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef"
    )

    assert "https://news.example.com/article?id=1" in sanitized
    assert sanitized.count("[REDACTED_URL]") == 8
    assert "token=abc" not in sanitized
    assert "auth_token=abc" not in sanitized
    assert "api-token=abc" not in sanitized
    assert "user:pass" not in sanitized
    assert "hooks.slack.com" not in sanitized
    assert "open.feishu.cn" not in sanitized
    assert "qyapi.weixin.qq.com" not in sanitized
    assert "]&id=" not in sanitized


@pytest.mark.parametrize(
    ("raw_text", "expected_text", "leaked_fragments"),
    [
        (
            "auth Bearer abcdef0123456789 next",
            "auth Bearer [REDACTED] next",
            ("abcdef0123456789", "0123456789"),
        ),
        (
            "jwt Bearer header.payload:signature next",
            "jwt Bearer [REDACTED] next",
            ("header.payload:signature", "payload:signature"),
        ),
        (
            "base64 Bearer abc+/def==, next",
            "base64 Bearer [REDACTED], next",
            ("abc+/def==", "+/def==", "def=="),
        ),
        (
            "semicolon Bearer abc+/def==; next",
            "semicolon Bearer [REDACTED]; next",
            ("abc+/def==", "+/def==", "def=="),
        ),
        (
            "ampersand Bearer abc+/def==&next=1",
            "ampersand Bearer [REDACTED]&next=1",
            ("abc+/def==", "+/def==", "def=="),
        ),
    ],
)
def test_decision_signal_sanitizer_redacts_entire_bearer_token_matrix(
    raw_text,
    expected_text,
    leaked_fragments,
) -> None:
    sanitized = sanitize_decision_signal_text(raw_text)

    assert expected_text in sanitized
    for leaked in leaked_fragments:
        assert leaked not in sanitized


@pytest.mark.parametrize(
    ("raw_text", "expected_text", "leaked_fragments"),
    [
        (
            "basic Authorization: Basic dXNlcjpwYXNz next",
            "basic Authorization: [REDACTED] next",
            ("dXNlcjpwYXNz", "pwYXNz"),
        ),
        (
            "token Authorization: Token abc+/def==; next",
            "token Authorization: [REDACTED]; next",
            ("abc+/def==", "+/def==", "def=="),
        ),
        (
            "assignment authorization=secret-value next",
            "assignment authorization=[REDACTED] next",
            ("secret-value",),
        ),
        (
            "cookie Cookie: session=abc123; next",
            "cookie Cookie: [REDACTED]; next",
            ("session=abc123", "abc123"),
        ),
        (
            "set-cookie Set-Cookie: session=abc123; Path=/ next",
            "set-cookie Set-Cookie: [REDACTED]; Path=/ next",
            ("session=abc123", "abc123"),
        ),
        (
            "cookie assignment cookie=session=abc123 next",
            "cookie assignment cookie=[REDACTED] next",
            ("session=abc123", "abc123"),
        ),
    ],
)
def test_decision_signal_sanitizer_redacts_authorization_and_cookie_matrix(
    raw_text,
    expected_text,
    leaked_fragments,
) -> None:
    sanitized = sanitize_decision_signal_text(raw_text)

    assert expected_text in sanitized
    for leaked in leaked_fragments:
        assert leaked not in sanitized


def test_shared_diagnostic_sanitizer_uses_same_auth_credential_boundary() -> None:
    sanitized = sanitize_diagnostic_text(
        "Authorization: Bearer abc+/def==; next "
        "Authorization: Basic dXNlcjpwYXNz "
        "Cookie: session=abc123"
    )

    assert "Authorization: [REDACTED]; next" in sanitized
    assert "Authorization: [REDACTED]" in sanitized
    assert "Cookie: [REDACTED]" in sanitized
    for leaked in (
        "abc+/def==",
        "+/def==",
        "def==",
        "dXNlcjpwYXNz",
        "pwYXNz",
        "session=abc123",
        "abc123",
    ):
        assert leaked not in sanitized


def test_trace_id_identity_is_not_silently_truncated(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    trace_a = f"{'x' * 63}a"
    trace_b = f"{'x' * 63}b"

    first = service.create_signal(_payload(source_report_id=None, trace_id=trace_a))
    second = service.create_signal(_payload(source_report_id=None, trace_id=trace_b))

    assert first["created"] is True
    assert second["created"] is True
    assert first["item"]["id"] != second["item"]["id"]


def test_trace_id_rejects_sensitive_identity_text(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    with pytest.raises(ValueError, match="trace_id"):
        service.create_signal(_payload(trace_id="Bearer abc+/def=="))

    with pytest.raises(ValueError, match="trace_id"):
        service.create_signal(_payload(trace_id="Authorization: Basic dXNlcjpwYXNz"))

    with pytest.raises(ValueError, match="trace_id"):
        service.create_signal(_payload(trace_id="cookie=session=abc123"))

    with pytest.raises(ValueError, match="trace_id"):
        service.create_signal(_payload(trace_id="https://hooks.example.com/send"))


def test_service_sanitizes_public_short_fields_before_persisting(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)

    result = service.create_signal(
        _payload(
            stock_name="secret=plain-secret",
            source_agent="Bearer abc+/def==",
            trigger_source="Bearer abc+/def==",
            action_label="token=abc",
        )
    )

    item = result["item"]
    assert item["stock_name"] == "secret=[REDACTED]"
    assert item["source_agent"] == "Bearer [REDACTED]"
    assert item["trigger_source"] == "Bearer [REDACTED]"
    assert item["action_label"] == "token=[REDACTED]"
    assert "plain-secret" not in str(item)
    assert "abc+/def==" not in str(item)

    with isolated_db.get_session() as session:
        row = session.query(DecisionSignalRecord).filter_by(id=item["id"]).one()
        stored_blob = " ".join(
            str(value or "")
            for value in (
                row.stock_name,
                row.source_agent,
                row.trigger_source,
                row.action_label,
            )
        )
    assert "plain-secret" not in stored_blob
    assert "abc+/def==" not in stored_blob
    assert "Bearer [REDACTED]" in stored_blob


def test_service_sanitizes_text_and_json_before_persisting(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    long_text = "x" * 450

    result = service.create_signal(
        _payload(
            reason=f"{long_text} Bearer abc.def.ghi https://hooks.example.com/send",
            risk_summary="api_key=sk-1234567890abcdef123456",
            invalidation={"token": "plain-secret", "note": "secret=keepout"},
            watch_conditions=["watch https://example.com/path"],
            evidence={
                "webhook_url": "https://secret.example.com/hook",
                "source_url": "https://news.example.com/article?id=1",
                "signed_url": "https://news.example.com/article?token=abc&id=1",
                "auth_url": "https://news.example.com/article?auth_token=abc&id=2",
                "hyphen_signed_url": "https://news.example.com/article?api-key=abc",
                "slack": "https://hooks.slack.com/services/T000/B000/abcdef",
                "feishu": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef",
                "userinfo": "https://user:pass@example.com/path",
                "fragment": "https://news.example.com/cb#access_token=abc",
                "note": "Bearer abc+/def==",
                "auth_header": "Authorization: Basic dXNlcjpwYXNz",
                "cookie_header": "Cookie: session=abc123",
            },
            data_quality_summary={
                "level": "limited",
                "email_password": "mail-secret",
                "note": "password=mail-secret-2",
            },
            metadata={
                "access_token": "abc",
                "callback": "https://example.com/cb",
                "auth_assignment": "authorization=secret-value",
            },
        )
    )

    item = result["item"]
    assert len(item["reason"]) > 300
    response_blob = str(item)
    assert "hooks.example.com" not in response_blob
    assert "news.example.com/article?id=1" in response_blob
    assert "example.com/cb" in response_blob
    assert "secret.example.com" not in response_blob
    assert "hooks.slack.com" not in response_blob
    assert "open.feishu.cn" not in response_blob
    assert "user:pass" not in response_blob
    assert "access_token=abc" not in response_blob
    assert "token=abc" not in response_blob
    assert "auth_token=abc" not in response_blob
    assert "api-key=abc" not in response_blob
    assert "]&id=" not in response_blob
    assert "plain-secret" not in response_blob
    assert "abc+/def==" not in response_blob
    assert "+/def==" not in response_blob
    assert "dXNlcjpwYXNz" not in response_blob
    assert "pwYXNz" not in response_blob
    assert "session=abc123" not in response_blob
    assert "mail-secret" not in response_blob
    assert "mail-secret-2" not in response_blob
    assert "secret-value" not in response_blob
    assert "sk-1234567890abcdef123456" not in response_blob
    assert "[REDACTED" in response_blob

    with isolated_db.get_session() as session:
        row = session.query(DecisionSignalRecord).filter_by(id=item["id"]).one()
        stored_blob = " ".join(
            str(value or "")
            for value in (
                row.reason,
                row.risk_summary,
                row.invalidation,
                row.watch_conditions,
                row.evidence_json,
                row.data_quality_summary_json,
                row.metadata_json,
            )
        )
    assert "hooks.example.com" not in stored_blob
    assert "news.example.com/article?id=1" in stored_blob
    assert "hooks.slack.com" not in stored_blob
    assert "open.feishu.cn" not in stored_blob
    assert "user:pass" not in stored_blob
    assert "access_token=abc" not in stored_blob
    assert "token=abc" not in stored_blob
    assert "auth_token=abc" not in stored_blob
    assert "api-key=abc" not in stored_blob
    assert "]&id=" not in stored_blob
    assert "plain-secret" not in stored_blob
    assert "abc+/def==" not in stored_blob
    assert "+/def==" not in stored_blob
    assert "dXNlcjpwYXNz" not in stored_blob
    assert "pwYXNz" not in stored_blob
    assert "session=abc123" not in stored_blob
    assert "mail-secret" not in stored_blob
    assert "mail-secret-2" not in stored_blob
    assert "secret-value" not in stored_blob
    assert "sk-1234567890abcdef123456" not in stored_blob


def test_service_raises_on_corrupt_persisted_json(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    result = service.create_signal(_payload(source_report_id=351, trace_id="trace-351"))
    signal_id = result["item"]["id"]

    with isolated_db.get_session() as session:
        row = session.get(DecisionSignalRecord, signal_id)
        row.evidence_json = "{not valid json"
        session.commit()

    with pytest.raises(DecisionSignalStorageError, match="invalid persisted JSON"):
        service.get_signal(signal_id)


@pytest.mark.parametrize("terminal_status", ["expired", "invalidated", "closed", "archived"])
def test_service_rejects_terminal_status_reactivation(isolated_db, terminal_status) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    created = service.create_signal(
        _payload(source_report_id=360, trace_id=f"trace-terminal-{terminal_status}")
    )
    signal_id = created["item"]["id"]

    service.update_status(signal_id, status=terminal_status)

    with pytest.raises(ValueError, match="terminal decision signal"):
        service.update_status(signal_id, status="active")


def test_service_status_metadata_preserves_null_contract_and_profile_identity(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    signal = service.create_signal(
        _payload(
            source_report_id=365,
            trace_id="trace-status-profile",
            decision_profile="aggressive",
        )
    )["item"]

    preserved = service.update_status(signal["id"], status="active")
    assert preserved["metadata"] == signal["metadata"]

    updated = service.update_status(
        signal["id"],
        status="closed",
        metadata={"decision_profile": "balanced", "closed_by": "tester"},
        replace_metadata=True,
    )
    assert updated["decision_profile"] == "aggressive"
    assert updated["metadata"] == {
        "decision_profile": "aggressive",
        "closed_by": "tester",
    }

    cleared = service.update_status(
        signal["id"],
        status="archived",
        metadata=None,
        replace_metadata=True,
    )
    assert cleared["metadata"] is None

    legacy_fields, _ = service._normalize_payload(
        _payload(source_report_id=366, trace_id="trace-status-legacy-profile")
    )
    legacy_fields["decision_profile"] = None
    legacy = service.repo.create(legacy_fields)
    legacy_updated = service.update_status(
        legacy.id,
        status="closed",
        metadata={"decision_profile": "balanced", "closed_by": "tester"},
        replace_metadata=True,
    )
    assert legacy_updated["decision_profile"] is None
    assert legacy_updated["metadata"] == {"closed_by": "tester"}


def test_service_invalidates_opposing_active_signals(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    old_buy = service.create_signal(
        _payload(
            source_report_id=371,
            trace_id="trace-opposing-buy",
            action="buy",
            metadata={"task_id": "old-buy"},
        )
    )["item"]

    with isolated_db.get_session() as session:
        row = session.get(DecisionSignalRecord, old_buy["id"])
        row.metadata_json = json.dumps({
            "decision_profile": "aggressive",
            "task_id": "old-buy",
        })
        session.commit()

    new_sell = service.create_signal(
        _payload(
            source_report_id=372,
            trace_id="trace-opposing-sell",
            action="sell",
        )
    )["item"]

    old_after = service.get_signal(old_buy["id"])
    assert new_sell["status"] == "active"
    assert old_after["status"] == "invalidated"
    assert old_after["metadata"]["task_id"] == "old-buy"
    assert old_after["metadata"]["decision_profile"] == "balanced"
    assert old_after["metadata"]["invalidated_by_signal_id"] == new_sell["id"]
    assert old_after["metadata"]["invalidated_reason"] == "opposite_active_signal:buy->sell"
    assert old_after["metadata"]["previous_status"] == "active"

    latest = service.get_latest_active(stock_code="600519", limit=5)
    assert [item["id"] for item in latest["items"]] == [new_sell["id"]]


def test_service_opposing_signal_invalidation_is_same_profile_and_null_safe(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    old_balanced_buy = service.create_signal(
        _payload(
            source_report_id=3721,
            trace_id="trace-opposing-balanced-buy",
            action="buy",
            decision_profile="balanced",
        )
    )["item"]
    aggressive_sell = service.create_signal(
        _payload(
            source_report_id=3722,
            trace_id="trace-opposing-aggressive-sell",
            action="sell",
            decision_profile="aggressive",
        )
    )["item"]

    assert service.get_signal(old_balanced_buy["id"])["status"] == "active"
    assert aggressive_sell["status"] == "active"

    balanced_sell = service.create_signal(
        _payload(
            source_report_id=3723,
            trace_id="trace-opposing-balanced-sell",
            action="sell",
            decision_profile="balanced",
        )
    )["item"]
    assert service.get_signal(old_balanced_buy["id"])["status"] == "invalidated"
    assert service.get_signal(aggressive_sell["id"])["status"] == "active"
    assert balanced_sell["status"] == "active"

    legacy_buy_fields, _ = service._normalize_payload(
        _payload(
            source_report_id=3724,
            trace_id="trace-opposing-null-buy",
            action="buy",
        )
    )
    legacy_buy_fields["decision_profile"] = None
    legacy_buy = service.repo.create(legacy_buy_fields)

    legacy_no_profile_fields, _ = service._normalize_payload(
        _payload(
            source_report_id=3727,
            trace_id="trace-opposing-null-buy-no-profile",
            action="buy",
        )
    )
    legacy_no_profile_fields["decision_profile"] = None
    legacy_no_profile_fields["metadata_json"] = json.dumps({"legacy_marker": "keep"})
    legacy_no_profile = service.repo.create(legacy_no_profile_fields)

    balanced_sell_2 = service.create_signal(
        _payload(
            source_report_id=3725,
            trace_id="trace-opposing-balanced-sell-2",
            action="sell",
            decision_profile="balanced",
        )
    )["item"]
    assert service.get_signal(legacy_buy.id)["status"] == "active"

    legacy_sell_fields, _ = service._normalize_payload(
        _payload(
            source_report_id=3726,
            trace_id="trace-opposing-null-sell",
            action="sell",
        )
    )
    legacy_sell_fields["decision_profile"] = None
    legacy_sell = service.repo.create(legacy_sell_fields)
    service._invalidate_opposing_active_signals(legacy_sell, reference_at=legacy_sell.created_at)

    legacy_buy_after = service.get_signal(legacy_buy.id)
    assert legacy_buy_after["status"] == "invalidated"
    assert legacy_buy_after["metadata"]["decision_profile"] == "balanced"
    legacy_no_profile_after = service.get_signal(legacy_no_profile.id)
    assert legacy_no_profile_after["status"] == "invalidated"
    assert legacy_no_profile_after["metadata"]["legacy_marker"] == "keep"
    assert "decision_profile" not in legacy_no_profile_after["metadata"]
    assert service.get_signal(balanced_sell_2["id"])["status"] == "active"


def test_service_expired_refresh_invalidates_later_opposing_active_signal(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    buy_payload = _payload(source_report_id=376, trace_id="trace-refresh-buy", action="buy")
    old_buy = service.create_signal(buy_payload)["item"]
    service.update_status(old_buy["id"], status="expired")

    active_sell = service.create_signal(
        _payload(source_report_id=377, trace_id="trace-refresh-sell", action="sell")
    )["item"]
    assert service.get_signal(active_sell["id"])["status"] == "active"

    refreshed = service.create_signal(
        {
            **buy_payload,
            "expires_at": (utc_naive_now() + timedelta(days=1)).isoformat(),
        }
    )

    assert refreshed["created"] is False
    assert refreshed["item"]["id"] == old_buy["id"]
    assert refreshed["item"]["status"] == "active"
    assert refreshed["item"]["decision_profile"] == "balanced"
    assert refreshed["item"]["metadata"]["decision_profile"] == "balanced"
    sell_after = service.get_signal(active_sell["id"])
    assert sell_after["status"] == "invalidated"
    assert sell_after["metadata"]["invalidated_by_signal_id"] == old_buy["id"]
    latest = service.get_latest_active(stock_code="600519", limit=5)
    assert [item["id"] for item in latest["items"]] == [old_buy["id"]]


def test_service_does_not_invalidate_neutral_or_terminal_signals(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    old_buy = service.create_signal(
        _payload(source_report_id=381, trace_id="trace-neutral-buy", action="buy")
    )["item"]

    hold = service.create_signal(
        _payload(source_report_id=382, trace_id="trace-neutral-hold", action="hold")
    )["item"]

    assert hold["status"] == "active"
    assert service.get_signal(old_buy["id"])["status"] == "active"

    service.update_status(old_buy["id"], status="closed")
    service.create_signal(
        _payload(source_report_id=383, trace_id="trace-terminal-sell", action="sell")
    )
    assert service.get_signal(old_buy["id"])["status"] == "closed"


@pytest.mark.parametrize(
    "invalid_metadata",
    [
        "{not valid json",
        b"\xff",
        "[" * 10_000 + "]" * 10_000,
    ],
    ids=["malformed-text", "invalid-blob", "too-deep"],
)
def test_service_replaces_corrupt_metadata_during_invalidation(
    isolated_db,
    invalid_metadata,
) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    old_buy = service.create_signal(
        _payload(source_report_id=391, trace_id="trace-corrupt-metadata-buy", action="buy")
    )["item"]

    with isolated_db.get_session() as session:
        row = session.query(DecisionSignalRecord).filter_by(id=old_buy["id"]).one()
        row.metadata_json = invalid_metadata
        session.commit()

    new_sell = service.create_signal(
        _payload(source_report_id=392, trace_id="trace-corrupt-metadata-sell", action="sell")
    )["item"]

    old_after = service.get_signal(old_buy["id"])
    assert old_after["status"] == "invalidated"
    assert old_after["metadata"]["metadata_replaced_due_to_invalid_json"] is True
    assert old_after["metadata"]["decision_profile"] == "balanced"
    assert old_after["metadata"]["invalidated_by_signal_id"] == new_sell["id"]


def test_service_replaces_non_object_metadata_during_invalidation(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    old_buy = service.create_signal(
        _payload(source_report_id=393, trace_id="trace-non-object-metadata-buy", action="buy")
    )["item"]

    with isolated_db.get_session() as session:
        row = session.query(DecisionSignalRecord).filter_by(id=old_buy["id"]).one()
        row.metadata_json = '["legacy"]'
        session.commit()

    new_sell = service.create_signal(
        _payload(source_report_id=394, trace_id="trace-non-object-metadata-sell", action="sell")
    )["item"]

    old_after = service.get_signal(old_buy["id"])
    assert old_after["status"] == "invalidated"
    assert old_after["metadata"]["metadata_replaced_due_to_non_object"] is True
    assert old_after["metadata"]["decision_profile"] == "balanced"
    assert old_after["metadata"]["invalidated_by_signal_id"] == new_sell["id"]


def test_service_duplicate_retry_repairs_failed_invalidation(isolated_db, monkeypatch) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    old_buy = service.create_signal(
        _payload(source_report_id=392, trace_id="trace-repair-buy", action="buy")
    )["item"]
    sell_payload = _payload(source_report_id=393, trace_id="trace-repair-sell", action="sell")
    original_update_status = service.repo.update_status

    def fail_once(*_args, **_kwargs):
        raise RuntimeError("invalidation write failed")

    monkeypatch.setattr(service.repo, "update_status", fail_once)
    with pytest.raises(RuntimeError, match="invalidation write failed"):
        service.create_signal(sell_payload)

    assert service.get_signal(old_buy["id"])["status"] == "active"

    monkeypatch.setattr(service.repo, "update_status", original_update_status)
    retried = service.create_signal(sell_payload)

    assert retried["created"] is False
    assert retried["item"]["status"] == "active"
    old_after = service.get_signal(old_buy["id"])
    assert old_after["status"] == "invalidated"
    assert old_after["metadata"]["invalidated_by_signal_id"] == retried["item"]["id"]


def test_service_duplicate_old_signal_does_not_invalidate_newer_opposing_signal(isolated_db, monkeypatch) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    buy_payload = _payload(source_report_id=395, trace_id="trace-old-replay-buy", action="buy")
    old_buy = service.create_signal(buy_payload)["item"]

    monkeypatch.setattr(service, "_invalidate_opposing_active_signals", lambda *_args, **_kwargs: None)
    new_sell = service.create_signal(
        _payload(source_report_id=396, trace_id="trace-old-replay-sell", action="sell")
    )["item"]
    monkeypatch.undo()

    replayed_buy = service.create_signal(buy_payload)

    assert replayed_buy["created"] is False
    assert replayed_buy["item"]["id"] == old_buy["id"]
    assert service.get_signal(new_sell["id"])["status"] == "active"
    assert service.get_signal(old_buy["id"])["status"] == "active"


def test_service_relaxed_active_fill_does_not_invalidate_newer_opposing_signal(isolated_db, monkeypatch) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    buy_payload = _payload(source_report_id=397, trace_id="trace-relaxed-fill-buy", action="buy")
    old_buy = service.create_signal(buy_payload)["item"]

    with isolated_db.get_session() as session:
        row = session.query(DecisionSignalRecord).filter_by(id=old_buy["id"]).one()
        row.horizon = None
        row.market_phase = None
        session.commit()

    monkeypatch.setattr(service, "_invalidate_opposing_active_signals", lambda *_args, **_kwargs: None)
    new_sell = service.create_signal(
        _payload(source_report_id=398, trace_id="trace-relaxed-fill-sell", action="sell")
    )["item"]
    monkeypatch.undo()

    relaxed_payload = dict(buy_payload)
    relaxed_payload.pop("horizon")
    replayed_buy = service.create_signal(relaxed_payload)

    assert replayed_buy["created"] is False
    assert replayed_buy["item"]["id"] == old_buy["id"]
    assert replayed_buy["item"]["horizon"] == "intraday"
    assert replayed_buy["item"]["market_phase"] == "intraday"
    assert service.get_signal(new_sell["id"])["status"] == "active"
    assert service.get_signal(old_buy["id"])["status"] == "active"


def test_service_propagates_unexpected_invalidation_failures(isolated_db) -> None:
    class FailingInvalidationRepo:
        def create_if_absent(
            self,
            fields,
            *,
            allow_relaxed_horizon_fill=False,
            allow_refresh=True,
        ):
            assert allow_refresh is True
            row = SimpleNamespace(
                id=1,
                status="active",
                action=fields["action"],
                market=fields["market"],
                stock_code=fields["stock_code"],
                decision_profile=fields["decision_profile"],
            )
            return DecisionSignalCreateResult(
                row=row,
                created=True,
                invalidation_reference_at=utc_naive_now(),
            )

        def list_active_by_stock_actions(self, **_kwargs):
            raise RuntimeError("invalidation write failed")

    service = DecisionSignalService(repo=FailingInvalidationRepo(), db_manager=isolated_db)

    with pytest.raises(RuntimeError, match="invalidation write failed"):
        service.create_signal(_payload(source_report_id=392, trace_id="trace-invalidation-failure"))
