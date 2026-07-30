# -*- coding: utf-8 -*-
"""Purged walk-forward and paired OOS evaluation for frozen replay runs."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from statistics import mean, stdev
from typing import Any


class PortfolioWalkForwardService:
    def build_folds(
        self,
        *,
        events: Sequence[Mapping[str, Any]],
        fold_specs: Sequence[Mapping[str, Any]],
        purge_bars: int,
        embargo_bars: int,
    ) -> list[dict[str, Any]]:
        if purge_bars < 60 or embargo_bars < 60:
            raise ValueError("purge_embargo_below_60_bars")
        deduplicated, duplicate_ids = self._deduplicate_events(events)
        folds = []
        previous_train_end = None
        for spec in fold_specs:
            train_end = int(spec["train_end"])
            validation_start = int(spec["validation_start"])
            validation_end = int(spec["validation_end"])
            test_start = int(spec["test_start"])
            test_end = int(spec["test_end"])
            if validation_start - train_end < purge_bars:
                raise ValueError("purge_boundary_violation")
            if test_start - validation_end < embargo_bars:
                raise ValueError("embargo_boundary_violation")
            if previous_train_end is not None and train_end < previous_train_end:
                raise ValueError("training_window_not_expanding")
            previous_train_end = train_end

            train = [
                event
                for event in deduplicated
                if int(event["cutoff_bar_index"]) <= train_end
                and int(event["label_end_bar_index"]) < validation_start
            ]
            validation = [
                event
                for event in deduplicated
                if validation_start <= int(event["cutoff_bar_index"]) <= validation_end
            ]
            test = [
                event
                for event in deduplicated
                if test_start <= int(event["cutoff_bar_index"]) <= test_end
            ]
            folds.append(
                {
                    "fold_id": spec["fold_id"],
                    "train_event_ids": [event["event_id"] for event in train],
                    "validation_event_ids": [event["event_id"] for event in validation],
                    "test_event_ids": [event["event_id"] for event in test],
                    "duplicate_event_ids": duplicate_ids,
                    "purge_bars": purge_bars,
                    "embargo_bars": embargo_bars,
                }
            )
        return folds

    def compare_paired_runs(
        self,
        *,
        champion: Mapping[str, Any],
        challenger: Mapping[str, Any],
        hold: Mapping[str, Any],
        eligible_event_ids: Sequence[str],
    ) -> dict[str, Any]:
        indexes = {
            "champion": self._event_index(champion),
            "challenger": self._event_index(challenger),
            "hold": self._event_index(hold),
        }
        eligible = list(dict.fromkeys(str(event_id) for event_id in eligible_event_ids))
        paired = []
        unable = []
        for event_id in eligible:
            reasons = [
                f"{name}_event_missing"
                for name, index in indexes.items()
                if event_id not in index
            ]
            if reasons:
                unable.append({"event_id": event_id, "reasons": reasons})
            else:
                paired.append(event_id)
        paired_rows = []
        for event_id in paired:
            paired_rows.append(
                {
                    "event_id": event_id,
                    "champion": indexes["champion"][event_id],
                    "challenger": indexes["challenger"][event_id],
                    "hold": indexes["hold"][event_id],
                }
            )
        return {
            "eligible_event_ids": eligible,
            "paired_event_ids": paired,
            "unable_events": unable,
            "denominator_changed": False,
            "eligible_event_count": len(eligible),
            "paired_event_count": len(paired),
            "paired_rows": paired_rows,
        }

    def segment_metrics(self, run: Mapping[str, Any]) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str, str, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
        for event in run.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            for horizon, metrics in (event.get("horizons") or {}).items():
                if not isinstance(metrics, Mapping):
                    continue
                key = (
                    str(horizon),
                    str(event.get("market") or "unknown"),
                    str(event.get("product_type") or "unknown"),
                    str(event.get("position_action") or "unknown"),
                    str(event.get("incremental_action") or "unknown"),
                )
                groups.setdefault(key, []).append((event, metrics))

        segments = []
        for key, rows in sorted(groups.items()):
            complete_values = [
                float(metrics["decision_value_vs_hold_pct"])
                for _event, metrics in rows
                if metrics.get("decision_value_status") == "complete"
                and metrics.get("decision_value_vs_hold_pct") is not None
            ]
            excess = self._numbers(metrics.get("excess_return_pct") for _event, metrics in rows)
            mfe = self._numbers(
                metrics.get("max_favorable_excursion_pct") for _event, metrics in rows
            )
            mae = self._numbers(
                metrics.get("max_adverse_excursion_pct") for _event, metrics in rows
            )
            instruments = Counter(str(event.get("symbol") or "unknown") for event, _ in rows)
            regimes = Counter(str(event.get("regime") or "unknown") for event, _ in rows)
            segments.append(
                {
                    "horizon": key[0],
                    "market": key[1],
                    "product_type": key[2],
                    "position_action": key[3],
                    "incremental_action": key[4],
                    "eligible_events": len(rows),
                    "effective_sample_size": len(
                        {
                            event.get("material_event_fingerprint") or event.get("event_id")
                            for event, _metrics in rows
                        }
                    ),
                    "unable_rate": (len(rows) - len(complete_values)) / len(rows),
                    "excess_return": mean(excess) if excess else None,
                    "decision_value_vs_hold": mean(complete_values) if complete_values else None,
                    "mfe": mean(mfe) if mfe else None,
                    "mae": mean(mae) if mae else None,
                    "drawdown": min(mae) if mae else None,
                    "turnover": None,
                    "costs": None,
                    "opportunity_cost": None,
                    "human_override_value": None,
                    "instrument_concentration": self._concentration(instruments, len(rows)),
                    "regime_concentration": self._concentration(regimes, len(rows)),
                    "confidence_interval": self._confidence_interval(complete_values),
                }
            )
        return segments

    @staticmethod
    def robustness_checks(
        *,
        baseline_value: float,
        cost_sensitivity: Sequence[float],
        one_instrument_out: Mapping[str, float],
        one_regime_out: Mapping[str, float],
        parameter_perturbation: Sequence[float],
        alternate_eligible_dates: Sequence[float],
    ) -> dict[str, Any]:
        warnings = []

        def sign_flip(values: Sequence[float]) -> bool:
            return any(float(value) * baseline_value < 0 for value in values)

        if sign_flip(list(one_instrument_out.values())):
            warnings.append("one_instrument_out_sign_flip")
        if sign_flip(list(one_regime_out.values())):
            warnings.append("one_regime_out_sign_flip")
        if sign_flip(parameter_perturbation):
            warnings.append("parameter_perturbation_sign_flip")
        if sign_flip(cost_sensitivity):
            warnings.append("cost_sensitivity_sign_flip")
        if sign_flip(alternate_eligible_dates):
            warnings.append("alternate_eligible_dates_sign_flip")
        return {
            "status": "observed" if warnings else "robustness_checks_passed",
            "warnings": warnings,
            "baseline_value": baseline_value,
            "cost_sensitivity": list(cost_sensitivity),
            "one_instrument_out": dict(one_instrument_out),
            "one_regime_out": dict(one_regime_out),
            "parameter_perturbation": list(parameter_perturbation),
            "alternate_eligible_dates": list(alternate_eligible_dates),
        }

    @staticmethod
    def _deduplicate_events(
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], list[str]]:
        ordered = sorted(events, key=lambda event: int(event["cutoff_bar_index"]))
        seen = set()
        kept = []
        duplicates = []
        for event in ordered:
            fingerprint = event.get("material_event_fingerprint")
            if not fingerprint:
                raise ValueError("material_event_fingerprint_missing")
            if fingerprint in seen:
                duplicates.append(str(event["event_id"]))
                continue
            seen.add(fingerprint)
            kept.append(event)
        return kept, duplicates

    @staticmethod
    def _event_index(run: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {
            str(event["event_id"]): event
            for event in run.get("events") or []
            if isinstance(event, Mapping) and event.get("event_id")
        }

    @staticmethod
    def _numbers(values: Any) -> list[float]:
        result = []
        for value in values:
            if value is None:
                continue
            number = float(value)
            if math.isfinite(number):
                result.append(number)
        return result

    @staticmethod
    def _concentration(counts: Counter, total: int) -> list[dict[str, Any]]:
        return [
            {"key": key, "count": count, "pct": count / total * 100}
            for key, count in counts.most_common()
        ]

    @staticmethod
    def _confidence_interval(values: Sequence[float]) -> dict[str, float] | None:
        if len(values) < 2:
            return None
        center = mean(values)
        margin = 1.96 * stdev(values) / math.sqrt(len(values))
        return {"lower": center - margin, "upper": center + margin, "method": "normal_95"}
