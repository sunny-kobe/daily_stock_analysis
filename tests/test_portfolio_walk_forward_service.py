# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from src.services.portfolio_walk_forward_service import PortfolioWalkForwardService


def _events():
    return [
        {
            "event_id": f"event-{index}",
            "material_event_fingerprint": f"fingerprint-{index}",
            "cutoff_bar_index": index,
            "label_end_bar_index": index + 60,
        }
        for index in range(400)
    ]


def test_walk_forward_requires_frozen_60_bar_purge_and_embargo() -> None:
    service = PortfolioWalkForwardService()
    specs = [
        {
            "fold_id": "fold-1",
            "train_end": 99,
            "validation_start": 160,
            "validation_end": 179,
            "test_start": 240,
            "test_end": 259,
        },
        {
            "fold_id": "fold-2",
            "train_end": 139,
            "validation_start": 200,
            "validation_end": 219,
            "test_start": 280,
            "test_end": 299,
        },
    ]

    folds = service.build_folds(
        events=_events(),
        fold_specs=specs,
        purge_bars=60,
        embargo_bars=60,
    )

    assert [fold["fold_id"] for fold in folds] == ["fold-1", "fold-2"]
    assert len(folds[1]["train_event_ids"]) > len(folds[0]["train_event_ids"])
    assert all(event_id not in folds[0]["train_event_ids"] for event_id in folds[0]["test_event_ids"])

    with pytest.raises(ValueError, match="purge_boundary_violation"):
        service.build_folds(
            events=_events(),
            fold_specs=[dict(specs[0], validation_start=120)],
            purge_bars=60,
            embargo_bars=60,
        )


def test_duplicate_material_events_are_grouped_before_split() -> None:
    events = _events()[:300]
    duplicate = dict(
        events[10],
        event_id="duplicate-event",
        cutoff_bar_index=11,
        label_end_bar_index=71,
    )
    events.append(duplicate)

    folds = PortfolioWalkForwardService().build_folds(
        events=events,
        fold_specs=[
            {
                "fold_id": "fold-1",
                "train_end": 99,
                "validation_start": 160,
                "validation_end": 179,
                "test_start": 240,
                "test_end": 259,
            }
        ],
        purge_bars=60,
        embargo_bars=60,
    )

    assert folds[0]["duplicate_event_ids"] == ["duplicate-event"]
    assert "duplicate-event" not in folds[0]["train_event_ids"]


def _run(strategy, values, *, product="equity", instrument="AAPL"):
    return {
        "strategy_version": strategy,
        "events": [
            {
                "event_id": event_id,
                "market": "us",
                "symbol": instrument,
                "product_type": product,
                "position_action": "hold",
                "incremental_action": "wait",
                "regime": "trend",
                "horizons": {
                    "20d": {
                        "excess_return_pct": value,
                        "decision_value_vs_hold_pct": value,
                        "max_favorable_excursion_pct": value + 2,
                        "max_adverse_excursion_pct": -3,
                        "decision_value_status": "complete",
                    }
                },
            }
            for event_id, value in values.items()
        ],
    }


def test_paired_comparison_keeps_one_denominator_and_reports_missing_events() -> None:
    champion = _run("champion-v1", {"a": 1.0, "b": -1.0})
    challenger = _run("challenger-v1", {"a": 2.0})
    hold = _run("hold-baseline", {"a": 0.0, "b": 0.0})

    report = PortfolioWalkForwardService().compare_paired_runs(
        champion=champion,
        challenger=challenger,
        hold=hold,
        eligible_event_ids=["a", "b"],
    )

    assert report["eligible_event_ids"] == ["a", "b"]
    assert report["paired_event_ids"] == ["a"]
    assert report["unable_events"] == [
        {"event_id": "b", "reasons": ["challenger_event_missing"]}
    ]
    assert report["denominator_changed"] is False


def test_metrics_remain_segmented_by_product_and_action_axis() -> None:
    champion = _run("champion-v1", {"equity": 1.0})
    leveraged = _run(
        "challenger-v1",
        {"leveraged": 3.0},
        product="daily_leveraged_product",
        instrument="MAGX",
    )
    combined = {
        "strategy_version": "mixed",
        "events": champion["events"] + leveraged["events"],
    }

    segments = PortfolioWalkForwardService().segment_metrics(combined)

    assert {segment["product_type"] for segment in segments} == {
        "equity",
        "daily_leveraged_product",
    }
    assert all(segment["horizon"] == "20d" for segment in segments)
    assert all("confidence_interval" in segment for segment in segments)


def test_robustness_remains_observed_when_threshold_sensitive() -> None:
    report = PortfolioWalkForwardService().robustness_checks(
        baseline_value=1.0,
        cost_sensitivity=[0.8, 0.4],
        one_instrument_out={"AAPL": -0.2, "MSFT": 0.9},
        one_regime_out={"trend": -0.1, "sideways": 0.7},
        parameter_perturbation=[1.1, -0.3],
        alternate_eligible_dates=[0.6, 0.2],
    )

    assert report["status"] == "observed"
    assert "one_instrument_out_sign_flip" in report["warnings"]
    assert "parameter_perturbation_sign_flip" in report["warnings"]
