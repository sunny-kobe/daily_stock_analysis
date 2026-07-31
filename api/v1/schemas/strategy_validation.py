# -*- coding: utf-8 -*-
"""API contracts for strategy versions and validation scorecards."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from src.schemas.strategy_validation import (
    StrategyBenchmarkPolicy,
    StrategyCostModel,
    StrategyTransitionRequest,
    StrategyValidationRun,
    StrategyVersionManifest,
)


class StrategyCreateRequest(StrategyVersionManifest):
    pass


class StrategyRunCreateRequest(StrategyValidationRun):
    pass


class StrategyTransitionBody(StrategyTransitionRequest):
    pass


class StrategyValidationRunItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    strategy_key: str
    strategy_version: str
    validation_kind: str
    protocol: dict[str, Any]
    dataset_hash: str
    engine_version: str
    status: str
    status_label: str
    qualifying: bool
    result: dict[str, Any]
    run_hash: str
    created_at: str


class StrategyVersionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_key: str
    version: str
    name: str
    change_summary: str
    changed_dimension: str
    markets: list[str]
    instrument_types: list[str]
    horizons: list[str]
    evaluation_mode: str
    policy: dict[str, Any]
    cost_model: StrategyCostModel
    benchmark_policy: StrategyBenchmarkPolicy
    status: str
    status_label: str
    allowed_transitions: list[str]
    latest_run: StrategyValidationRunItem | None = None
    manifest_hash: str
    created_at: str


class StrategyVersionListResponse(BaseModel):
    items: list[StrategyVersionItem]


class StrategyTransitionResponse(BaseModel):
    strategy_key: str
    version: str
    from_status: str
    status: str
    status_label: str
    human_reason: str
    transition_id: int
