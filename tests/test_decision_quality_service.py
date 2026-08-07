# -*- coding: utf-8 -*-
from __future__ import annotations

from copy import deepcopy

import pytest

from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.services.decision_quality_service import DecisionQualityService
from src.storage import DatabaseManager, DecisionSignalRecord


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'decision_quality_service.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _signal(signal_id: int = 101, **overrides):
    signal = {
        "id": signal_id,
        "market": "us",
        "stock_code": "AAPL",
        "decision_profile": "balanced",
    }
    signal.update(overrides)
    return signal


def _decision(**overrides):
    decision = {
        "position_action": "hold",
        "incremental_action": "wait",
        "confidence_by_horizon": {"5d": 0.55, "20d": 0.68, "60d": 0.61},
        "supporting_evidence": ["cash flow remains positive"],
        "opposing_evidence": ["valuation remains elevated"],
        "invalidation": "verified guidance is withdrawn",
        "watch_conditions": ["valuation enters the approved range"],
        "next_review": "next earnings release",
        "benchmark": {
            "market": "us",
            "code": "SPY",
            "type": "market_index",
            "evidence_url": "https://example.com/benchmark",
            "evidence_as_of": "2026-07-25T08:00:00Z",
        },
        "decision_version": "portfolio-decision-v1",
    }
    decision.update(overrides)
    return decision


def _snapshot(**overrides):
    snapshot = {
        "schema_version": "portfolio-research-snapshot-v1",
        "snapshot_hash": "a" * 64,
        "cutoff": "2026-07-25T08:00:00Z",
        "positions": [],
        "instruments": [
            {
                "symbol": "AAPL",
                "market": "us",
                "instrument_type": "equity",
            }
        ],
    }
    snapshot.update(overrides)
    return snapshot


def test_freeze_context_persists_complete_recommendation_time_contract(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(repo=repo)

    result = service.freeze_context(
        signal=_signal(),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )

    assert result["created"] is True
    assert result["status"] == "complete"
    assert result["unable_reasons"] == []
    row = repo.get_context_by_signal(signal_id=101)
    assert row.account_id == 2
    assert row.frozen_snapshot_hash == "a" * 64
    assert row.benchmark_code == "SPY"
    assert row.instrument_type == "equity"
    assert row.position_action == "hold"
    assert row.incremental_action == "wait"


def test_quality_detail_includes_decision_evidence_summary(isolated_db) -> None:
    class EvidenceSummaryStub:
        def get_summary(self, *, signal_id: int):
            return {
                "signal_id": signal_id,
                "status": "complete",
                "display_status": "已保存",
                "strategy_name": "当前持仓策略",
                "strategy_version": "1.0.0",
                "unable_reasons": [],
            }

    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(
        repo=repo,
        db_manager=isolated_db,
        decision_evidence_service=EvidenceSummaryStub(),
    )
    service.freeze_context(
        signal=_signal(),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )

    detail = service.get_quality(signal_id=101)

    assert detail["evidence_snapshot"]["display_status"] == "已保存"
    assert detail["evidence_snapshot"]["strategy_version"] == "1.0.0"


def test_quality_detail_resolves_reused_context_but_keeps_exact_signal_evidence(
    isolated_db,
) -> None:
    requested_evidence_ids: list[int] = []

    class EvidenceSummaryStub:
        def get_summary(self, *, signal_id: int):
            requested_evidence_ids.append(signal_id)
            return {
                "signal_id": signal_id,
                "status": "complete",
                "display_status": "已保存",
                "unable_reasons": [],
            }

    with isolated_db.session_scope() as session:
        first_signal = DecisionSignalRecord(
            stock_code="AAPL",
            market="us",
            source_type="analysis",
            trigger_source="portfolio",
            action="hold",
            plan_quality="complete",
            status="active",
        )
        session.add(first_signal)
        session.flush()
        first_signal_id = int(first_signal.id)
        replay_signal = DecisionSignalRecord(
            stock_code="AAPL",
            market="us",
            source_type="analysis",
            trigger_source="portfolio",
            action="hold",
            plan_quality="complete",
            status="active",
            metadata_json=(
                '{"quality_context_signal_id":' + str(first_signal_id) + "}"
            ),
        )
        session.add(replay_signal)
        session.flush()
        replay_signal_id = int(replay_signal.id)

    quality_repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(
        repo=quality_repo,
        db_manager=isolated_db,
        decision_evidence_service=EvidenceSummaryStub(),
    )
    service.freeze_context(
        signal=_signal(first_signal_id),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )

    detail = service.get_quality(signal_id=replay_signal_id)

    assert detail["context"]["signal_id"] == first_signal_id
    assert detail["evidence_snapshot"]["signal_id"] == replay_signal_id
    assert requested_evidence_ids == [replay_signal_id]


def test_freeze_context_does_not_persist_when_decision_evidence_is_incomplete(
    isolated_db,
) -> None:
    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(repo=repo, db_manager=isolated_db)

    result = service.freeze_context(
        signal=_signal(),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
        evidence_unable_reasons=["benchmark_evidence_missing"],
    )

    assert result["status"] == "insufficient_evidence"
    assert result["context_id"] is None
    assert result["unable_reasons"] == ["benchmark_evidence_missing"]
    assert repo.get_context_by_signal(signal_id=101) is None


def test_freeze_context_reuses_identical_material_event_across_signal_refreshes(isolated_db) -> None:
    service = DecisionQualityService(repo=DecisionQualityRepository(isolated_db))

    first = service.freeze_context(
        signal=_signal(101),
        portfolio_decision=_decision(display_refreshed_at="2026-07-25T09:00:00Z"),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )
    repeated = service.freeze_context(
        signal=_signal(102),
        portfolio_decision=_decision(display_refreshed_at="2026-07-25T10:00:00Z"),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )

    assert repeated["created"] is False
    assert repeated["context_id"] == first["context_id"]
    assert repeated["signal_id"] == 101


@pytest.mark.parametrize(
    ("decision_change", "snapshot_change"),
    [
        ({"position_action": "reduce"}, {}),
        ({"incremental_action": "no_add"}, {}),
        ({"benchmark": {"market": "us", "code": "QQQ", "type": "market_index"}}, {}),
        ({"trigger_contract": {"type": "price", "operator": "lte", "value": 175}}, {}),
        ({"watch_conditions": ["new trigger"]}, {}),
        ({"invalidation": "new invalidation"}, {}),
    ],
)
def test_material_changes_create_distinct_contexts(
    isolated_db,
    decision_change,
    snapshot_change,
) -> None:
    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(repo=repo)
    baseline = service.freeze_context(
        signal=_signal(101),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )
    changed_decision = deepcopy(_decision())
    changed_decision.update(decision_change)
    changed_snapshot = deepcopy(_snapshot())
    changed_snapshot.update(snapshot_change)

    changed = service.freeze_context(
        signal=_signal(102),
        portfolio_decision=changed_decision,
        frozen_snapshot=changed_snapshot,
        portfolio_context={"account_id": 2},
    )

    assert changed["context_id"] != baseline["context_id"]
    assert changed["material_event_fingerprint"] != baseline["material_event_fingerprint"]


def test_daily_snapshot_refresh_reuses_unchanged_material_decision(isolated_db) -> None:
    service = DecisionQualityService(repo=DecisionQualityRepository(isolated_db))
    first = service.freeze_context(
        signal=_signal(101),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )

    refreshed = service.freeze_context(
        signal=_signal(102),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(
            snapshot_hash="b" * 64,
            cutoff="2026-07-26T08:00:00Z",
        ),
        portfolio_context={"account_id": 2},
    )

    assert refreshed["created"] is False
    assert refreshed["context_id"] == first["context_id"]
    assert refreshed["signal_id"] == 101


def test_missing_benchmark_is_frozen_as_insufficient_evidence_not_invented(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(repo=repo)

    result = service.freeze_context(
        signal=_signal(),
        portfolio_decision=_decision(benchmark=None),
        frozen_snapshot=_snapshot(),
        portfolio_context={"account_id": 2},
    )

    assert result["status"] == "insufficient_evidence"
    assert "benchmark_identity_missing" in result["unable_reasons"]
    assert repo.get_context_by_signal(signal_id=101).benchmark_code is None


def test_missing_instrument_type_is_frozen_as_insufficient_evidence(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(repo=repo)

    result = service.freeze_context(
        signal=_signal(),
        portfolio_decision=_decision(),
        frozen_snapshot=_snapshot(instruments=[]),
        portfolio_context={"account_id": 2},
    )

    assert result["status"] == "insufficient_evidence"
    assert "instrument_type_missing" in result["unable_reasons"]
    assert repo.get_context_by_signal(signal_id=101).instrument_type is None


@pytest.mark.parametrize(
    ("signal_overrides", "decision_overrides", "snapshot_overrides", "portfolio_context", "blocker"),
    [
        ({}, {}, {}, {}, "account_id_missing"),
        ({"market": None}, {}, {}, {"account_id": 2}, "instrument_identity_missing"),
        ({"stock_code": None}, {}, {}, {"account_id": 2}, "instrument_identity_missing"),
        ({}, {"position_action": None}, {}, {"account_id": 2}, "position_action_missing"),
        ({}, {"incremental_action": None}, {}, {"account_id": 2}, "incremental_action_missing"),
        ({}, {}, {"cutoff": None}, {"account_id": 2}, "evidence_cutoff_missing"),
        ({}, {}, {"snapshot_hash": "invalid"}, {"account_id": 2}, "frozen_snapshot_hash_invalid"),
    ],
)
def test_missing_context_identity_is_insufficient_without_failed_sidecar_write(
    isolated_db,
    signal_overrides,
    decision_overrides,
    snapshot_overrides,
    portfolio_context,
    blocker,
) -> None:
    repo = DecisionQualityRepository(isolated_db)
    service = DecisionQualityService(repo=repo)

    result = service.freeze_context(
        signal=_signal(**signal_overrides),
        portfolio_decision=_decision(**decision_overrides),
        frozen_snapshot=_snapshot(**snapshot_overrides),
        portfolio_context=portfolio_context,
    )

    assert result["context_id"] is None
    assert result["signal_id"] == 101
    assert result["created"] is False
    assert result["status"] == "insufficient_evidence"
    assert blocker in result["unable_reasons"]
    assert repo.get_context_by_signal(signal_id=101) is None
