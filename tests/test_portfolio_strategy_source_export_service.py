# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


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
        """
    )
    connection.execute(
        "INSERT INTO decision_signals (id, metadata_json, created_at) VALUES (?, ?, ?)",
        (
            7,
            json.dumps(
                {
                    "portfolio_snapshot_hash": "a" * 64,
                    "portfolio_decision": {
                        "position_action": "hold",
                        "incremental_action": "wait",
                    },
                }
            ),
            "2026-07-27 03:00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO decision_signal_quality_contexts (
            id, signal_id, account_id, market, stock_code, instrument_type,
            frozen_snapshot_hash, material_event_fingerprint,
            position_action, incremental_action,
            benchmark_market, benchmark_code, benchmark_type,
            benchmark_evidence_url, benchmark_evidence_as_of,
            decision_cutoff, context_status, unable_reasons_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            11,
            7,
            3,
            "us",
            "AAPL",
            "equity",
            "a" * 64,
            "b" * 64,
            "hold",
            "wait",
            "us",
            "SPX",
            "market_index",
            None,
            None,
            "2026-07-27 02:30:00",
            "complete",
            "[]",
            "2026-07-27 03:00:01",
            "2026-07-27 03:00:01",
        ),
    )
    connection.execute(
        """
        INSERT INTO decision_signal_quality_contexts (
            id, signal_id, account_id, market, stock_code, instrument_type,
            frozen_snapshot_hash, material_event_fingerprint,
            position_action, incremental_action,
            decision_cutoff, context_status, unable_reasons_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            12,
            7,
            3,
            "us",
            "AAPL",
            "equity",
            "a" * 64,
            "c" * 64,
            "hold",
            "wait",
            "2026-08-01 02:30:00",
            "complete",
            "[]",
        ),
    )
    connection.commit()
    connection.close()


def test_export_uses_readonly_sqlite_and_preserves_missing_evidence(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    before = database.read_bytes()
    real_connect = sqlite3.connect

    with patch(
        "src.services.portfolio_strategy_source_export_service.sqlite3.connect",
        wraps=real_connect,
    ) as connect:
        source = PortfolioStrategySourceExportService(database).export(
            frozen_at="2026-07-31T12:00:00+08:00",
            reporting_currency="CNY",
        )

    assert connect.call_args.args[0].endswith("?mode=ro")
    assert connect.call_args.kwargs == {"uri": True}
    assert database.read_bytes() == before
    assert source["schema_version"] == "frozen-historical-source-v1"
    assert source["synthetic"] is False
    assert source["candidate_count"] == 1
    assert source["candidates"][0]["candidate_id"] == "dsa-quality-context-11"
    assert source["candidates"][0]["decision"]["position_action"] == "hold"
    assert source["candidates"][0]["decision"]["decision_cutoff"] == "2026-07-27T02:30:00+00:00"
    assert source["candidates"][0]["decision"]["as_of"] == "2026-07-27T03:00:01+00:00"
    assert "strategy_key" not in source["candidates"][0]["decision"]
    assert "decision_input_hash" not in source["candidates"][0]["decision"]
    assert "structured_inputs" not in source["candidates"][0]
    assert "strategy_binding_missing" in source["candidates"][0]["audit_gaps"]
    assert "structured_inputs_missing" in source["candidates"][0]["audit_gaps"]
    assert "decision_persisted_after_cutoff" in source["candidates"][0]["audit_gaps"]
    assert source["source_audit"]["excluded_after_frozen_at"] == 1


def test_export_excludes_records_persisted_after_frozen_at_with_audit_reasons(
    tmp_path: Path,
) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO decision_signals (id, metadata_json, created_at) VALUES (?, ?, ?)",
        (8, "{}", "2026-08-01 00:00:00"),
    )
    rows = [
        (14, 7, "2026-08-01 00:00:00", "2026-07-27 03:00:01"),
        (15, 7, "2026-07-27 03:00:01", "2026-08-01 00:00:00"),
        (16, 8, "2026-07-27 03:00:01", "2026-07-27 03:00:01"),
    ]
    for context_id, signal_id, created_at, updated_at in rows:
        connection.execute(
            """
            INSERT INTO decision_signal_quality_contexts (
                id, signal_id, account_id, market, stock_code, instrument_type,
                frozen_snapshot_hash, material_event_fingerprint,
                position_action, incremental_action,
                decision_cutoff, context_status, unable_reasons_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context_id,
                signal_id,
                3,
                "us",
                f"TEST{context_id}",
                "equity",
                "d" * 64,
                "e" * 64,
                "hold",
                "wait",
                "2026-07-27 02:30:00",
                "complete",
                "[]",
                created_at,
                updated_at,
            ),
        )
    connection.commit()
    connection.close()

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    assert [item["candidate_id"] for item in source["candidates"]] == [
        "dsa-quality-context-11"
    ]
    assert source["source_audit"]["excluded_after_frozen_at"] == 4
    assert source["source_audit"]["excluded_after_frozen_at_reasons"] == {
        "context_created_after_frozen_at": 1,
        "context_updated_after_frozen_at": 1,
        "decision_cutoff_after_frozen_at": 1,
        "signal_created_after_frozen_at": 1,
    }


def test_same_database_and_parameters_produce_same_source_hash(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    service = PortfolioStrategySourceExportService(database)

    first = service.export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )
    second = service.export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    assert first == second
    assert len(first["source_snapshot_hash"]) == 64


@pytest.mark.parametrize("frozen_at", ["2026-07-31T23:59:59", "not-a-time"])
def test_export_rejects_invalid_or_timezone_naive_frozen_at(
    tmp_path: Path,
    frozen_at: str,
) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)

    with pytest.raises(ValueError, match="frozen_at_invalid"):
        PortfolioStrategySourceExportService(database).export(
            frozen_at=frozen_at,
            reporting_currency="CNY",
        )


def test_export_rejects_future_frozen_at(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)

    with pytest.raises(ValueError, match="frozen_at_in_future"):
        PortfolioStrategySourceExportService(database).export(
            frozen_at="2999-01-01T00:00:00+00:00",
            reporting_currency="CNY",
        )


def test_export_rejects_missing_required_tables(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()

    with pytest.raises(ValueError, match="source_database_contract_missing"):
        PortfolioStrategySourceExportService(database).export(
            frozen_at="2026-07-31T12:00:00+08:00",
            reporting_currency="CNY",
        )


def test_export_keeps_orphaned_and_invalid_cutoff_contexts_as_auditable_candidates(
    tmp_path: Path,
) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    connection = sqlite3.connect(database)
    connection.execute(
        """
        INSERT INTO decision_signal_quality_contexts (
            id, signal_id, account_id, market, stock_code, instrument_type,
            frozen_snapshot_hash, material_event_fingerprint,
            position_action, incremental_action,
            decision_cutoff, context_status, unable_reasons_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            13,
            999,
            3,
            "us",
            "MSFT",
            "equity",
            "d" * 64,
            "e" * 64,
            "hold",
            "wait",
            "invalid-cutoff",
            "insufficient_evidence",
            "[]",
        ),
    )
    connection.commit()
    connection.close()

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    orphan = next(item for item in source["candidates"] if item["candidate_id"] == "dsa-quality-context-13")
    assert "decision_cutoff" not in orphan["decision"]
    assert "decision_cutoff_invalid" in orphan["audit_gaps"]
    assert "decision_signal_missing" in orphan["audit_gaps"]
    assert source["source_audit"]["invalid_decision_cutoff"] == 1
