# -*- coding: utf-8 -*-
"""Prospective same-input strategy shadow comparison sidecars."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.schemas.portfolio_decision_quality import INCREMENTAL_ACTIONS, POSITION_ACTIONS
from src.schemas.portfolio_strategy_validation import (
    freeze_strategy_manifest,
    strategy_manifest_hash,
)
from src.repositories.portfolio_strategy_validation_repo import (
    PortfolioStrategyValidationRepository,
)


class PortfolioShadowValidationService:
    def __init__(
        self,
        repo: PortfolioStrategyValidationRepository | None = None,
        db_manager: Any = None,
    ):
        self.repo = repo or PortfolioStrategyValidationRepository(db_manager)

    def record_comparison(
        self,
        *,
        event_id: str,
        frozen_input: Mapping[str, Any],
        champion_strategy: Mapping[str, Any],
        challenger_strategy: Mapping[str, Any],
        champion_decision: Mapping[str, Any],
        challenger_decision: Mapping[str, Any],
        protocol: Mapping[str, Any],
    ) -> dict[str, Any]:
        protocol_payload = self._validate_protocol(protocol)
        champion = freeze_strategy_manifest(champion_strategy)
        challenger = freeze_strategy_manifest(challenger_strategy)
        input_hash = strategy_manifest_hash(frozen_input)
        champion_output = self._validate_decision(
            champion_decision,
            expected_input_hash=input_hash,
            expected_strategy_hash=champion["manifest_hash"],
            expected_strategy_version=champion["strategy_version"],
        )
        challenger_output = self._validate_decision(
            challenger_decision,
            expected_input_hash=input_hash,
            expected_strategy_hash=challenger["manifest_hash"],
            expected_strategy_version=challenger["strategy_version"],
        )
        comparison = {
            "event_id": event_id,
            "protocol": protocol_payload,
            "frozen_input": dict(frozen_input),
            "input_hash": input_hash,
            "champion_decision": champion_output,
            "challenger_decision": challenger_output,
            "production_signal_written": False,
            "order_capability": False,
        }
        comparison_id = strategy_manifest_hash(
            {
                "protocol_id": protocol_payload["protocol_id"],
                "event_id": event_id,
                "input_hash": input_hash,
                "champion_strategy_hash": champion["manifest_hash"],
                "challenger_strategy_hash": challenger["manifest_hash"],
            }
        )
        row = self.repo.create_shadow_comparison(
            comparison_id=comparison_id,
            protocol_id=protocol_payload["protocol_id"],
            event_id=event_id,
            input_hash=input_hash,
            champion_strategy_hash=champion["manifest_hash"],
            challenger_strategy_hash=challenger["manifest_hash"],
            comparison=comparison,
        )
        return self._serialize(row)

    def weekly_review(self, *, protocol_id: str | None = None) -> dict[str, Any]:
        rows = self.repo.list_shadow_comparisons(protocol_id=protocol_id)
        resolved_protocol_id = protocol_id
        if resolved_protocol_id is None and rows:
            resolved_protocol_id = rows[-1].protocol_id
            rows = [row for row in rows if row.protocol_id == resolved_protocol_id]
        comparisons = [self._serialize(row) for row in rows]
        disagreements = [
            item
            for item in comparisons
            if (
                item["champion_decision"]["position_action"],
                item["champion_decision"]["incremental_action"],
            )
            != (
                item["challenger_decision"]["position_action"],
                item["challenger_decision"]["incremental_action"],
            )
        ]
        abstentions = [
            item
            for item in comparisons
            if item["champion_decision"]["decision_status"] == "abstain"
            or item["challenger_decision"]["decision_status"] == "abstain"
        ]
        hard_gate_failures = sorted(
            {
                str(blocker)
                for item in comparisons
                for decision in (item["champion_decision"], item["challenger_decision"])
                for blocker in decision.get("blockers", [])
            }
        )
        return {
            "protocol_id": resolved_protocol_id,
            "comparison_count": len(comparisons),
            "comparisons": comparisons,
            "paired_disagreements": disagreements,
            "abstentions": abstentions,
            "hard_gate_failures": hard_gate_failures,
            "rules_tuned": False,
            "automatic_promotion": False,
        }

    @staticmethod
    def assess_maturity(
        *,
        protocol: Mapping[str, Any],
        trading_days: int,
        independent_event_count: int,
        mature_20d_paired_count: int,
        hard_gate_violations: list[str],
        cost_delta_pct: float,
        drawdown_delta_pct: float,
        historical_oos_positive: bool,
        prospective_shadow_positive: bool,
    ) -> dict[str, Any]:
        validated = PortfolioShadowValidationService._validate_protocol(protocol)
        blockers = []
        if trading_days < int(validated["minimum_trading_days"]):
            blockers.append("minimum_trading_days_not_met")
        minimum_events = int(validated["minimum_independent_events"])
        if independent_event_count < minimum_events:
            blockers.append("minimum_independent_events_not_met")
        if mature_20d_paired_count < minimum_events:
            blockers.append("mature_20d_paired_outcomes_not_met")
        if hard_gate_violations:
            blockers.append("hard_gate_regression")
        if cost_delta_pct > float(validated["max_cost_delta_pct"]):
            blockers.append("cost_non_inferiority_failed")
        if drawdown_delta_pct > float(validated["max_drawdown_delta_pct"]):
            blockers.append("drawdown_non_inferiority_failed")
        if not historical_oos_positive:
            blockers.append("historical_oos_not_positive")
        if not prospective_shadow_positive:
            blockers.append("prospective_shadow_not_positive")
        return {
            "decision": "CONTINUE_SHADOW" if blockers else "ELIGIBLE_FOR_HUMAN_REVIEW",
            "blockers": blockers,
            "hard_gate_violations": list(hard_gate_violations),
            "automatic_promotion": False,
            "long_term_improvement_status": "PROVISIONAL",
        }

    @staticmethod
    def _validate_protocol(protocol: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "protocol_id",
            "frozen_at",
            "shadow_start",
            "minimum_trading_days",
            "minimum_independent_events",
            "max_cost_delta_pct",
            "max_drawdown_delta_pct",
        }
        if not isinstance(protocol, Mapping) or not required <= set(protocol):
            raise ValueError("shadow_protocol_incomplete")
        payload = dict(protocol)
        frozen_at = PortfolioShadowValidationService._timestamp(payload["frozen_at"])
        shadow_start = PortfolioShadowValidationService._timestamp(payload["shadow_start"])
        if frozen_at > shadow_start:
            raise ValueError("shadow_protocol_frozen_after_start")
        if int(payload["minimum_trading_days"]) < 20:
            raise ValueError("minimum_trading_days_below_20")
        return payload

    @staticmethod
    def _validate_decision(
        decision: Mapping[str, Any],
        *,
        expected_input_hash: str,
        expected_strategy_hash: str,
        expected_strategy_version: str,
    ) -> dict[str, Any]:
        if not isinstance(decision, Mapping):
            raise TypeError("shadow decision must be a mapping")
        forbidden = {
            key
            for key in decision
            if any(token in str(key).lower() for token in ("order", "broker", "execute", "trade_quantity"))
        }
        if forbidden:
            raise ValueError(f"order_field_forbidden:{','.join(sorted(forbidden))}")
        if decision.get("input_hash") != expected_input_hash:
            raise ValueError("shadow_input_hash_mismatch")
        if decision.get("strategy_manifest_hash") != expected_strategy_hash:
            raise ValueError("shadow_strategy_hash_mismatch")
        if decision.get("strategy_version") != expected_strategy_version:
            raise ValueError("shadow_strategy_version_mismatch")
        if decision.get("position_action") not in POSITION_ACTIONS:
            raise ValueError("position_action_missing")
        if decision.get("incremental_action") not in INCREMENTAL_ACTIONS:
            raise ValueError("incremental_action_missing")
        if decision.get("decision_status") not in {"eligible", "abstain"}:
            raise ValueError("decision_status_invalid")
        for field in ("blockers", "triggers", "invalidation", "confidence"):
            if field not in decision:
                raise ValueError(f"{field}_missing")
        return dict(decision)

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("shadow_protocol_timezone_missing")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        payload = json.loads(row.comparison_json)
        return {
            "comparison_id": row.comparison_id,
            "protocol_id": row.protocol_id,
            "event_id": row.event_id,
            "input_hash": row.input_hash,
            "champion_strategy_hash": row.champion_strategy_hash,
            "challenger_strategy_hash": row.challenger_strategy_hash,
            "champion_decision": payload["champion_decision"],
            "challenger_decision": payload["challenger_decision"],
            "production_signal_written": False,
            "order_capability": False,
        }
