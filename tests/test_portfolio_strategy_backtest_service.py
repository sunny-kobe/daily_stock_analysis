# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config import Config
from src.services.portfolio_strategy_backtest_service import PortfolioStrategyBacktestService
from src.services.strategy_registry_service import canonical_json
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
    return json.loads(
        (ROOT / "tests/fixtures/strategy_validation/minimal_portfolio_events.json").read_text()
    )


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
    assert result["eligible_event_count"] == 2
    assert len(result["buckets"]) == 6
    assert "overall_performance" not in result
    dimensions = {tuple(bucket["dimensions"].items()) for bucket in result["buckets"]}
    assert {
        bucket["dimensions"]["horizon"] for bucket in result["buckets"]
    } == {"5d", "20d", "60d"}
    assert {bucket["dimensions"]["market"] for bucket in result["buckets"]} == {"cn", "us"}
    assert {bucket["dimensions"]["product_type"] for bucket in result["buckets"]} == {
        "equity",
        "etf",
    }
    assert {bucket["dimensions"]["period"] for bucket in result["buckets"]} == {
        "development",
        "validation",
    }
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
