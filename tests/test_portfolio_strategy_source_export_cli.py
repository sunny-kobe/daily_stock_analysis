# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
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
        CREATE TABLE decision_signal_evidence_snapshots (
            id INTEGER PRIMARY KEY,
            signal_id INTEGER NOT NULL UNIQUE,
            quality_context_id INTEGER,
            schema_version TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            strategy_manifest_hash TEXT NOT NULL,
            decision_cutoff DATETIME NOT NULL,
            reporting_currency TEXT NOT NULL,
            structured_inputs_json TEXT NOT NULL,
            decision_input_hash TEXT NOT NULL,
            evidence_bundle_json TEXT NOT NULL,
            evidence_bundle_hash TEXT NOT NULL,
            readiness_status TEXT NOT NULL,
            blockers_json TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            created_at DATETIME
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
            'hold', 'wait', 'us', 'SPY', 'market_index',
            '2026-07-27 02:30:00', 'complete', '[]'
        );
        """
    )
    connection.commit()
    connection.close()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _insert_complete_sidecar(path: Path) -> None:
    structured_inputs = {
        "identity": {"account_id": 3, "market": "us", "symbol": "AAPL"},
        "position": {"quantity": 10.0, "price": 200.0, "currency": "USD"},
        "instrument": {
            "instrument_type": "equity",
            "trade_lot_size": 1.0,
            "adjustment_identity": "adjusted",
        },
        "benchmark": {
            "code": "SPY",
            "price": 500.0,
            "adjustment_identity": "adjusted",
        },
        "fx": {"pair": "USD/CNY", "rate": 7.2},
        "research_snapshot_hash": "a" * 64,
        "strategy_key": "portfolio-current-policy",
        "strategy_version": "1.0.0",
        "strategy_manifest_hash": "f" * 64,
    }

    def envelope(source: str, body: dict, source_hash: str) -> dict:
        return {
            "schema_version": "decision-source-envelope-v1",
            "as_of": "2026-07-27T02:30:00Z",
            "source": source,
            "source_version": "v1",
            "source_hash": source_hash,
            "body": body,
        }

    evidence_bundle = {
        "account": envelope(
            "portfolio_daily_snapshot",
            {"base_currency": "USD", "total_cash": 1000.0, "total_equity": 3000.0},
            "1" * 64,
        ),
        "position": envelope(
            "frozen-quote",
            {"currency": "USD", "quantity": 10.0, "last_price": 200.0},
            "2" * 64,
        ),
        "instrument": envelope(
            "verified-instrument-registry",
            {
                "market": "us",
                "symbol": "AAPL",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1.0,
                "adjustment_identity": "adjusted",
            },
            "3" * 64,
        ),
        "benchmark": envelope(
            "frozen-benchmark-bar",
            {
                "market": "us",
                "code": "SPY",
                "currency": "USD",
                "price": 500.0,
                "adjustment_identity": "adjusted",
            },
            "4" * 64,
        ),
        "fx": envelope("frozen-fx", {"pair": "USD/CNY", "rate": 7.2}, "5" * 64),
        "risk_policy": envelope(
            "portfolio-risk-policy",
            {"max_single_position_pct": 20.0},
            "6" * 64,
        ),
        "risk_budget": envelope(
            "portfolio-risk-budget",
            {"evaluated": True},
            "7" * 64,
        ),
        "cost_model": {
            "schema_version": "decision-source-envelope-v1",
            "as_of": "2026-07-27T02:30:00Z",
            "source": "strategy_registry",
            "source_version": "1.0.0",
            "source_hash": "f" * 64,
            "body": {"commission_bps": 3.0},
        },
        "decision_rationale": envelope(
            "decision_signal",
            {"position_action": "hold", "incremental_action": "wait"},
            "8" * 64,
        ),
        "research_context": [],
    }
    structured_inputs["account"] = {
        "reporting_currency": "USD",
        "total_cash": 1000.0,
        "total_equity": 3000.0,
        "evidence_hash": _canonical_hash(evidence_bundle["account"]),
    }
    for key in ("position", "instrument", "benchmark", "fx"):
        structured_inputs[key]["evidence_hash"] = _canonical_hash(evidence_bundle[key])
    structured_inputs["risk"] = {
        "policy": evidence_bundle["risk_policy"]["body"],
        "budget": evidence_bundle["risk_budget"]["body"],
        "policy_evidence_hash": _canonical_hash(evidence_bundle["risk_policy"]),
        "budget_evidence_hash": _canonical_hash(evidence_bundle["risk_budget"]),
    }
    structured_inputs["cost_model"] = {
        **evidence_bundle["cost_model"]["body"],
        "evidence_hash": _canonical_hash(evidence_bundle["cost_model"]),
    }
    structured_inputs["research_context_hashes"] = []
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO decision_signal_evidence_snapshots (
            id, signal_id, quality_context_id, schema_version,
            strategy_key, strategy_version, strategy_manifest_hash,
            decision_cutoff, reporting_currency,
            structured_inputs_json, decision_input_hash,
            evidence_bundle_json, evidence_bundle_hash,
            readiness_status, blockers_json, snapshot_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            21, 7, 11, "decision-evidence-snapshot-v1",
            "portfolio-current-policy", "1.0.0", "f" * 64,
            "2026-07-27 02:30:00", "CNY",
            json.dumps(structured_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _canonical_hash(structured_inputs),
            json.dumps(evidence_bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _canonical_hash(evidence_bundle),
            "complete", "[]", "a" * 64, "2026-07-27 03:00:01",
        ),
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


def test_cli_exports_complete_sidecar_without_recommendation_time_gaps(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.portfolio_strategy_export_source import main

    database = tmp_path / "source.db"
    output = tmp_path / "source.json"
    _create_source_db(database)
    _insert_complete_sidecar(database)

    assert main(
        [
            "--database", str(database),
            "--frozen-at", "2026-07-31T12:00:00+08:00",
            "--reporting-currency", "CNY",
            "--output", str(output),
        ]
    ) == 0

    source = json.loads(output.read_text(encoding="utf-8"))
    candidate = source["candidates"][0]
    assert candidate["decision"]["strategy_key"] == "portfolio-current-policy"
    assert "strategy_binding_missing" not in candidate["audit_gaps"]
    assert "price_bars_not_frozen" in candidate["audit_gaps"]
    stdout = capsys.readouterr().out
    assert "可直接回测: 0" in stdout


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
