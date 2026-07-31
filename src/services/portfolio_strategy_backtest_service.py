# -*- coding: utf-8 -*-
"""Deterministic, no-network validation for frozen portfolio strategy events."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from src.schemas.strategy_validation import StrategyVersionManifest
from src.services.strategy_registry_service import (
    StrategyRegistryService,
    canonical_json,
    sha256_json,
)
from src.storage import DatabaseManager


ENGINE_VERSION = "portfolio-strategy-v1"
DATASET_SCHEMA_VERSION = "portfolio-strategy-events-v1"
_SUPPORTED_PRODUCTS = {"equity", "etf", "qdii", "adr_ads", "daily_leveraged_product"}
_HORIZON_FIELDS = ("end_close", "max_high", "min_low", "benchmark_end_close")


class PortfolioStrategyBacktestService:
    """Evaluate only caller-supplied frozen data and persist an immutable run."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        self.registry = StrategyRegistryService(db_manager=db_manager)

    def run(
        self,
        *,
        strategy_manifest: dict[str, Any],
        dataset: dict[str, Any],
        engine_version: str = ENGINE_VERSION,
    ) -> dict[str, Any]:
        manifest = StrategyVersionManifest.model_validate(strategy_manifest)
        self.registry.create_version(manifest.model_dump(mode="json"))
        dataset_hash = sha256_json(dataset)

        if manifest.evaluation_mode == "forward_only":
            result = self._with_result_hash(
                {
                    "historical_status": "not_available",
                    "display_message": "历史回测不可用，等待模拟样本",
                    "eligible_event_count": 0,
                    "evaluation_count": 0,
                    "buckets": [],
                    "unable_reasons": ["historical_inputs_not_replayable"],
                }
            )
            return self.registry.record_validation_run(
                self._run_payload(
                    manifest=manifest,
                    dataset_hash=dataset_hash,
                    engine_version=engine_version,
                    status="completed",
                    qualifying=False,
                    result=result,
                )
            )

        unable_reasons = self._validate_dataset(dataset=dataset, manifest=manifest)
        if unable_reasons:
            result = self._with_result_hash(
                {
                    "historical_status": "unable",
                    "eligible_event_count": 0,
                    "evaluation_count": 0,
                    "buckets": [],
                    "unable_reasons": unable_reasons,
                }
            )
            return self.registry.record_validation_run(
                self._run_payload(
                    manifest=manifest,
                    dataset_hash=dataset_hash,
                    engine_version=engine_version,
                    status="unable",
                    qualifying=False,
                    result=result,
                )
            )

        result = self._evaluate(dataset=dataset, manifest=manifest)
        return self.registry.record_validation_run(
            self._run_payload(
                manifest=manifest,
                dataset_hash=dataset_hash,
                engine_version=engine_version,
                status="completed",
                qualifying=True,
                result=result,
            )
        )

    @staticmethod
    def _run_payload(
        *,
        manifest: StrategyVersionManifest,
        dataset_hash: str,
        engine_version: str,
        status: str,
        qualifying: bool,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "strategy_key": manifest.strategy_key,
            "strategy_version": manifest.version,
            "validation_kind": "historical_backtest",
            "protocol": {
                "dataset_schema_version": DATASET_SCHEMA_VERSION,
                "execution_price": "first_bar_strictly_after_cutoff_open",
                "engine_version": engine_version,
                "network_access": "forbidden",
                "cost_model": manifest.cost_model.model_dump(mode="json"),
                "benchmark_policy": manifest.benchmark_policy.model_dump(mode="json"),
            },
            "dataset_hash": dataset_hash,
            "engine_version": engine_version,
            "status": status,
            "qualifying": qualifying,
            "result": result,
        }

    def _validate_dataset(
        self,
        *,
        dataset: dict[str, Any],
        manifest: StrategyVersionManifest,
    ) -> list[str]:
        reasons: set[str] = set()
        if not isinstance(dataset, dict) or dataset.get("schema_version") != DATASET_SCHEMA_VERSION:
            return ["dataset_schema_unsupported"]
        events = dataset.get("events")
        if not isinstance(events, list) or not events:
            return ["eligible_events_missing"]
        instruction = manifest.policy.get("display_instruction")
        if instruction != "hold" or manifest.policy.get("position_fraction") != 1.0:
            reasons.add("strategy_policy_not_replayable")

        event_ids: set[str] = set()
        for event in events:
            if not isinstance(event, dict):
                reasons.add("event_contract_invalid")
                continue
            event_id = str(event.get("event_id") or "").strip()
            if not event_id or event_id in event_ids:
                reasons.add("event_identity_invalid")
            event_ids.add(event_id)
            cutoff = self._aware_datetime(event.get("decision_cutoff"))
            if cutoff is None:
                reasons.add("missing_decision_cutoff")
            market = str(event.get("market") or "").strip().lower()
            symbol = str(event.get("symbol") or "").strip()
            if market not in manifest.markets or not symbol:
                reasons.add("missing_identity")
            product_type = str(event.get("instrument_type") or "").strip()
            if product_type not in _SUPPORTED_PRODUCTS or product_type not in manifest.instrument_types:
                reasons.add("missing_product_type")
            if not str(event.get("adjusted_price_identity") or "").strip():
                reasons.add("adjusted_price_identity_missing")

            benchmark = event.get("benchmark")
            if not isinstance(benchmark, dict) or not str(benchmark.get("symbol") or "").strip():
                reasons.add("benchmark_identity_missing")
            elif not str(benchmark.get("adjusted_price_identity") or "").strip():
                reasons.add("benchmark_adjustment_identity_missing")
            elif benchmark.get("symbol") != manifest.benchmark_policy.benchmarks.get(market):
                reasons.add("benchmark_policy_mismatch")

            fx = event.get("fx")
            if not isinstance(fx, dict):
                reasons.add("fx_evidence_missing")
            else:
                fx_as_of = self._aware_datetime(fx.get("as_of"))
                if (
                    not str(fx.get("pair") or "").strip()
                    or self._positive(fx.get("rate")) is None
                    or fx_as_of is None
                ):
                    reasons.add("fx_evidence_missing")
                elif cutoff is not None and fx_as_of > cutoff:
                    reasons.add("fx_evidence_after_cutoff")

            structured = event.get("structured_inputs")
            replayable_keys = (
                set(structured) - {"ai_analysis", "free_text"}
                if isinstance(structured, dict)
                else set()
            )
            if not replayable_keys:
                reasons.add("structured_inputs_missing")

            if not str(event.get("market_regime") or "").strip():
                reasons.add("market_regime_missing")
            if event.get("period") not in {"development", "validation"}:
                reasons.add("period_identity_missing")

            execution = event.get("execution")
            execution_time = (
                self._aware_datetime(execution.get("timestamp"))
                if isinstance(execution, dict)
                else None
            )
            if (
                not isinstance(execution, dict)
                or execution_time is None
                or self._positive(execution.get("price")) is None
                or self._positive(execution.get("benchmark_price")) is None
            ):
                reasons.add("execution_contract_missing")
            elif cutoff is not None and execution_time <= cutoff:
                reasons.add("execution_not_after_cutoff")

            horizons = event.get("horizon_results")
            if not isinstance(horizons, dict):
                reasons.add("horizon_results_missing")
                continue
            for horizon in manifest.horizons:
                values = horizons.get(horizon)
                if not isinstance(values, dict) or any(
                    self._positive(values.get(field)) is None for field in _HORIZON_FIELDS
                ):
                    reasons.add(f"{horizon}_result_missing")
        return sorted(reasons)

    def _evaluate(
        self,
        *,
        dataset: dict[str, Any],
        manifest: StrategyVersionManifest,
    ) -> dict[str, Any]:
        grouped: dict[tuple[str, ...], list[dict[str, float]]] = defaultdict(list)
        instruction = str(manifest.policy["display_instruction"])
        total_cost_bps = sum(manifest.cost_model.model_dump().values())
        for event in dataset["events"]:
            execution = event["execution"]
            start = float(execution["price"])
            benchmark_start = float(execution["benchmark_price"])
            turnover_pct = 0.0 if instruction == "hold" else 100.0
            cost_pct = turnover_pct / 100.0 * total_cost_bps / 100.0
            for horizon in manifest.horizons:
                values = event["horizon_results"][horizon]
                gross_return = (float(values["end_close"]) / start - 1.0) * 100.0
                benchmark_return = (
                    float(values["benchmark_end_close"]) / benchmark_start - 1.0
                ) * 100.0
                net_return = gross_return - cost_pct
                key = (
                    horizon,
                    str(event["market"]),
                    str(event["instrument_type"]),
                    instruction,
                    str(event["market_regime"]),
                    str(event["period"]),
                )
                grouped[key].append(
                    {
                        "net_return": net_return,
                        "excess": net_return - benchmark_return,
                        "drawdown": (float(values["min_low"]) / start - 1.0) * 100.0,
                        "turnover": turnover_pct,
                        "cost": cost_pct,
                    }
                )

        buckets = []
        for key in sorted(grouped):
            rows = grouped[key]
            returns = [row["net_return"] for row in rows]
            gains = [value for value in returns if value > 0]
            losses = [value for value in returns if value < 0]
            dimensions = dict(
                zip(
                    ("horizon", "market", "product_type", "instruction", "market_regime", "period"),
                    key,
                )
            )
            buckets.append(
                {
                    "dimensions": dimensions,
                    "metrics": {
                        "sample_count": len(rows),
                        "win_rate_pct": self._rounded(
                            sum(value > 0 for value in returns) / len(rows) * 100.0
                        ),
                        "win_definition": "持有期间扣除成本后的收益大于 0",
                        "net_return_after_cost_pct": self._average(returns),
                        "benchmark_excess_pct": self._average([row["excess"] for row in rows]),
                        "maximum_drawdown_pct": self._rounded(min(row["drawdown"] for row in rows)),
                        "average_gain_pct": self._average(gains) if gains else None,
                        "average_loss_pct": self._average(losses) if losses else None,
                        "turnover_pct": self._average([row["turnover"] for row in rows]),
                        "total_cost_pct": self._rounded(sum(row["cost"] for row in rows)),
                        "unable_count": 0,
                    },
                }
            )
        return self._with_result_hash(
            {
                "historical_status": "complete",
                "eligible_event_count": len(dataset["events"]),
                "evaluation_count": sum(len(rows) for rows in grouped.values()),
                "buckets": buckets,
                "unable_reasons": [],
            }
        )

    @staticmethod
    def _aware_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    @staticmethod
    def _positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _rounded(value: float) -> float:
        return round(float(value), 10)

    @classmethod
    def _average(cls, values: list[float]) -> float:
        return cls._rounded(sum(values) / len(values))

    @staticmethod
    def _with_result_hash(result: dict[str, Any]) -> dict[str, Any]:
        return {**result, "result_hash": sha256_json(result)}
