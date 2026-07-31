# -*- coding: utf-8 -*-
"""Freeze immutable recommendation-time context for decision-quality review."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.repositories.stock_repo import StockRepository
from src.repositories.decision_signal_outcome_repo import DecisionSignalOutcomeRepository
from src.schemas.portfolio_decision_quality import (
    is_materially_evaluable,
    material_event_fingerprint,
    normalize_portfolio_decision,
    QUALITY_HORIZONS,
    ATTRIBUTION_CATEGORIES,
    ATTRIBUTION_STATUSES,
)
from src.schemas.portfolio_instruction import project_holding_instruction
from src.utils.sanitize import sanitize_decision_signal_payload, sanitize_decision_signal_text


DECISION_QUALITY_ENGINE_VERSION = "decision-quality-v1"
_NON_PERSISTABLE_CONTEXT_BLOCKERS = frozenset(
    {
        "account_id_missing",
        "instrument_identity_missing",
        "frozen_snapshot_hash_missing",
        "frozen_snapshot_hash_invalid",
        "position_action_missing",
        "incremental_action_missing",
        "evidence_cutoff_missing",
    }
)


class DecisionQualityService:
    """Persist one immutable sidecar for each materially distinct decision."""

    def __init__(
        self,
        repo: DecisionQualityRepository | None = None,
        stock_repo: StockRepository | None = None,
        db_manager: Any = None,
        feedback_repo: DecisionSignalOutcomeRepository | None = None,
    ):
        self.repo = repo or DecisionQualityRepository(db_manager)
        self.stock_repo = stock_repo or StockRepository(db_manager)
        self.feedback_repo = feedback_repo or DecisionSignalOutcomeRepository(db_manager)

    def freeze_context(
        self,
        *,
        signal: Mapping[str, Any],
        portfolio_decision: Mapping[str, Any],
        frozen_snapshot: Mapping[str, Any],
        portfolio_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision = dict(portfolio_decision)
        context = dict(portfolio_context) if isinstance(portfolio_context, Mapping) else {}
        snapshot = dict(frozen_snapshot)
        benchmark = decision.get("benchmark")
        benchmark = dict(benchmark) if isinstance(benchmark, Mapping) else None

        material = normalize_portfolio_decision(
            {
                **decision,
                "account_id": self._account_id(context, snapshot),
                "market": signal.get("market"),
                "stock_code": signal.get("stock_code"),
                "instrument_type": self._instrument_type(snapshot, signal),
                "frozen_snapshot_hash": snapshot.get("snapshot_hash"),
                "evidence_cutoff": snapshot.get("cutoff"),
                "evidence_version": snapshot.get("schema_version"),
                "decision_profile": signal.get("decision_profile"),
            }
        )
        unable_reasons = is_materially_evaluable(material)
        status = "complete" if not unable_reasons else "insufficient_evidence"
        fingerprint = material_event_fingerprint(material)
        if any(reason in _NON_PERSISTABLE_CONTEXT_BLOCKERS for reason in unable_reasons):
            return {
                "context_id": None,
                "signal_id": int(signal["id"]),
                "material_event_fingerprint": fingerprint,
                "created": False,
                "status": status,
                "unable_reasons": unable_reasons,
            }
        row, created = self.repo.create_context_if_absent(
            {
                "signal_id": int(signal["id"]),
                "account_id": material["account_id"],
                "market": material["market"],
                "stock_code": material["stock_code"],
                "instrument_type": material["instrument_type"],
                "frozen_snapshot_hash": material["frozen_snapshot_hash"],
                "material_event_fingerprint": fingerprint,
                "position_action": material["position_action"],
                "incremental_action": material["incremental_action"],
                "confidence_by_horizon_json": json.dumps(
                    material["confidence_by_horizon"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "benchmark_market": benchmark.get("market") if benchmark else None,
                "benchmark_code": benchmark.get("code") if benchmark else None,
                "benchmark_type": benchmark.get("type") if benchmark else None,
                "benchmark_evidence_url": benchmark.get("evidence_url") if benchmark else None,
                "benchmark_evidence_as_of": self._datetime(
                    benchmark.get("evidence_as_of") if benchmark else None
                ),
                "decision_cutoff": self._required_datetime(material["evidence_cutoff"]),
                "context_status": status,
                "unable_reasons_json": json.dumps(unable_reasons, separators=(",", ":")),
            }
        )
        return {
            "context_id": row.id,
            "signal_id": row.signal_id,
            "material_event_fingerprint": row.material_event_fingerprint,
            "created": created,
            "status": row.context_status,
            "unable_reasons": json.loads(row.unable_reasons_json or "[]"),
        }

    def evaluate_outcome(self, *, signal_id: int, horizon: str) -> dict[str, Any]:
        if horizon not in QUALITY_HORIZONS:
            raise ValueError(f"unsupported quality horizon: {horizon}")
        context = self.repo.get_context_by_signal(signal_id=signal_id)
        if context is None:
            return self._empty_outcome(
                signal_id=signal_id,
                horizon=horizon,
                unable_reason="missing_context",
            )

        base = self._outcome_base(context, horizon)
        if not context.instrument_type:
            return self._persist_outcome(
                {**base, "eval_status": "unable", "unable_reason": "instrument_type_missing"}
            )
        if not context.benchmark_market or not context.benchmark_code or not context.benchmark_type:
            return self._persist_outcome(
                {**base, "eval_status": "unable", "unable_reason": "missing_benchmark_identity"}
            )

        paired = self.stock_repo.get_exact_paired_forward_bars(
            stock_code=context.stock_code,
            benchmark_code=context.benchmark_code,
            anchor_date=context.decision_cutoff.date(),
            eval_window_days=QUALITY_HORIZONS[horizon],
        )
        if paired.unable_reason:
            return self._persist_outcome(
                {**base, "eval_status": "unable", "unable_reason": paired.unable_reason}
            )

        start_price = self._positive(getattr(paired.stock_anchor, "open", None))
        benchmark_start = self._positive(getattr(paired.benchmark_anchor, "open", None))
        end_close = self._positive(getattr(paired.stock_bars[-1], "close", None))
        benchmark_end = self._positive(getattr(paired.benchmark_bars[-1], "close", None))
        highs = [self._positive(getattr(bar, "high", None)) for bar in paired.stock_bars]
        lows = [self._positive(getattr(bar, "low", None)) for bar in paired.stock_bars]
        if start_price is None:
            return self._persist_outcome(
                {**base, "eval_status": "unable", "unable_reason": "missing_anchor_price"}
            )
        if benchmark_start is None:
            return self._persist_outcome(
                {**base, "eval_status": "unable", "unable_reason": "missing_benchmark_anchor"}
            )
        if end_close is None or benchmark_end is None or any(value is None for value in [*highs, *lows]):
            return self._persist_outcome(
                {**base, "eval_status": "unable", "unable_reason": "insufficient_forward_bars"}
            )

        stock_return = (end_close / start_price - 1) * 100
        benchmark_return = (benchmark_end / benchmark_start - 1) * 100
        max_high = max(value for value in highs if value is not None)
        min_low = min(value for value in lows if value is not None)
        metrics = {
            "eval_status": "complete",
            "unable_reason": None,
            "anchor_date": paired.stock_anchor.date,
            "start_price": start_price,
            "end_close": end_close,
            "max_high": max_high,
            "min_low": min_low,
            "stock_return_pct": stock_return,
            "benchmark_start_price": benchmark_start,
            "benchmark_end_close": benchmark_end,
            "benchmark_return_pct": benchmark_return,
            "excess_return_pct": stock_return - benchmark_return,
            "max_favorable_excursion_pct": (max_high / start_price - 1) * 100,
            "max_adverse_excursion_pct": (min_low / start_price - 1) * 100,
            "data_quality_level": paired.adjustment_marker,
        }
        normalized = self._normalized_action_metrics(
            position_action=context.position_action,
            incremental_action=context.incremental_action,
            stock_return=stock_return,
        )
        return self._persist_outcome({**base, **metrics, **normalized})

    def run_outcomes(
        self,
        *,
        signal_id: int | None = None,
        horizons: list[str] | None = None,
    ) -> dict[str, Any]:
        selected = horizons or list(QUALITY_HORIZONS)
        if any(horizon not in QUALITY_HORIZONS for horizon in selected):
            raise ValueError("unsupported quality horizon")
        contexts = (
            [self.repo.get_context_by_signal(signal_id=signal_id)]
            if signal_id is not None
            else self.repo.list_contexts_for_weekly_review(limit=1000)
        )
        contexts = [context for context in contexts if context is not None]
        if signal_id is not None and not contexts:
            raise ValueError(f"decision quality context not found: {signal_id}")
        items = [
            self.evaluate_outcome(signal_id=context.signal_id, horizon=horizon)
            for context in contexts
            for horizon in selected
        ]
        return {"items": items, "evaluated": len(items), "engine_version": DECISION_QUALITY_ENGINE_VERSION}

    def get_quality(self, *, signal_id: int) -> dict[str, Any]:
        context = self.repo.get_context_by_signal(signal_id=signal_id)
        if context is None:
            raise ValueError(f"decision quality context not found: {signal_id}")
        outcomes = {
            row.horizon: self._serialize_outcome(row)
            for row in self.repo.list_quality_outcomes(
                signal_id=signal_id,
                engine_version=DECISION_QUALITY_ENGINE_VERSION,
            )
        }
        horizon_items = []
        for horizon in QUALITY_HORIZONS:
            item = outcomes.get(horizon)
            if item is None:
                item = {
                    "signal_id": signal_id,
                    "horizon": horizon,
                    "engine_version": DECISION_QUALITY_ENGINE_VERSION,
                    "eval_status": "pending",
                    "unable_reason": "horizon_not_mature",
                }
            item["maturity"] = "mature" if item["eval_status"] == "complete" else item["eval_status"]
            item["unable_reasons"] = [item["unable_reason"]] if item.get("unable_reason") else []
            horizon_items.append(item)
        return {
            "context": self._serialize_context(context),
            "outcomes": horizon_items,
            "attributions": [
                self._serialize_attribution(row)
                for row in self.repo.list_attributions(signal_id=signal_id)
            ],
        }

    def get_stats(self, *, horizon: str) -> dict[str, Any]:
        if horizon not in QUALITY_HORIZONS:
            raise ValueError(f"unsupported quality horizon: {horizon}")
        rows = self.repo.list_quality_outcomes(
            horizon=horizon,
            engine_version=DECISION_QUALITY_ENGINE_VERSION,
            eval_status="complete",
        )
        sample_size = len(rows)
        if sample_size == 0:
            return {
                "sample_size": 0,
                "horizon": horizon,
                "empty_state": True,
                "performance": None,
                "instrument_concentration": [],
                "engine_version": DECISION_QUALITY_ENGINE_VERSION,
            }
        contexts = {
            context.signal_id: context
            for context in self.repo.list_contexts_for_weekly_review(limit=1000)
        }
        counts = Counter(
            (contexts[row.signal_id].market, contexts[row.signal_id].stock_code)
            for row in rows
            if row.signal_id in contexts
        )
        concentration = [
            {
                "market": market,
                "stock_code": stock_code,
                "count": count,
                "pct": count / sample_size * 100,
            }
            for (market, stock_code), count in counts.most_common()
        ]
        excess = [row.excess_return_pct for row in rows if row.excess_return_pct is not None]
        values = [
            row.decision_value_vs_hold_pct
            for row in rows
            if row.decision_value_vs_hold_pct is not None
        ]
        return {
            "sample_size": sample_size,
            "horizon": horizon,
            "empty_state": False,
            "performance": {
                "status": "PROVISIONAL",
                "avg_excess_return_pct": sum(excess) / len(excess) if excess else None,
                "avg_decision_value_vs_hold_pct": sum(values) / len(values) if values else None,
            },
            "instrument_concentration": concentration,
            "engine_version": DECISION_QUALITY_ENGINE_VERSION,
        }

    def put_attribution(
        self,
        *,
        signal_id: int,
        horizon: str,
        category: str,
        status: str,
        summary: str,
        evidence: list[Any] | None = None,
        counterexamples: list[Any] | None = None,
        user_note: str | None = None,
    ) -> dict[str, Any]:
        if self.repo.get_context_by_signal(signal_id=signal_id) is None:
            raise ValueError(f"decision quality context not found: {signal_id}")
        if horizon not in QUALITY_HORIZONS:
            raise ValueError("unsupported quality horizon")
        if category not in ATTRIBUTION_CATEGORIES or status not in ATTRIBUTION_STATUSES:
            raise ValueError("invalid attribution category or status")
        row, _ = self.repo.upsert_attribution(
            {
                "signal_id": signal_id,
                "horizon": horizon,
                "engine_version": DECISION_QUALITY_ENGINE_VERSION,
                "category": category,
                "status": status,
                "summary": sanitize_decision_signal_text(summary),
                "evidence_json": json.dumps(sanitize_decision_signal_payload(evidence or []), ensure_ascii=False),
                "counterexamples_json": json.dumps(
                    sanitize_decision_signal_payload(counterexamples or []), ensure_ascii=False
                ),
                "user_note": sanitize_decision_signal_text(user_note) if user_note else None,
            }
        )
        return self._serialize_attribution(row)

    def weekly_review(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        window_end = until or datetime.now()
        window_start = since or (window_end - timedelta(days=7))
        if window_start >= window_end:
            raise ValueError("weekly review since must be before until")
        contexts = self.repo.list_contexts_for_weekly_review(
            since=window_start,
            until=window_end,
            limit=1000,
        )
        context_signal_ids = {context.signal_id for context in contexts}
        disagreements = []
        for context in contexts:
            feedback = self.feedback_repo.get_feedback(signal_id=context.signal_id)
            if feedback is None:
                continue
            if (
                feedback.human_position_action
                and feedback.human_position_action != context.position_action
            ) or (
                feedback.human_incremental_action
                and feedback.human_incremental_action != context.incremental_action
            ):
                disagreements.append(
                    {
                        "signal_id": context.signal_id,
                        "ai_position_action": context.position_action,
                        "ai_incremental_action": context.incremental_action,
                        "human_position_action": feedback.human_position_action,
                        "human_incremental_action": feedback.human_incremental_action,
                    }
                )
        confirmed = [
            row
            for row in self.repo.list_confirmed_attributions(limit=5000)
            if row.signal_id in context_signal_ids
        ]
        return {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "material_decision_count": len(contexts),
            "decisions": [self.get_quality(signal_id=context.signal_id) for context in contexts],
            "ai_human_disagreements": disagreements,
            "confirmed_attribution_counts": dict(Counter(row.category for row in confirmed)),
            "triggered_conditions": [],
            "expired_conditions": [],
            "candidate_patterns": self.get_learning_patterns(
                since=window_start,
                until=window_end,
            ),
            "automatic_rules_activated": False,
        }

    def get_learning_patterns(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        confirmed = self.repo.list_confirmed_attributions(limit=5000)
        contexts = {
            row.signal_id: row
            for row in self.repo.list_contexts_for_weekly_review(
                since=since,
                until=until,
                limit=1000,
            )
        }
        outcomes = {
            (row.signal_id, row.horizon, row.engine_version): row
            for row in self.repo.list_quality_outcomes(eval_status="complete")
        }
        grouped: dict[tuple[str, str, str], list[tuple[Any, Any, Any]]] = {}
        for attribution in confirmed:
            outcome = outcomes.get(
                (attribution.signal_id, attribution.horizon, attribution.engine_version)
            )
            context = contexts.get(attribution.signal_id)
            if outcome is None or context is None:
                continue
            key = (
                attribution.category,
                attribution.horizon,
                outcome.instrument_type or "unknown",
            )
            grouped.setdefault(key, []).append((attribution, outcome, context))

        patterns = []
        for (category, horizon, instrument_type), rows in sorted(grouped.items()):
            instrument_counts = Counter(
                (context.market, context.stock_code) for _attribution, _outcome, context in rows
            )
            counterexamples = []
            for attribution, _outcome, _context in rows:
                try:
                    values = json.loads(attribution.counterexamples_json or "[]")
                except (TypeError, ValueError):
                    values = []
                if isinstance(values, list):
                    counterexamples.extend(values)
            dominant_count = max(instrument_counts.values()) if instrument_counts else 0
            patterns.append(
                {
                    "category": category,
                    "horizon": horizon,
                    "instrument_type": instrument_type,
                    "status": "observed",
                    "eligible_sample_count": len(rows),
                    "counterexamples": counterexamples,
                    "instrument_counts": [
                        {"market": market, "stock_code": code, "count": count}
                        for (market, code), count in instrument_counts.most_common()
                    ],
                    "instrument_concentration_warning": (
                        dominant_count / len(rows) > 0.5 if rows else False
                    ),
                    "correlated_repeated_event_warning": (
                        dominant_count > 1
                    ),
                    "automatic_activation": False,
                }
            )
        return patterns

    def _outcome_base(self, context: Any, horizon: str) -> dict[str, Any]:
        return {
            "signal_id": context.signal_id,
            "horizon": horizon,
            "engine_version": DECISION_QUALITY_ENGINE_VERSION,
            "eval_status": "unable",
            "unable_reason": None,
            "anchor_date": context.decision_cutoff.date(),
            "eval_window_days": QUALITY_HORIZONS[horizon],
            "start_price": None,
            "end_close": None,
            "max_high": None,
            "min_low": None,
            "stock_return_pct": None,
            "benchmark_start_price": None,
            "benchmark_end_close": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
            "normalized_action_return_pct": None,
            "decision_value_vs_hold_pct": None,
            "hindsight_regret_pct": None,
            "decision_value_status": "unable",
            "position_action": context.position_action,
            "incremental_action": context.incremental_action,
            "market": context.market,
            "instrument_type": context.instrument_type,
            "data_quality_level": None,
        }

    @staticmethod
    def _empty_outcome(*, signal_id: int, horizon: str, unable_reason: str) -> dict[str, Any]:
        return {
            "signal_id": signal_id,
            "horizon": horizon,
            "engine_version": DECISION_QUALITY_ENGINE_VERSION,
            "eval_status": "unable",
            "unable_reason": unable_reason,
            "decision_value_status": "unable",
        }

    def _persist_outcome(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        row, _created = self.repo.upsert_quality_outcome(fields)
        return self._serialize_outcome(row)

    @staticmethod
    def _serialize_outcome(row: Any) -> dict[str, Any]:
        return {
            field: getattr(row, field)
            for field in (
                "signal_id",
                "horizon",
                "engine_version",
                "eval_status",
                "unable_reason",
                "anchor_date",
                "eval_window_days",
                "start_price",
                "end_close",
                "max_high",
                "min_low",
                "stock_return_pct",
                "benchmark_start_price",
                "benchmark_end_close",
                "benchmark_return_pct",
                "excess_return_pct",
                "max_favorable_excursion_pct",
                "max_adverse_excursion_pct",
                "normalized_action_return_pct",
                "decision_value_vs_hold_pct",
                "hindsight_regret_pct",
                "decision_value_status",
                "position_action",
                "incremental_action",
                "market",
                "instrument_type",
                "data_quality_level",
            )
        }

    @staticmethod
    def _serialize_context(row: Any) -> dict[str, Any]:
        unable_reasons = json.loads(row.unable_reasons_json or "[]")
        try:
            user_instruction = project_holding_instruction(
                position_action=row.position_action,
                incremental_action=row.incremental_action,
                blocked=row.context_status != "complete" or bool(unable_reasons),
            )
        except ValueError:
            user_instruction = "insufficient"
        return {
            "id": row.id,
            "signal_id": row.signal_id,
            "account_id": row.account_id,
            "market": row.market,
            "stock_code": row.stock_code,
            "instrument_type": row.instrument_type,
            "frozen_snapshot_hash": row.frozen_snapshot_hash,
            "material_event_fingerprint": row.material_event_fingerprint,
            "position_action": row.position_action,
            "incremental_action": row.incremental_action,
            "user_instruction": user_instruction,
            "confidence_by_horizon": json.loads(row.confidence_by_horizon_json or "{}"),
            "benchmark": {
                "market": row.benchmark_market,
                "code": row.benchmark_code,
                "type": row.benchmark_type,
            },
            "decision_cutoff": row.decision_cutoff.isoformat(),
            "context_status": row.context_status,
            "unable_reasons": unable_reasons,
        }

    @staticmethod
    def _serialize_attribution(row: Any) -> dict[str, Any]:
        return {
            "signal_id": row.signal_id,
            "horizon": row.horizon,
            "engine_version": row.engine_version,
            "category": row.category,
            "status": row.status,
            "summary": row.summary,
            "evidence": json.loads(row.evidence_json or "[]"),
            "counterexamples": json.loads(row.counterexamples_json or "[]"),
            "user_note": row.user_note,
        }

    @staticmethod
    def _normalized_action_metrics(
        *,
        position_action: str,
        incremental_action: str,
        stock_return: float,
    ) -> dict[str, Any]:
        if position_action == "reduce" or incremental_action == "add_in_batches":
            return {
                "unable_reason": "exposure_contract_missing",
                "normalized_action_return_pct": None,
                "decision_value_vs_hold_pct": None,
                "hindsight_regret_pct": None,
                "decision_value_status": "unable",
            }
        normalized = stock_return if position_action == "hold" else 0.0
        return {
            "normalized_action_return_pct": normalized,
            "decision_value_vs_hold_pct": normalized - stock_return,
            "hindsight_regret_pct": max(stock_return, 0.0) - normalized,
            "decision_value_status": "complete",
        }

    @staticmethod
    def _positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _account_id(context: Mapping[str, Any], snapshot: Mapping[str, Any]) -> Any:
        if context.get("account_id") is not None:
            return context.get("account_id")
        account_ids = {
            item.get("account_id")
            for item in snapshot.get("positions") or []
            if isinstance(item, Mapping) and item.get("account_id") is not None
        }
        return next(iter(account_ids)) if len(account_ids) == 1 else None

    @staticmethod
    def _instrument_type(snapshot: Mapping[str, Any], signal: Mapping[str, Any]) -> str | None:
        target_symbol = canonical_stock_code(
            normalize_stock_code(str(signal.get("stock_code") or ""))
        )
        target_market = str(signal.get("market") or "").strip().lower()
        for item in snapshot.get("instruments") or []:
            if not isinstance(item, Mapping):
                continue
            symbol = canonical_stock_code(
                normalize_stock_code(str(item.get("symbol") or ""))
            )
            market = str(item.get("market") or "").strip().lower()
            if symbol == target_symbol and market == target_market:
                instrument_type = str(item.get("instrument_type") or "").strip().lower()
                return instrument_type or None
        return None

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @classmethod
    def _required_datetime(cls, value: Any) -> datetime:
        parsed = cls._datetime(value)
        if parsed is None:
            raise ValueError("frozen snapshot cutoff is required")
        return parsed
