# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Config
from src.storage import DatabaseManager


@pytest.fixture()
def client(tmp_path):
    old_env = os.environ.get("ENV_FILE")
    old_db = os.environ.get("DATABASE_PATH")
    env_path = tmp_path / ".env"
    db_path = tmp_path / "strategy-api.db"
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
    try:
        yield TestClient(app), app
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


def _manifest():
    return {
        "strategy_key": "portfolio-hold-baseline",
        "version": "1.0.0",
        "name": "持有比较基线",
        "change_summary": "固定持有作为比较基准",
        "changed_dimension": "baseline",
        "markets": ["cn", "hk", "us"],
        "instrument_types": ["equity", "etf"],
        "horizons": ["5d", "20d", "60d"],
        "evaluation_mode": "historical_and_forward",
        "policy": {"display_instruction": "hold", "position_fraction": 1.0},
        "cost_model": {
            "commission_bps": 3.0,
            "tax_bps": 0.0,
            "slippage_bps": 5.0,
            "fx_bps": 0.0,
            "product_cost_bps": 0.0,
        },
        "benchmark_policy": {
            "selection": "decision_time_market",
            "benchmarks": {"cn": "000300", "hk": "HSI", "us": "SPY"},
        },
        "status": "draft",
    }


def _run():
    return {
        "strategy_key": "portfolio-hold-baseline",
        "strategy_version": "1.0.0",
        "validation_kind": "historical_backtest",
        "protocol": {"execution_price": "next_bar_open"},
        "dataset_hash": "a" * 64,
        "engine_version": "portfolio-strategy-v1",
        "status": "completed",
        "qualifying": True,
        "result": {
            "historical_status": "complete",
            "sample_count": 20,
            "unable_reasons": [],
        },
    }


def test_strategy_create_is_idempotent_and_versions_are_queryable(client) -> None:
    http, _app = client

    first = http.post("/api/v1/strategy-validation/strategies", json=_manifest())
    repeated = http.post("/api/v1/strategy-validation/strategies", json=_manifest())
    listed = http.get("/api/v1/strategy-validation/strategies")
    detail = http.get(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0"
    )

    assert first.status_code == 200, first.text
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["manifest_hash"] == first.json()["manifest_hash"]
    assert listed.json()["items"][0]["status_label"] == "待回测"
    assert detail.json()["change_summary"] == "固定持有作为比较基准"


def test_existing_strategy_version_cannot_be_rewritten(client) -> None:
    http, _app = client
    http.post("/api/v1/strategy-validation/strategies", json=_manifest())
    changed = _manifest()
    changed["change_summary"] = "试图改写同一版本"

    response = http.post("/api/v1/strategy-validation/strategies", json=changed)

    assert response.status_code == 409
    assert response.json()["error"] == "strategy_version_immutable"


def test_validation_run_is_idempotent_queryable_and_immutable(client) -> None:
    http, _app = client
    http.post("/api/v1/strategy-validation/strategies", json=_manifest())

    first = http.post("/api/v1/strategy-validation/runs", json=_run())
    repeated = http.post("/api/v1/strategy-validation/runs", json=_run())
    detail = http.get(f"/api/v1/strategy-validation/runs/{first.json()['run_id']}")
    strategy = http.get(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0"
    )

    assert first.status_code == 200, first.text
    assert repeated.json()["run_hash"] == first.json()["run_hash"]
    assert detail.json()["status_label"] == "已完成"
    assert detail.json()["result"]["sample_count"] == 20
    assert strategy.json()["latest_run"]["run_id"] == first.json()["run_id"]


def test_transition_requires_human_reason_and_completed_qualifying_run(client) -> None:
    http, _app = client
    http.post("/api/v1/strategy-validation/strategies", json=_manifest())
    missing_reason = http.post(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0/transition",
        json={"to_status": "backtest_running", "human_reason": ""},
    )
    started = http.post(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0/transition",
        json={"to_status": "backtest_running", "human_reason": "人工开始回测"},
    )
    blocked = http.post(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0/transition",
        json={"to_status": "simulation", "human_reason": "申请模拟观察"},
    )
    http.post("/api/v1/strategy-validation/runs", json=_run())
    running = http.get(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0"
    ).json()
    simulation = http.post(
        "/api/v1/strategy-validation/strategies/portfolio-hold-baseline/versions/1.0.0/transition",
        json={"to_status": "simulation", "human_reason": "回测合格，人工批准"},
    )

    assert missing_reason.status_code == 422
    assert started.json()["status_label"] == "回测中"
    assert running["allowed_transitions"] == ["backtest_failed", "simulation"]
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "qualifying_backtest_required"
    assert simulation.json()["status_label"] == "模拟观察"


def test_api_rejects_not_available_history_as_a_qualifying_backtest(client) -> None:
    http, _app = client
    http.post("/api/v1/strategy-validation/strategies", json=_manifest())
    unavailable_run = _run()
    unavailable_run["result"] = {
        "historical_status": "not_available",
        "unable_reasons": ["historical_inputs_not_replayable"],
    }

    response = http.post("/api/v1/strategy-validation/runs", json=unavailable_run)

    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"


def test_api_has_no_automatic_promotion_or_order_endpoint(client) -> None:
    _http, app = client
    paths = app.openapi()["paths"]

    strategy_paths = [path for path in paths if path.startswith("/api/v1/strategy-validation")]
    assert strategy_paths
    assert all("promote" not in path and "order" not in path for path in strategy_paths)
    assert all("champion" not in path and "challenger" not in path for path in strategy_paths)
