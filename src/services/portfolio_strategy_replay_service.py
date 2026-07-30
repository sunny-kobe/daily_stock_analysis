# -*- coding: utf-8 -*-
"""Deterministic point-in-time replay over fully frozen local artifacts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from src.schemas.portfolio_decision_quality import INCREMENTAL_ACTIONS, POSITION_ACTIONS
from src.schemas.portfolio_strategy_validation import (
    freeze_strategy_manifest,
    strategy_manifest_hash,
    validate_point_in_time_manifest,
)


DATASET_SCHEMA_VERSION = "portfolio-strategy-validation-dataset-v1"


class PortfolioStrategyReplayService:
    """Replay reproducible policy layers without current data or network access."""

    def replay(
        self,
        *,
        dataset: Mapping[str, Any],
        strategy: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(dataset, Mapping):
            raise TypeError("dataset must be a mapping")
        if dataset.get("schema_version") != DATASET_SCHEMA_VERSION:
            raise ValueError("unsupported_dataset_schema")
        frozen_strategy = freeze_strategy_manifest(strategy)
        events = dataset.get("events")
        if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
            raise TypeError("events must be a sequence")
        horizons = dataset.get("horizons") or ["5d", "20d", "60d"]
        if any(horizon not in {"5d", "20d", "60d"} for horizon in horizons):
            raise ValueError("unsupported_horizon")

        replayed = [
            self._replay_event(event, strategy=frozen_strategy, horizons=list(horizons))
            for event in events
        ]
        identity = {
            "dataset_hash": strategy_manifest_hash(dataset),
            "strategy_manifest_hash": frozen_strategy["manifest_hash"],
            "event_ids": [event["event_id"] for event in replayed],
        }
        result = {
            "schema_version": "portfolio-strategy-validation-run-v1",
            "run_id": strategy_manifest_hash(identity),
            "status": "complete" if replayed else "insufficient_evidence",
            "strategy_version": frozen_strategy["strategy_version"],
            "strategy_manifest_hash": frozen_strategy["manifest_hash"],
            "dataset_hash": identity["dataset_hash"],
            "event_count": len(replayed),
            "network_calls": 0,
            "events": replayed,
        }
        if not replayed:
            result["blockers"] = ["no_eligible_events"]
        return result

    def preflight(self, *, dataset: Mapping[str, Any], strategy: Mapping[str, Any]) -> dict[str, Any]:
        try:
            result = self.replay(dataset=dataset, strategy=strategy)
        except (TypeError, ValueError) as exc:
            return {"status": "NOT_READY", "blockers": [str(exc)], "network_calls": 0}
        if result["event_count"] == 0:
            return {
                "status": "NOT_READY",
                "event_count": 0,
                "blockers": ["no_eligible_events"],
                "network_calls": 0,
            }
        return {
            "status": "READY",
            "event_count": result["event_count"],
            "dataset_hash": result["dataset_hash"],
            "network_calls": 0,
        }

    @staticmethod
    def build_dataset(
        *,
        contexts: Sequence[Any],
        cutoff_from: str,
        cutoff_to: str,
    ) -> dict[str, Any]:
        exclusions = [
            {
                "signal_id": getattr(context, "signal_id", None),
                "reason": "frozen_historical_inputs_incomplete",
            }
            for context in contexts
        ]
        return {
            "schema_version": DATASET_SCHEMA_VERSION,
            "cutoff_from": cutoff_from,
            "cutoff_to": cutoff_to,
            "horizons": ["5d", "20d", "60d"],
            "events": [],
            "exclusions": exclusions,
            "network_calls": 0,
            "legacy_markdown_included": False,
        }

    def _replay_event(
        self,
        event: Any,
        *,
        strategy: Mapping[str, Any],
        horizons: list[str],
    ) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise TypeError("event must be a mapping")
        payload = deepcopy(dict(event))
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event_id_missing")
        point_in_time = validate_point_in_time_manifest(
            {
                "cutoff": payload.get("cutoff"),
                "evidence_artifacts": payload.get("evidence_artifacts", []),
            }
        )
        cutoff = self._timestamp(point_in_time["cutoff"], field="cutoff")

        holdings = payload.get("frozen_holdings")
        if not isinstance(holdings, Mapping):
            raise ValueError("frozen_holdings_missing")
        if payload.get("position_action") not in POSITION_ACTIONS:
            raise ValueError("position_action_missing")
        if payload.get("incremental_action") not in INCREMENTAL_ACTIONS:
            raise ValueError("incremental_action_missing")
        strategy_identity = payload.get("strategy_identity")
        if not isinstance(strategy_identity, Mapping):
            raise ValueError("strategy_identity_missing")
        if strategy_identity.get("manifest_hash") != strategy["manifest_hash"]:
            raise ValueError("strategy_identity_mismatch")

        instrument = payload.get("instrument_identity")
        if not isinstance(instrument, Mapping):
            raise ValueError("instrument_identity_missing")
        if instrument.get("source") != "frozen":
            raise ValueError("current_registry_forbidden")
        frozen_at = self._timestamp(instrument.get("frozen_at"), field="instrument_frozen_at")
        if frozen_at > cutoff:
            raise ValueError("instrument_identity_after_cutoff")
        if not instrument.get("identity_hash"):
            raise ValueError("instrument_identity_hash_missing")

        benchmark = payload.get("benchmark")
        if not isinstance(benchmark, Mapping):
            raise ValueError("benchmark_identity_missing")
        selected_at = self._timestamp(
            benchmark.get("selected_at"), field="benchmark_selected_at"
        )
        if selected_at > cutoff:
            raise ValueError("benchmark_selected_after_cutoff")

        bars = payload.get("bars")
        if not isinstance(bars, Mapping):
            raise ValueError("bars_missing")
        base_adjustment = bars.get("adjustment_identity")
        if not base_adjustment:
            raise ValueError("adjustment_identity_missing")
        observation = bars.get("observation")
        execution = bars.get("shadow_execution")
        forward = bars.get("forward")
        if not isinstance(observation, Mapping) or not isinstance(execution, Mapping):
            raise ValueError("execution_anchor_unverified")
        if isinstance(forward, (str, bytes)) or not isinstance(forward, Sequence):
            raise ValueError("forward_bars_missing")
        adjustment_values = {
            base_adjustment,
            observation.get("adjustment_identity"),
            execution.get("adjustment_identity"),
            *(bar.get("adjustment_identity") for bar in forward if isinstance(bar, Mapping)),
        }
        if None in adjustment_values or len(adjustment_values) != 1:
            raise ValueError("adjustment_identity_changed")
        execution_date = datetime.fromisoformat(str(execution.get("date"))).date()
        if execution_date <= cutoff.date():
            raise ValueError("execution_anchor_not_after_cutoff")

        start_price = self._positive(execution.get("open"), field="execution_open")
        benchmark_start = self._positive(
            execution.get("benchmark_open"), field="benchmark_execution_open"
        )
        horizon_results = {}
        for horizon in horizons:
            bars_required = int(horizon[:-1])
            if len(forward) < bars_required:
                raise ValueError(f"insufficient_forward_bars:{event_id}:{horizon}")
            window = list(forward[:bars_required])
            if any(not isinstance(bar, Mapping) for bar in window):
                raise ValueError("forward_bar_invalid")
            end_close = self._positive(window[-1].get("close"), field="end_close")
            benchmark_end = self._positive(
                window[-1].get("benchmark_close"), field="benchmark_end_close"
            )
            highs = [self._positive(bar.get("high"), field="high") for bar in window]
            lows = [self._positive(bar.get("low"), field="low") for bar in window]
            stock_return = (end_close / start_price - 1) * 100
            benchmark_return = (benchmark_end / benchmark_start - 1) * 100
            normalized = self._normalized_action_return(
                position_action=payload["position_action"],
                incremental_action=payload["incremental_action"],
                stock_return=stock_return,
            )
            horizon_results[horizon] = {
                "stock_return_pct": stock_return,
                "benchmark_return_pct": benchmark_return,
                "excess_return_pct": stock_return - benchmark_return,
                "max_favorable_excursion_pct": (max(highs) / start_price - 1) * 100,
                "max_adverse_excursion_pct": (min(lows) / start_price - 1) * 100,
                **normalized,
            }
        return {
            "event_id": event_id,
            "cutoff": point_in_time["cutoff"],
            "material_event_fingerprint": payload.get("material_event_fingerprint"),
            "market": instrument.get("market"),
            "symbol": instrument.get("symbol"),
            "product_type": instrument.get("product_type"),
            "position_action": payload["position_action"],
            "incremental_action": payload["incremental_action"],
            "portfolio_return_status": (
                "eligible" if payload.get("risk_budget_evaluated") is True else "unable"
            ),
            "portfolio_return_unable_reason": (
                None
                if payload.get("risk_budget_evaluated") is True
                else "risk_budget_not_evaluated"
            ),
            "horizons": horizon_results,
        }

    @staticmethod
    def _normalized_action_return(
        *,
        position_action: str,
        incremental_action: str,
        stock_return: float,
    ) -> dict[str, Any]:
        if position_action == "reduce" or incremental_action == "add_in_batches":
            return {
                "normalized_action_return_pct": None,
                "decision_value_vs_hold_pct": None,
                "decision_value_status": "unable",
                "unable_reason": "exposure_contract_missing",
            }
        normalized = stock_return if position_action == "hold" else 0.0
        return {
            "normalized_action_return_pct": normalized,
            "decision_value_vs_hold_pct": normalized - stock_return,
            "decision_value_status": "complete",
            "unable_reason": None,
        }

    @staticmethod
    def _timestamp(value: Any, *, field: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}_missing")
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field}_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field}_timezone_missing")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _positive(value: Any, *, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}_invalid") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{field}_invalid")
        return number
