# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from scripts.portfolio_strategy_backtest import main
from src.config import Config
from src.storage import DatabaseManager


ROOT = Path(__file__).resolve().parents[1]


def test_cli_writes_deterministic_report_without_starting_application(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "cli.db"
    output_path = tmp_path / "report.json"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    DatabaseManager.reset_instance()
    Config.reset_instance()
    try:
        exit_code = main(
            [
                "--dataset",
                str(ROOT / "tests/fixtures/strategy_validation/minimal_portfolio_events.json"),
                "--strategy",
                str(ROOT / "strategies/portfolio_hold_baseline_v1.json"),
                "--output",
                str(output_path),
            ]
        )
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()

    assert exit_code == 0
    report = json.loads(output_path.read_text())
    assert report["status"] == "completed"
    assert report["result"]["eligible_event_count"] == 2
    assert database_path.exists()
