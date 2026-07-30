# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.services.decision_quality_service import DecisionQualityService
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'learning.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _seed_case(db, *, signal_id, code, horizon="5d", product="equity", status="confirmed"):
    repo = DecisionQualityRepository(db)
    repo.create_context_if_absent({
        "signal_id": signal_id, "account_id": 2, "market": "us", "stock_code": code,
        "frozen_snapshot_hash": f"{signal_id + 100:064x}",
        "material_event_fingerprint": f"{signal_id:064x}",
        "position_action": "hold", "incremental_action": "wait",
        "confidence_by_horizon_json": '{}', "benchmark_market": "us",
        "benchmark_code": "SPY", "benchmark_type": "market_index",
        "benchmark_evidence_url": None, "benchmark_evidence_as_of": None,
        "decision_cutoff": datetime(2026, 1, 2), "context_status": "complete",
        "unable_reasons_json": '[]',
    })
    repo.upsert_quality_outcome({
        "signal_id": signal_id, "horizon": horizon, "engine_version": "decision-quality-v1",
        "eval_status": "complete", "unable_reason": None, "anchor_date": None,
        "eval_window_days": int(horizon[:-1]), "start_price": 100, "end_close": 95,
        "max_high": 102, "min_low": 90, "stock_return_pct": -5,
        "benchmark_start_price": 100, "benchmark_end_close": 100,
        "benchmark_return_pct": 0, "excess_return_pct": -5,
        "max_favorable_excursion_pct": 2, "max_adverse_excursion_pct": -10,
        "normalized_action_return_pct": -5, "decision_value_vs_hold_pct": 0,
        "hindsight_regret_pct": 5, "decision_value_status": "complete",
        "position_action": "hold", "incremental_action": "wait", "market": "us",
        "instrument_type": product, "data_quality_level": "qfq",
    })
    repo.upsert_attribution({
        "signal_id": signal_id, "horizon": horizon, "engine_version": "decision-quality-v1",
        "category": "timing_error", "status": status,
        "summary": "Timing was early.", "evidence_json": '["MAE"]',
        "counterexamples_json": json.dumps([f"counterexample-{signal_id}"]), "user_note": None,
    })


def test_learning_patterns_use_confirmed_rows_and_keep_segments_visible(isolated_db):
    _seed_case(isolated_db, signal_id=1, code="AAPL")
    _seed_case(isolated_db, signal_id=2, code="MSFT", horizon="20d")
    _seed_case(isolated_db, signal_id=3, code="QQQ", product="etf")
    _seed_case(isolated_db, signal_id=4, code="NVDA", status="proposed")

    patterns = DecisionQualityService(db_manager=isolated_db).get_learning_patterns()

    assert len(patterns) == 3
    assert {(item["horizon"], item["instrument_type"]) for item in patterns} == {
        ("5d", "equity"), ("20d", "equity"), ("5d", "etf")
    }
    assert all(item["status"] == "observed" for item in patterns)
    assert all(item["eligible_sample_count"] == 1 for item in patterns)
    assert all(item["counterexamples"] for item in patterns)


def test_learning_patterns_show_single_instrument_dominance_and_do_not_activate_rules(isolated_db):
    _seed_case(isolated_db, signal_id=1, code="AAPL")
    _seed_case(isolated_db, signal_id=2, code="AAPL")

    review = DecisionQualityService(db_manager=isolated_db).weekly_review()

    assert review["candidate_patterns"][0]["instrument_concentration_warning"] is True
    assert review["candidate_patterns"][0]["status"] == "observed"
    assert review["automatic_rules_activated"] is False
