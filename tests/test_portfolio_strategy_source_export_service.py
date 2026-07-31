# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
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
            "SPY",
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


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _complete_sidecar_payload() -> tuple[dict, dict]:
    strategy_manifest_hash = "f" * 64
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
        "strategy_manifest_hash": strategy_manifest_hash,
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
        "fx": envelope(
            "frozen-fx",
            {"pair": "USD/CNY", "rate": 7.2},
            "5" * 64,
        ),
        "risk_policy": envelope("portfolio-risk-policy", {"max_single_position_pct": 20.0}, "6" * 64),
        "risk_budget": envelope("portfolio-risk-budget", {"evaluated": True}, "7" * 64),
        "cost_model": {
            "schema_version": "decision-source-envelope-v1",
            "as_of": "2026-07-27T02:30:00Z",
            "source": "strategy_registry",
            "source_version": "1.0.0",
            "source_hash": strategy_manifest_hash,
            "body": {
                "commission_bps": 3.0,
                "tax_bps": 0.0,
                "slippage_bps": 5.0,
                "fx_bps": 0.0,
                "product_cost_bps": 0.0,
            },
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
    return structured_inputs, evidence_bundle


def _insert_complete_sidecar(
    path: Path,
    *,
    structured_inputs: dict | None = None,
    evidence_bundle: dict | None = None,
    decision_input_hash: str | None = None,
    evidence_bundle_hash: str | None = None,
    readiness_status: str = "complete",
) -> None:
    default_inputs, default_bundle = _complete_sidecar_payload()
    inputs = structured_inputs or default_inputs
    bundle = evidence_bundle or default_bundle
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
            21,
            7,
            11,
            "decision-evidence-snapshot-v1",
            "portfolio-current-policy",
            "1.0.0",
            "f" * 64,
            "2026-07-27 02:30:00",
            "CNY",
            json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            decision_input_hash or _canonical_hash(inputs),
            json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            evidence_bundle_hash or _canonical_hash(bundle),
            readiness_status,
            "[]",
            "a" * 64,
            "2026-07-27 03:00:01",
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
    assert "legacy_evidence_snapshot_missing" in source["candidates"][0]["audit_gaps"]
    assert "decision_persisted_after_cutoff" in source["candidates"][0]["audit_gaps"]
    assert source["source_audit"]["excluded_after_frozen_at"] == 1


def test_export_maps_complete_sidecar_and_keeps_unmatured_horizon_gaps(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    _insert_complete_sidecar(database)

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    candidate = source["candidates"][0]
    assert candidate["decision"]["strategy_key"] == "portfolio-current-policy"
    assert candidate["decision"]["strategy_version"] == "1.0.0"
    assert candidate["decision"]["decision_input_hash"] == _canonical_hash(
        candidate["structured_inputs"]
    )
    assert candidate["benchmark"]["symbol"] == "SPY"
    assert "strategy_binding_missing" not in candidate["audit_gaps"]
    assert "structured_inputs_missing" not in candidate["audit_gaps"]
    assert "benchmark_evidence_incomplete" not in candidate["audit_gaps"]
    assert "price_bars_not_frozen" in candidate["audit_gaps"]
    assert "development_validation_partition_missing" in candidate["audit_gaps"]


@pytest.mark.parametrize(
    ("sidecar_overrides", "expected_gap"),
    [
        ({"decision_input_hash": "0" * 64}, "decision_input_hash_mismatch"),
        ({"evidence_bundle_hash": "0" * 64}, "evidence_bundle_hash_mismatch"),
        ({"readiness_status": "insufficient_evidence"}, "evidence_snapshot_not_complete"),
    ],
)
def test_export_fails_closed_when_sidecar_integrity_drifts(
    tmp_path: Path,
    sidecar_overrides: dict,
    expected_gap: str,
) -> None:
    from src.services.portfolio_strategy_historical_sample_service import (
        PortfolioStrategyHistoricalSampleService,
    )
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    _insert_complete_sidecar(database, **sidecar_overrides)

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    candidate = source["candidates"][0]
    assert expected_gap in candidate["audit_gaps"]
    dataset = PortfolioStrategyHistoricalSampleService().build(source=source)
    assert dataset["eligible_events"] == []


def test_export_fails_closed_when_frozen_strategy_or_benchmark_drifts(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_historical_sample_service import (
        PortfolioStrategyHistoricalSampleService,
    )
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    structured_inputs, evidence_bundle = _complete_sidecar_payload()
    structured_inputs["strategy_version"] = "9.9.9"
    structured_inputs["research_snapshot_hash"] = "c" * 64
    evidence_bundle["benchmark"]["body"]["code"] = "SPX"
    _insert_complete_sidecar(
        database,
        structured_inputs=structured_inputs,
        evidence_bundle=evidence_bundle,
    )

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    candidate = source["candidates"][0]
    assert "strategy_binding_mismatch" in candidate["audit_gaps"]
    assert "structured_snapshot_hash_mismatch" in candidate["audit_gaps"]
    assert "benchmark_strategy_mismatch" in candidate["audit_gaps"]
    assert PortfolioStrategyHistoricalSampleService().build(source=source)["eligible_events"] == []


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("account", "evidence_hash"), "0" * 64),
        (("position", "evidence_hash"), "0" * 64),
        (("instrument", "evidence_hash"), "0" * 64),
        (("benchmark", "evidence_hash"), "0" * 64),
        (("fx", "evidence_hash"), "0" * 64),
        (("risk", "policy_evidence_hash"), "0" * 64),
        (("risk", "budget_evidence_hash"), "0" * 64),
        (("cost_model", "evidence_hash"), "0" * 64),
        (("research_context_hashes",), ["0" * 64]),
    ],
)
def test_export_fails_closed_when_structured_evidence_reference_drifts(
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: object,
) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    structured_inputs, evidence_bundle = _complete_sidecar_payload()
    target = structured_inputs
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    _insert_complete_sidecar(
        database,
        structured_inputs=structured_inputs,
        evidence_bundle=evidence_bundle,
    )

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    assert "evidence_reference_mismatch" in source["candidates"][0]["audit_gaps"]


def test_export_fails_closed_when_complete_sidecar_has_invalid_fx_envelope(
    tmp_path: Path,
) -> None:
    from src.services.portfolio_strategy_historical_sample_service import (
        PortfolioStrategyHistoricalSampleService,
    )
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    structured_inputs, evidence_bundle = _complete_sidecar_payload()
    evidence_bundle["fx"] = {
        "schema_version": "decision-source-envelope-v1",
        "as_of": "2026-07-27T02:30:00Z",
        "source": "frozen-fx",
        "source_version": "v1",
        "body": {"pair": "USD/CNY", "rate": 7.2},
    }
    _insert_complete_sidecar(
        database,
        structured_inputs=structured_inputs,
        evidence_bundle=evidence_bundle,
    )

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    candidate = source["candidates"][0]
    assert "fx_evidence_mismatch" in candidate["audit_gaps"]
    assert PortfolioStrategyHistoricalSampleService().build(source=source)["eligible_events"] == []


def test_export_fails_closed_when_complete_sidecar_loses_required_risk_evidence(
    tmp_path: Path,
) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    structured_inputs, evidence_bundle = _complete_sidecar_payload()
    evidence_bundle.pop("risk_policy")
    _insert_complete_sidecar(
        database,
        structured_inputs=structured_inputs,
        evidence_bundle=evidence_bundle,
    )

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    candidate = source["candidates"][0]
    assert "evidence_bundle_incomplete" in candidate["audit_gaps"]
    assert "strategy_binding_missing" in candidate["audit_gaps"]


def test_export_accepts_sidecar_frozen_before_quality_context_binding(
    tmp_path: Path,
) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    _insert_complete_sidecar(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE decision_signal_evidence_snapshots SET quality_context_id = NULL WHERE id = 21"
    )
    connection.commit()
    connection.close()

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    candidate = source["candidates"][0]
    assert candidate["decision"]["strategy_key"] == "portfolio-current-policy"
    assert "evidence_snapshot_quality_context_mismatch" not in candidate["audit_gaps"]


def test_export_excludes_sidecar_persisted_after_frozen_at(tmp_path: Path) -> None:
    from src.services.portfolio_strategy_source_export_service import (
        PortfolioStrategySourceExportService,
    )

    database = tmp_path / "source.db"
    _create_source_db(database)
    _insert_complete_sidecar(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE decision_signal_evidence_snapshots "
        "SET created_at = '2026-08-01 00:00:00' WHERE id = 21"
    )
    connection.commit()
    connection.close()

    source = PortfolioStrategySourceExportService(database).export(
        frozen_at="2026-07-31T12:00:00+08:00",
        reporting_currency="CNY",
    )

    assert source["candidate_count"] == 0
    assert source["source_audit"]["excluded_after_frozen_at"] == 2
    assert source["source_audit"]["excluded_after_frozen_at_reasons"] == {
        "decision_cutoff_after_frozen_at": 1,
        "evidence_snapshot_created_after_frozen_at": 2,
    }


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
