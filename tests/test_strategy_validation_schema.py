# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.strategy_validation import (
    StrategyTransitionRequest,
    StrategyValidationRun,
    StrategyVersionManifest,
)


def _manifest(**overrides):
    payload = {
        "strategy_key": "portfolio-current-policy",
        "version": "1.0.0",
        "name": "当前持仓策略",
        "change_summary": "冻结当前规则作为比较基线",
        "changed_dimension": "baseline",
        "markets": ["cn", "hk", "us"],
        "instrument_types": [
            "equity",
            "etf",
            "qdii",
            "adr_ads",
            "daily_leveraged_product",
        ],
        "horizons": ["5d", "20d", "60d"],
        "evaluation_mode": "forward_only",
        "policy": {"decision_contract": "portfolio-v1"},
        "cost_model": {
            "commission_bps": 3.0,
            "tax_bps": 0.0,
            "slippage_bps": 5.0,
            "fx_bps": 0.0,
            "product_cost_bps": 0.0,
        },
        "benchmark_policy": {
            "selection": "decision_time_market",
            "benchmarks": {"cn": "000300", "hk": "HSI", "us": "SPY"},
        },
        "status": "draft",
    }
    payload.update(overrides)
    return payload


def test_strategy_manifest_accepts_explicit_replay_and_cost_contract() -> None:
    manifest = StrategyVersionManifest.model_validate(_manifest())

    assert manifest.strategy_key == "portfolio-current-policy"
    assert manifest.evaluation_mode == "forward_only"
    assert manifest.cost_model.slippage_bps == 5.0


@pytest.mark.parametrize("evaluation_mode", ["historical", "live", "auto"])
def test_strategy_manifest_rejects_unsupported_evaluation_mode(evaluation_mode: str) -> None:
    with pytest.raises(ValidationError):
        StrategyVersionManifest.model_validate(_manifest(evaluation_mode=evaluation_mode))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("markets", []),
        ("instrument_types", []),
        ("horizons", []),
        ("changed_dimension", ["entry_rule", "risk_limit"]),
        ("status", "champion"),
    ],
)
def test_strategy_manifest_rejects_missing_scope_multiple_dimensions_and_automatic_status(
    field: str, value
) -> None:
    with pytest.raises(ValidationError):
        StrategyVersionManifest.model_validate(_manifest(**{field: value}))


def test_strategy_manifest_requires_every_cost_assumption() -> None:
    cost_model = _manifest()["cost_model"]
    del cost_model["slippage_bps"]

    with pytest.raises(ValidationError):
        StrategyVersionManifest.model_validate(_manifest(cost_model=cost_model))


def test_strategy_manifest_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StrategyVersionManifest.model_validate(_manifest(automatic_promotion=True))


def test_validation_run_requires_frozen_identity_and_forbids_unknown_fields() -> None:
    run = StrategyValidationRun.model_validate(
        {
            "strategy_key": "portfolio-current-policy",
            "strategy_version": "1.0.0",
            "validation_kind": "historical_backtest",
            "protocol": {"execution_price": "next_bar_open"},
            "dataset_hash": "a" * 64,
            "engine_version": "portfolio-strategy-v1",
            "status": "completed",
            "qualifying": True,
            "result": {"sample_count": 10},
        }
    )

    assert run.dataset_hash == "a" * 64
    with pytest.raises(ValidationError):
        StrategyValidationRun.model_validate(
            {**run.model_dump(), "automatic_promotion": True}
        )


def test_transition_requires_a_human_reason() -> None:
    with pytest.raises(ValidationError):
        StrategyTransitionRequest(to_status="simulation", human_reason=" ")
