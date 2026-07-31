# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from scripts.portfolio_strategy_build_dataset import main as build_dataset_main
from scripts.portfolio_strategy_backtest import main
from src.config import Config
from src.storage import DatabaseManager


ROOT = Path(__file__).resolve().parents[1]


def _refresh_source_hash(source: dict) -> None:
    payload = dict(source)
    payload.pop("source_snapshot_hash", None)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    source["source_snapshot_hash"] = hashlib.sha256(serialized.encode()).hexdigest()


def _write_valid_synthetic_source(path: Path, *, synthetic: bool = True) -> None:
    source = json.loads(
        (
            ROOT
            / "tests/fixtures/strategy_validation/synthetic_frozen_historical_source_v1.json"
        ).read_text()
    )
    candidate = source["candidates"][0]
    start = datetime.fromisoformat("2025-01-06T09:30:00+08:00")
    candidate["bars"].extend(
        {
            "timestamp": (start + timedelta(days=index)).isoformat(),
            "tradable": True,
            "open": 100.0 + index,
            "close": 101.0 + index,
            "high": 102.0 + index,
            "low": 99.0 + index,
            "as_of": (start + timedelta(days=index)).isoformat(),
            "source": "synthetic-fixture",
            "source_hash": f"instrument-{index}",
        }
        for index in range(61)
    )
    candidate["benchmark"]["bars"] = [
        {
            "timestamp": bar["timestamp"],
            "tradable": bar["tradable"],
            "open": 200.0 + index,
            "close": 201.0 + index,
            "high": 202.0 + index,
            "low": 199.0 + index,
            "as_of": bar["as_of"],
            "source": "synthetic-fixture",
            "source_hash": f"benchmark-{index}",
        }
        for index, bar in enumerate(candidate["bars"])
    ]
    source["synthetic"] = synthetic
    _refresh_source_hash(source)
    path.write_text(json.dumps(source), encoding="utf-8")


def test_cli_writes_synthetic_baseline_without_database_or_real_performance_claim(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "cli.db"
    source_path = tmp_path / "source.json"
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    _write_valid_synthetic_source(source_path)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    DatabaseManager.reset_instance()
    Config.reset_instance()
    try:
        assert build_dataset_main(["--source", str(source_path), "--output", str(dataset_path)]) == 0
        exit_code = main(
            [
                "--dataset",
                str(dataset_path),
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
    assert report["result"]["eligible_event_count"] == 1
    assert report["display_status"] == "测试数据，不代表真实成绩"
    assert not database_path.exists()


def test_cli_rejects_synthetic_dataset_as_real_performance(tmp_path) -> None:
    source_path = tmp_path / "source.json"
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    _write_valid_synthetic_source(source_path)
    assert build_dataset_main(["--source", str(source_path), "--output", str(dataset_path)]) == 0

    exit_code = main(
        [
            "--dataset",
            str(dataset_path),
            "--strategy",
            str(ROOT / "strategies/portfolio_hold_baseline_v1.json"),
            "--output",
            str(output_path),
            "--real-performance",
        ]
    )

    assert exit_code == 2
    assert not output_path.exists()


def test_cli_real_performance_requires_explicit_false_and_valid_dataset_hash(tmp_path) -> None:
    source_path = tmp_path / "source.json"
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    _write_valid_synthetic_source(source_path, synthetic=False)
    assert build_dataset_main(["--source", str(source_path), "--output", str(dataset_path)]) == 0

    dataset = json.loads(dataset_path.read_text())
    dataset["events"][0]["execution"]["price"] = 999.0
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    rejected = main(
        [
            "--dataset",
            str(dataset_path),
            "--strategy",
            str(ROOT / "strategies/portfolio_hold_baseline_v1.json"),
            "--output",
            str(output_path),
            "--real-performance",
        ]
    )

    assert rejected == 2
    assert not output_path.exists()

    dataset["events"][0]["execution"]["price"] = 100.0
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    accepted = main(
        [
            "--dataset",
            str(dataset_path),
            "--strategy",
            str(ROOT / "strategies/portfolio_hold_baseline_v1.json"),
            "--output",
            str(output_path),
            "--real-performance",
        ]
    )

    assert accepted == 0
    assert "display_status" not in json.loads(output_path.read_text())
