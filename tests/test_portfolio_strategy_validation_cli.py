# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.test_portfolio_strategy_replay_service import _dataset, _strategy


SCRIPT = Path("scripts/portfolio_strategy_validation.py")


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_replay_and_report_cli_use_only_frozen_files(tmp_path) -> None:
    dataset_path = tmp_path / "dataset.json"
    strategy_path = tmp_path / "strategy.json"
    run_path = tmp_path / "run.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    strategy_path.write_text(json.dumps(_strategy()), encoding="utf-8")

    replay = _run(
        "replay",
        "--dataset",
        str(dataset_path),
        "--strategy",
        str(strategy_path),
        "--output",
        str(run_path),
    )
    report = _run("report", "--run", str(run_path))

    assert replay.returncode == 0, replay.stderr
    assert json.loads(run_path.read_text(encoding="utf-8"))["status"] == "complete"
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout)["event_count"] == 1


def test_build_dataset_excludes_contexts_without_full_frozen_replay_inputs(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "dataset.json"
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    result = _run(
        "build-dataset",
        "--from",
        "2025-01-01",
        "--to",
        "2025-12-31",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "portfolio-strategy-validation-dataset-v1"
    assert payload["network_calls"] == 0
    assert payload["legacy_markdown_included"] is False


def test_report_marks_zero_event_run_unable(tmp_path) -> None:
    run_path = tmp_path / "empty-run.json"
    run_path.write_text(
        json.dumps(
            {
                "run_id": "empty-run",
                "status": "insufficient_evidence",
                "event_count": 0,
                "network_calls": 0,
                "events": [],
            }
        ),
        encoding="utf-8",
    )

    result = _run("report", "--run", str(run_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["portfolio_metrics"] == "unable"
    assert payload["blockers"] == ["no_eligible_events"]
