# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _create_source_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE decision_signals (
            id INTEGER PRIMARY KEY,
            metadata_json TEXT,
            created_at DATETIME
        );
        CREATE TABLE decision_signal_quality_contexts (
            id INTEGER PRIMARY KEY,
            signal_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            instrument_type TEXT,
            frozen_snapshot_hash TEXT NOT NULL,
            material_event_fingerprint TEXT NOT NULL,
            position_action TEXT NOT NULL,
            incremental_action TEXT NOT NULL,
            benchmark_market TEXT,
            benchmark_code TEXT,
            benchmark_type TEXT,
            benchmark_evidence_url TEXT,
            benchmark_evidence_as_of DATETIME,
            decision_cutoff DATETIME NOT NULL,
            context_status TEXT NOT NULL,
            unable_reasons_json TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO decision_signals (id, metadata_json, created_at)
        VALUES (7, '{}', '2026-07-27 03:00:00');
        INSERT INTO decision_signal_quality_contexts (
            id, signal_id, account_id, market, stock_code, instrument_type,
            frozen_snapshot_hash, material_event_fingerprint,
            position_action, incremental_action,
            benchmark_market, benchmark_code, benchmark_type,
            decision_cutoff, context_status, unable_reasons_json
        ) VALUES (
            11, 7, 3, 'us', 'AAPL', 'equity',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            'hold', 'wait', 'us', 'SPX', 'market_index',
            '2026-07-27 02:30:00', 'complete', '[]'
        );
        """
    )
    connection.commit()
    connection.close()


def test_cli_exports_real_candidates_and_displays_gap_counts(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.portfolio_strategy_export_source import main

    database = tmp_path / "source.db"
    output = tmp_path / "source.json"
    _create_source_db(database)

    exit_code = main(
        [
            "--database",
            str(database),
            "--frozen-at",
            "2026-07-31T12:00:00+08:00",
            "--reporting-currency",
            "CNY",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    source = json.loads(output.read_text(encoding="utf-8"))
    assert source["synthetic"] is False
    assert source["candidate_count"] == 1
    assert len(source["source_snapshot_hash"]) == 64
    stdout = capsys.readouterr().out
    assert "真实候选: 1" in stdout
    assert "可直接回测: 0" in stdout
    assert "strategy_binding_missing=1" in stdout


def test_exported_candidates_remain_ineligible_without_historical_evidence(
    tmp_path: Path,
) -> None:
    from scripts.portfolio_strategy_export_source import main
    from src.services.portfolio_strategy_historical_sample_service import (
        PortfolioStrategyHistoricalSampleService,
    )

    database = tmp_path / "source.db"
    first_output = tmp_path / "source-first.json"
    second_output = tmp_path / "source-second.json"
    _create_source_db(database)
    args = [
        "--database",
        str(database),
        "--frozen-at",
        "2026-07-31T12:00:00+08:00",
        "--reporting-currency",
        "CNY",
    ]

    assert main([*args, "--output", str(first_output)]) == 0
    assert main([*args, "--output", str(second_output)]) == 0
    first = json.loads(first_output.read_text(encoding="utf-8"))
    second = json.loads(second_output.read_text(encoding="utf-8"))
    dataset = PortfolioStrategyHistoricalSampleService().build(source=first)

    assert first["source_snapshot_hash"] == second["source_snapshot_hash"]
    assert dataset["candidate_count"] == 1
    assert dataset["eligible_events"] == []
    assert dataset["excluded_events"] == [
        {
            "candidate_id": "dsa-quality-context-11",
            "reason_code": "decision_evidence_invalid",
        }
    ]
