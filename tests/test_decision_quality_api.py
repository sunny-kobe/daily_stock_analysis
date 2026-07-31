# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.storage import (
    DatabaseManager,
    DecisionSignalFeedbackRecord,
    DecisionSignalRecord,
    StockDaily,
)


@pytest.fixture()
def client_and_db(tmp_path):
    old_env = os.environ.get("ENV_FILE")
    old_db = os.environ.get("DATABASE_PATH")
    env_path = tmp_path / ".env"
    db_path = tmp_path / "quality-api.db"
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    env_path.write_text(
        f"ADMIN_AUTH_ENABLED=false\nDATABASE_PATH={db_path}\nGEMINI_API_KEY=test\n",
        encoding="utf-8",
    )
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    app = create_app(static_dir=Path(static_dir))
    db = DatabaseManager.get_instance()
    try:
        yield TestClient(app), db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_env is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = old_env
        if old_db is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_db


def _seed_material_decision(db):
    with db.session_scope() as session:
        signal = DecisionSignalRecord(
            stock_code="AAPL",
            stock_name="Apple",
            market="us",
            source_type="analysis",
            trace_id="quality-api-101",
            decision_profile="balanced",
            market_phase="postmarket",
            trigger_source="portfolio",
            action="hold",
            action_label="Hold",
            horizon="20d",
            status="active",
            plan_quality="complete",
            expires_at=datetime(2026, 1, 9),
        )
        session.add(signal)
        session.flush()
        signal_id = int(signal.id)
    DecisionQualityRepository(db).create_context_if_absent(
        {
            "signal_id": signal_id,
            "account_id": 2,
            "market": "us",
            "stock_code": "AAPL",
            "instrument_type": "equity",
            "frozen_snapshot_hash": "a" * 64,
            "material_event_fingerprint": f"{signal_id:064x}",
            "position_action": "hold",
            "incremental_action": "wait",
            "confidence_by_horizon_json": json.dumps({"5d": 0.5, "20d": 0.6, "60d": 0.55}),
            "benchmark_market": "us",
            "benchmark_code": "SPY",
            "benchmark_type": "market_index",
            "benchmark_evidence_url": None,
            "benchmark_evidence_as_of": None,
            "decision_cutoff": datetime(2026, 1, 2, 21, 0),
            "context_status": "complete",
            "unable_reasons_json": "[]",
        }
    )
    with db.session_scope() as session:
        for code, start in (("AAPL", 100.0), ("SPY", 200.0)):
            for offset in range(6):
                close = start + offset
                session.add(
                    StockDaily(
                        code=code,
                        date=date(2026, 1, 2) + timedelta(days=offset),
                        open=close,
                        high=close + 1,
                        low=close - 1,
                        close=close,
                        data_source="fixture:qfq",
                    )
                )
    return signal_id


def test_quality_detail_and_run_expose_horizon_maturity_and_unable_reasons(client_and_db):
    client, db = client_and_db
    signal_id = _seed_material_decision(db)

    run = client.post(
        "/api/v1/decision-signals/quality/outcomes/run",
        json={"signal_id": signal_id, "horizons": ["5d"]},
    )
    detail = client.get(f"/api/v1/decision-signals/{signal_id}/quality")

    assert run.status_code == 200, run.text
    assert run.json()["items"][0]["eval_status"] == "complete"
    assert run.json()["items"][0]["instrument_type"] == "equity"
    assert detail.status_code == 200, detail.text
    assert detail.json()["context"]["instrument_type"] == "equity"
    assert detail.json()["context"]["user_instruction"] == "hold"
    assert detail.json()["evidence_snapshot"]["status"] == "missing"
    assert detail.json()["evidence_snapshot"]["display_status"] == "资料不足"
    assert detail.json()["evidence_snapshot"]["unable_reasons"] == [
        "legacy_evidence_snapshot_missing"
    ]
    by_horizon = {item["horizon"]: item for item in detail.json()["outcomes"]}
    assert by_horizon["5d"]["maturity"] == "mature"
    assert by_horizon["20d"]["maturity"] == "pending"
    assert by_horizon["20d"]["unable_reasons"] == ["horizon_not_mature"]


def test_quality_stats_zero_sample_is_empty_not_zero_performance(client_and_db):
    client, db = client_and_db
    _seed_material_decision(db)

    response = client.get("/api/v1/decision-signals/quality/stats?horizon=5d")

    assert response.status_code == 200, response.text
    assert response.json()["sample_size"] == 0
    assert response.json()["empty_state"] is True
    assert response.json()["performance"] is None


def test_quality_stats_exposes_sample_size_and_instrument_concentration(client_and_db):
    client, db = client_and_db
    signal_id = _seed_material_decision(db)
    client.post(
        "/api/v1/decision-signals/quality/outcomes/run",
        json={"signal_id": signal_id, "horizons": ["5d"]},
    )

    payload = client.get("/api/v1/decision-signals/quality/stats?horizon=5d").json()

    assert payload["sample_size"] == 1
    assert payload["horizon"] == "5d"
    assert payload["instrument_concentration"][0] == {
        "market": "us",
        "stock_code": "AAPL",
        "count": 1,
        "pct": 100.0,
    }
    assert payload["performance"]["status"] == "PROVISIONAL"


def test_quality_stats_requires_an_explicit_horizon(client_and_db):
    client, _db = client_and_db

    response = client.get("/api/v1/decision-signals/quality/stats")

    assert response.status_code == 422


def test_attribution_put_accepts_only_review_fields(client_and_db):
    client, db = client_and_db
    signal_id = _seed_material_decision(db)
    response = client.put(
        f"/api/v1/decision-signals/{signal_id}/attributions/5d",
        json={
            "category": "timing_error",
            "status": "confirmed",
            "summary": "Entry timing was early.",
            "evidence": ["5d MAE exceeded the trigger tolerance"],
            "counterexamples": ["20d horizon is not mature"],
            "user_note": "Keep provisional.",
        },
    )
    forbidden = client.put(
        f"/api/v1/decision-signals/{signal_id}/attributions/5d",
        json={
            "category": "timing_error",
            "status": "confirmed",
            "summary": "bad",
            "position_action": "exit",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["category"] == "timing_error"
    assert forbidden.status_code == 422


def test_weekly_review_shows_disagreement_without_activating_rules(client_and_db):
    client, db = client_and_db
    signal_id = _seed_material_decision(db)
    with db.session_scope() as session:
        session.add(
            DecisionSignalFeedbackRecord(
                signal_id=signal_id,
                feedback_value="useful",
                source="web",
                human_decision="modify",
                human_position_action="reduce",
                human_incremental_action="no_add",
            )
        )

    payload = client.get("/api/v1/decision-signals/quality/weekly-review").json()

    assert payload["material_decision_count"] == 1
    assert len(payload["ai_human_disagreements"]) == 1
    assert payload["confirmed_attribution_counts"] == {}
    assert payload["candidate_patterns"] == []
    assert payload["automatic_rules_activated"] is False
