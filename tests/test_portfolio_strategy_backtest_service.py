# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config import Config
from src.services.portfolio_strategy_backtest_service import PortfolioStrategyBacktestService
from src.services.portfolio_strategy_historical_sample_service import (
    PortfolioStrategyHistoricalSampleService,
)
from src.services.strategy_registry_service import canonical_json, sha256_json
from src.storage import DatabaseManager


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'strategy_backtest.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


@pytest.fixture()
def manifest():
    return json.loads((ROOT / "strategies/portfolio_hold_baseline_v1.json").read_text())


@pytest.fixture()
def dataset():
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
    source_payload = copy.deepcopy(source)
    source_payload.pop("source_snapshot_hash")
    source["source_snapshot_hash"] = sha256_json(source_payload)
    return PortfolioStrategyHistoricalSampleService().build(source=source)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda event: event.pop("decision_cutoff"), "missing_decision_cutoff"),
        (lambda event: event.update(symbol=""), "missing_identity"),
        (lambda event: event.update(instrument_type="unknown"), "missing_product_type"),
        (lambda event: event.pop("adjusted_price_identity"), "adjusted_price_identity_missing"),
        (lambda event: event.pop("benchmark"), "benchmark_identity_missing"),
        (lambda event: event.pop("fx"), "fx_evidence_missing"),
        (lambda event: event.update(structured_inputs={}), "structured_inputs_missing"),
        (
            lambda event: event["execution"].update(timestamp=event["decision_cutoff"]),
            "execution_not_after_cutoff",
        ),
    ],
)
def test_hard_gate_failure_returns_unable_without_partial_performance(
    isolated_db, manifest, dataset, mutate, reason
) -> None:
    broken = copy.deepcopy(dataset)
    mutate(broken["events"][0])

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
    )

    assert run["status"] == "unable"
    assert run["qualifying"] is False
    assert reason in run["result"]["unable_reasons"]
    assert run["result"]["buckets"] == []


def test_missing_cost_assumption_is_rejected_before_backtest(isolated_db, manifest, dataset) -> None:
    broken = copy.deepcopy(manifest)
    broken["cost_model"].pop("slippage_bps")

    with pytest.raises(ValidationError):
        PortfolioStrategyBacktestService(db_manager=isolated_db).run(
            strategy_manifest=broken,
            dataset=dataset,
        )


def test_historical_validator_makes_no_network_calls(isolated_db, manifest, dataset) -> None:
    with patch("socket.create_connection", side_effect=AssertionError("network called")) as network:
        run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
            strategy_manifest=manifest,
            dataset=dataset,
        )

    assert run["status"] == "completed"
    network.assert_not_called()


def test_rejects_execution_without_first_eligible_bar_provenance(
    isolated_db, manifest, dataset
) -> None:
    broken = copy.deepcopy(dataset)
    broken["events"][0]["execution"].pop("instrument_bar_hash")

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
    )

    assert run["status"] == "unable"
    assert "execution_provenance_missing" in run["result"]["unable_reasons"]
    assert run["result"]["buckets"] == []


def test_rejects_future_or_unhashed_point_in_time_evidence(isolated_db, manifest, dataset) -> None:
    broken = copy.deepcopy(dataset)
    broken["events"][0]["fx"]["as_of"] = "2025-01-03T09:30:00+08:00"
    broken["events"][0]["fx"]["source_hash"] = ""

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
    )

    assert run["status"] == "unable"
    assert "point_in_time_evidence_invalid" in run["result"]["unable_reasons"]
    assert run["result"]["buckets"] == []


def test_rejects_unaligned_benchmark_fx_or_adjustment_identity(isolated_db, manifest, dataset) -> None:
    broken = copy.deepcopy(dataset)
    broken["events"][0]["benchmark"]["adjusted_price_identity"] = "hfq"
    broken["events"][0]["fx"]["pair"] = "USD/CNY"

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
    )

    assert run["status"] == "unable"
    assert "adjustment_identity_unaligned" in run["result"]["unable_reasons"]
    assert "fx_identity_unaligned" in run["result"]["unable_reasons"]


def test_rejects_daily_reset_product_without_required_evidence(isolated_db, manifest, dataset) -> None:
    broken = copy.deepcopy(dataset)
    broken["events"][0]["instrument_type"] = "daily_leveraged_product"
    broken["events"][0]["product_evidence"]["instrument_type"] = "daily_leveraged_product"

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
    )

    assert run["status"] == "unable"
    assert "daily_reset_evidence_missing" in run["result"]["unable_reasons"]


def test_result_contains_eligible_event_set_hash(isolated_db, manifest, dataset) -> None:
    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=dataset,
    )

    assert run["status"] == "completed"
    assert run["result"]["eligible_event_set_hash"] == dataset["eligible_event_set_hash"]


def test_valid_run_records_the_builder_declared_dataset_hash(isolated_db, manifest, dataset) -> None:
    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=dataset,
        persist=False,
    )

    assert run["status"] == "completed"
    assert run["dataset_hash"] == dataset["dataset_hash"]
    assert run["protocol"]["dataset_hash"] == dataset["dataset_hash"]
    assert run["protocol"]["reporting_currency"] == "CNY"


def test_rejects_tampered_event_when_dataset_hash_is_stale(isolated_db, manifest, dataset) -> None:
    broken = copy.deepcopy(dataset)
    broken["events"][0]["execution"]["price"] = 999.0

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
        persist=False,
    )

    assert run["status"] == "unable"
    assert "dataset_hash_mismatch" in run["result"]["unable_reasons"]
    assert run["dataset_hash"] == sha256_json(broken)
    assert run["result"]["buckets"] == []


def test_daily_dataset_missing_underlying_evidence_is_unable_after_rehash(
    isolated_db, manifest, dataset
) -> None:
    valid = copy.deepcopy(dataset)
    event = valid["events"][0]
    event["instrument_type"] = "daily_leveraged_product"
    event["product_evidence"].update(
        instrument_type="daily_leveraged_product",
        reset_frequency="daily",
        underlying_identity="QQQ",
    )
    event["daily_reset_evidence"] = {
        "reset_frequency": "daily",
        "as_of": event["decision_cutoff"],
        "source": "synthetic-fixture",
        "source_hash": "reset-proof",
    }
    event["underlying_evidence"] = {
        "underlying_identity": "QQQ",
        "as_of": event["decision_cutoff"],
        "source": "synthetic-fixture",
        "source_hash": "underlying-proof",
    }
    payload = copy.deepcopy(valid)
    payload.pop("dataset_hash")
    valid["dataset_hash"] = sha256_json(payload)
    assert PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=valid,
        persist=False,
    )["status"] == "completed"

    broken = copy.deepcopy(valid)
    broken["events"][0].pop("underlying_evidence")
    payload = copy.deepcopy(broken)
    payload.pop("dataset_hash")
    broken["dataset_hash"] = sha256_json(payload)
    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
        persist=False,
    )

    assert run["status"] == "unable"
    assert "daily_underlying_evidence_missing" in run["result"]["unable_reasons"]


def test_rehashed_structured_inputs_or_daily_identity_mismatch_is_unable(
    isolated_db, manifest, dataset
) -> None:
    broken_inputs = copy.deepcopy(dataset)
    broken_inputs["events"][0]["structured_inputs"]["risk_gate"] = "blocked"
    payload = copy.deepcopy(broken_inputs)
    payload.pop("dataset_hash")
    broken_inputs["dataset_hash"] = sha256_json(payload)
    input_run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken_inputs,
        persist=False,
    )
    assert input_run["status"] == "unable"
    assert "decision_input_hash_mismatch" in input_run["result"]["unable_reasons"]

    daily = copy.deepcopy(dataset)
    event = daily["events"][0]
    event["instrument_type"] = "daily_leveraged_product"
    event["product_evidence"].update(
        instrument_type="daily_leveraged_product",
        reset_frequency="daily",
        underlying_identity="QQQ",
    )
    event["daily_reset_evidence"] = {
        "reset_frequency": "daily",
        "as_of": event["decision_cutoff"],
        "source": "synthetic-fixture",
        "source_hash": "reset-proof",
    }
    event["underlying_evidence"] = {
        "underlying_identity": "OTHER",
        "as_of": event["decision_cutoff"],
        "source": "synthetic-fixture",
        "source_hash": "underlying-proof",
    }
    payload = copy.deepcopy(daily)
    payload.pop("dataset_hash")
    daily["dataset_hash"] = sha256_json(payload)
    daily_run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=daily,
        persist=False,
    )
    assert daily_run["status"] == "unable"
    assert "daily_underlying_identity_unaligned" in daily_run["result"]["unable_reasons"]


def test_rejects_dataset_without_explicit_boolean_classification(isolated_db, manifest, dataset) -> None:
    broken = copy.deepcopy(dataset)
    broken.pop("synthetic")
    payload = copy.deepcopy(broken)
    payload.pop("dataset_hash")
    broken["dataset_hash"] = sha256_json(payload)

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=broken,
        persist=False,
    )

    assert run["status"] == "unable"
    assert "dataset_classification_invalid" in run["result"]["unable_reasons"]


def test_same_frozen_inputs_produce_byte_equivalent_results(isolated_db, manifest, dataset) -> None:
    service = PortfolioStrategyBacktestService(db_manager=isolated_db)

    first = service.run(strategy_manifest=manifest, dataset=dataset)
    second = service.run(strategy_manifest=manifest, dataset=dataset)

    assert first["run_hash"] == second["run_hash"]
    assert canonical_json(first["result"]) == canonical_json(second["result"])
    assert first["result"]["result_hash"] == second["result"]["result_hash"]


def test_metrics_are_separated_by_every_required_dimension(isolated_db, manifest, dataset) -> None:
    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=dataset,
    )

    result = run["result"]
    assert result["eligible_event_count"] == 1
    assert len(result["buckets"]) == 3
    assert "overall_performance" not in result
    dimensions = {tuple(bucket["dimensions"].items()) for bucket in result["buckets"]}
    assert {
        bucket["dimensions"]["horizon"] for bucket in result["buckets"]
    } == {"5d", "20d", "60d"}
    assert {bucket["dimensions"]["market"] for bucket in result["buckets"]} == {"cn"}
    assert {bucket["dimensions"]["product_type"] for bucket in result["buckets"]} == {"equity"}
    assert {bucket["dimensions"]["period"] for bucket in result["buckets"]} == {"development"}
    assert len(dimensions) == len(result["buckets"])
    required_metrics = {
        "sample_count",
        "win_rate_pct",
        "win_definition",
        "net_return_after_cost_pct",
        "benchmark_excess_pct",
        "maximum_drawdown_pct",
        "average_gain_pct",
        "average_loss_pct",
        "turnover_pct",
        "total_cost_pct",
        "unable_count",
    }
    assert all(required_metrics <= set(bucket["metrics"]) for bucket in result["buckets"])


def test_current_ai_policy_is_honestly_forward_only(isolated_db, dataset) -> None:
    manifest = json.loads((ROOT / "strategies/portfolio_current_policy_v1.json").read_text())

    run = PortfolioStrategyBacktestService(db_manager=isolated_db).run(
        strategy_manifest=manifest,
        dataset=dataset,
    )

    assert run["status"] == "completed"
    assert run["validation_kind"] == "historical_backtest"
    assert run["qualifying"] is False
    assert run["result"]["historical_status"] == "not_available"
    assert run["result"]["display_message"] == "历史回测不可用，等待模拟样本"
    assert run["result"]["buckets"] == []
