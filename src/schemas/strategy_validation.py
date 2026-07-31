# -*- coding: utf-8 -*-
"""Strict contracts for versioned portfolio strategy validation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


StrategyStatus = Literal[
    "draft",
    "backtest_running",
    "backtest_failed",
    "simulation",
    "small_capital",
    "active",
    "retired",
]
StrategyValidationKind = Literal["historical_backtest", "forward_observation"]


class StrategyCostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commission_bps: float = Field(ge=0)
    tax_bps: float = Field(ge=0)
    slippage_bps: float = Field(ge=0)
    fx_bps: float = Field(ge=0)
    product_cost_bps: float = Field(ge=0)


class StrategyBenchmarkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: Literal["decision_time_market"]
    benchmarks: dict[Literal["cn", "hk", "us"], str]

    @field_validator("benchmarks")
    @classmethod
    def validate_benchmarks(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or any(not str(item).strip() for item in value.values()):
            raise ValueError("benchmark identity is required")
        return value


class StrategyVersionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_key: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^\d+\.\d+\.\d+$")
    name: str = Field(min_length=1, max_length=120)
    change_summary: str = Field(min_length=1, max_length=500)
    changed_dimension: Literal[
        "baseline",
        "entry_rule",
        "exit_rule",
        "risk_limit",
        "cost_model",
        "benchmark_policy",
    ]
    markets: list[Literal["cn", "hk", "us"]] = Field(min_length=1)
    instrument_types: list[
        Literal["equity", "etf", "qdii", "adr_ads", "daily_leveraged_product"]
    ] = Field(min_length=1)
    horizons: list[Literal["5d", "20d", "60d"]] = Field(min_length=1)
    evaluation_mode: Literal["historical_and_forward", "forward_only"]
    policy: dict[str, Any]
    cost_model: StrategyCostModel
    benchmark_policy: StrategyBenchmarkPolicy
    status: Literal["draft"] = "draft"

    @field_validator("markets", "instrument_types", "horizons")
    @classmethod
    def reject_duplicate_scope(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("strategy scope must not contain duplicates")
        return value


class StrategyValidationRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_key: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9][a-z0-9-]*$")
    strategy_version: str = Field(min_length=1, max_length=32, pattern=r"^\d+\.\d+\.\d+$")
    validation_kind: StrategyValidationKind
    protocol: dict[str, Any] = Field(min_length=1)
    dataset_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    engine_version: str = Field(min_length=1, max_length=64)
    status: Literal["completed", "failed", "unable"]
    qualifying: bool = False
    result: dict[str, Any]

    @model_validator(mode="after")
    def validate_qualification(self) -> "StrategyValidationRun":
        if self.qualifying and self.status != "completed":
            raise ValueError("only a completed validation run can qualify")
        return self


class StrategyTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_status: StrategyStatus
    human_reason: str = Field(min_length=1, max_length=1000)

    @field_validator("human_reason")
    @classmethod
    def validate_human_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("human_reason_required")
        return normalized
