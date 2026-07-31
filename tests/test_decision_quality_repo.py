# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'decision_quality.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _context_fields(**overrides):
    fields = {
        "signal_id": 101,
        "account_id": 2,
        "market": "us",
        "stock_code": "AAPL",
        "instrument_type": "equity",
        "frozen_snapshot_hash": "a" * 64,
        "material_event_fingerprint": "b" * 64,
        "position_action": "hold",
        "incremental_action": "wait",
        "confidence_by_horizon_json": json.dumps({"5d": 0.55, "20d": 0.68, "60d": 0.61}),
        "benchmark_market": "us",
        "benchmark_code": "SPY",
        "benchmark_type": "market_index",
        "benchmark_evidence_url": "https://example.com/benchmark",
        "benchmark_evidence_as_of": datetime(2026, 7, 25, 8, 0),
        "decision_cutoff": datetime(2026, 7, 25, 8, 0),
        "context_status": "complete",
        "unable_reasons_json": "[]",
    }
    fields.update(overrides)
    return fields


def _outcome_fields(**overrides):
    fields = {
        "signal_id": 101,
        "horizon": "5d",
        "engine_version": "decision-quality-v1",
        "eval_status": "pending",
        "unable_reason": None,
        "anchor_date": None,
        "eval_window_days": 5,
        "start_price": None,
        "end_close": None,
        "max_high": None,
        "min_low": None,
        "stock_return_pct": None,
        "benchmark_start_price": None,
        "benchmark_end_close": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None,
        "normalized_action_return_pct": None,
        "decision_value_vs_hold_pct": None,
        "hindsight_regret_pct": None,
        "decision_value_status": "pending",
        "position_action": "hold",
        "incremental_action": "wait",
        "market": "us",
        "instrument_type": "equity",
        "data_quality_level": "good",
    }
    fields.update(overrides)
    return fields


def _attribution_fields(**overrides):
    fields = {
        "signal_id": 101,
        "horizon": "5d",
        "engine_version": "decision-quality-v1",
        "category": "unattributed",
        "status": "proposed",
        "summary": "Forward evidence is not mature.",
        "evidence_json": "[]",
        "counterexamples_json": "[]",
        "user_note": None,
    }
    fields.update(overrides)
    return fields


def test_context_creation_reuses_identical_material_event(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)

    created, was_created = repo.create_context_if_absent(_context_fields())
    repeated, repeated_created = repo.create_context_if_absent(_context_fields(signal_id=202))

    assert was_created is True
    assert repeated_created is False
    assert repeated.id == created.id
    assert repeated.signal_id == 101
    assert repo.get_context_by_signal(signal_id=101).id == created.id
    assert repo.get_context_by_signal(signal_id=202) is None


def test_context_cannot_rewrite_recommendation_time_fields(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    original, _ = repo.create_context_if_absent(_context_fields())

    with pytest.raises(ValueError, match="immutable"):
        repo.create_context_if_absent(
            _context_fields(
                position_action="exit",
                material_event_fingerprint="c" * 64,
            )
        )

    persisted = repo.get_context_by_signal(signal_id=101)
    assert persisted.id == original.id
    assert persisted.position_action == "hold"
    assert persisted.material_event_fingerprint == "b" * 64


def test_context_repository_rejects_arbitrary_fields(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)

    with pytest.raises(ValueError, match="unsupported context fields"):
        repo.create_context_if_absent(_context_fields(action="buy"))


def test_weekly_context_query_is_bounded_and_chronological(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    first, _ = repo.create_context_if_absent(_context_fields())
    second, _ = repo.create_context_if_absent(
        _context_fields(
            signal_id=102,
            stock_code="MSFT",
            frozen_snapshot_hash="c" * 64,
            material_event_fingerprint="d" * 64,
        )
    )
    cutoff = first.created_at - timedelta(seconds=1)

    rows = repo.list_contexts_for_weekly_review(since=cutoff, limit=1)

    assert [row.id for row in rows] == [second.id]


def test_quality_outcome_upsert_keeps_one_row_per_versioned_horizon(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)

    pending, was_created = repo.upsert_quality_outcome(_outcome_fields())
    complete, repeated_created = repo.upsert_quality_outcome(
        _outcome_fields(
            eval_status="complete",
            start_price=100.0,
            end_close=105.0,
            stock_return_pct=5.0,
            decision_value_status="complete",
        )
    )

    assert was_created is True
    assert repeated_created is False
    assert complete.id == pending.id
    assert complete.eval_status == "complete"
    assert complete.stock_return_pct == 5.0
    assert len(repo.list_quality_outcomes(signal_id=101)) == 1


def test_completed_quality_outcome_cannot_be_rewritten_in_place(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    completed, was_created = repo.upsert_quality_outcome(
        _outcome_fields(
            eval_status="complete",
            start_price=100.0,
            end_close=105.0,
            stock_return_pct=5.0,
            decision_value_status="complete",
        )
    )

    repeated, repeated_created = repo.upsert_quality_outcome(
        _outcome_fields(
            eval_status="complete",
            start_price=100.0,
            end_close=95.0,
            stock_return_pct=-5.0,
            decision_value_status="complete",
        )
    )

    assert was_created is True
    assert repeated_created is False
    assert repeated.id == completed.id
    assert repeated.end_close == 105.0
    assert repeated.stock_return_pct == 5.0


def test_quality_outcomes_remain_separate_across_horizons_and_versions(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)
    repo.upsert_quality_outcome(_outcome_fields())
    repo.upsert_quality_outcome(_outcome_fields(horizon="20d", eval_window_days=20))
    repo.upsert_quality_outcome(_outcome_fields(engine_version="decision-quality-v2"))

    assert len(repo.list_quality_outcomes(signal_id=101)) == 3
    assert len(repo.list_quality_outcomes(signal_id=101, engine_version="decision-quality-v1")) == 2


def test_attribution_upsert_and_confirmed_listing(isolated_db) -> None:
    repo = DecisionQualityRepository(isolated_db)

    proposed, was_created = repo.upsert_attribution(_attribution_fields())
    confirmed, repeated_created = repo.upsert_attribution(
        _attribution_fields(
            category="timing_error",
            status="confirmed",
            summary="The entry trigger fired before evidence was complete.",
            user_note="Confirmed after review.",
        )
    )
    repo.upsert_attribution(_attribution_fields(signal_id=102, status="rejected"))

    assert was_created is True
    assert repeated_created is False
    assert confirmed.id == proposed.id
    assert confirmed.category == "timing_error"
    rows = repo.list_confirmed_attributions(engine_version="decision-quality-v1")
    assert [row.id for row in rows] == [confirmed.id]
