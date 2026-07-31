# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from scripts.portfolio_strategy_build_dataset import main


ROOT = Path(__file__).resolve().parents[1]


def test_cli_writes_dataset_manifest_and_displays_candidate_counts(tmp_path, capsys) -> None:
    output_path = tmp_path / "dataset.json"

    exit_code = main(
        [
            "--source",
            str(ROOT / "tests/fixtures/strategy_validation/synthetic_frozen_historical_source_v1.json"),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    dataset = json.loads(output_path.read_text())
    assert dataset["candidate_count"] == 1
    assert dataset["eligible_events"] == []
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "execution_bar_missing"}
    ]
    stdout = capsys.readouterr().out
    assert "候选记录: 1" in stdout
    assert "可用样本: 0" in stdout
