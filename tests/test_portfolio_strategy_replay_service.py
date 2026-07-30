# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import socket
from copy import deepcopy
from pathlib import Path

import pytest

from src.schemas.portfolio_strategy_validation import freeze_strategy_manifest
from src.services.portfolio_strategy_replay_service import PortfolioStrategyReplayService


def _strategy():
    return json.loads(Path("strategies/validation/champion-v1.json").read_text(encoding="utf-8"))


def _event(**overrides):
    strategy = freeze_strategy_manifest(_strategy())
    payload = {
        "event_id": "event-001",
        "cutoff": "2026-01-02T16:00:00Z",
        "evidence_artifacts": [
            {"artifact_id": "filing", "observed_at": "2026-01-02T15:00:00Z"},
        ],
        "frozen_holdings": {"account_id": 2, "quantity": 100.0, "cash": 5000.0},
        "position_action": "hold",
        "incremental_action": "wait",
        "strategy_identity": {
            "strategy_version": "champion-v1",
            "manifest_hash": strategy["manifest_hash"],
        },
        "instrument_identity": {
            "market": "us",
            "symbol": "AAPL",
            "source": "frozen",
            "frozen_at": "2026-01-02T15:00:00Z",
            "identity_hash": "a" * 64,
            "product_type": "equity",
        },
        "benchmark": {
            "market": "us",
            "symbol": "SPY",
            "selected_at": "2026-01-02T15:00:00Z",
            "mapping_version": "market-profile-v1",
        },
        "risk_budget_evaluated": False,
        "bars": {
            "adjustment_identity": "qfq",
            "observation": {"date": "2026-01-01", "close": 100.0, "adjustment_identity": "qfq"},
            "shadow_execution": {
                "date": "2026-01-03",
                "open": 101.0,
                "benchmark_open": 201.0,
                "adjustment_identity": "qfq",
            },
            "forward": [
                {
                    "date": f"2026-01-{day:02d}",
                    "close": close,
                    "high": close + 1,
                    "low": close - 1,
                    "benchmark_close": 200.0 + index,
                    "adjustment_identity": "qfq",
                }
                for index, (day, close) in enumerate(
                    [(3, 101.0), (4, 102.0), (5, 103.0), (6, 104.0), (7, 105.0)],
                    start=1,
                )
            ],
        },
    }
    payload.update(overrides)
    return payload


def _dataset(event=None):
    return {
        "schema_version": "portfolio-strategy-validation-dataset-v1",
        "horizons": ["5d"],
        "events": [event or _event()],
    }


def test_replay_rejects_evidence_after_cutoff() -> None:
    event = _event(
        evidence_artifacts=[
            {"artifact_id": "late", "observed_at": "2026-01-02T16:00:01Z"}
        ]
    )

    with pytest.raises(ValueError, match="evidence_after_cutoff"):
        PortfolioStrategyReplayService().replay(dataset=_dataset(event), strategy=_strategy())


def test_replay_rejects_current_registry_replacement() -> None:
    identity = dict(_event()["instrument_identity"], source="current_registry")

    with pytest.raises(ValueError, match="current_registry_forbidden"):
        PortfolioStrategyReplayService().replay(
            dataset=_dataset(_event(instrument_identity=identity)),
            strategy=_strategy(),
        )


def test_replay_rejects_hindsight_benchmark_selection() -> None:
    benchmark = dict(_event()["benchmark"], selected_at="2026-01-08T00:00:00Z")

    with pytest.raises(ValueError, match="benchmark_selected_after_cutoff"):
        PortfolioStrategyReplayService().replay(
            dataset=_dataset(_event(benchmark=benchmark)),
            strategy=_strategy(),
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("frozen_holdings", None, "frozen_holdings_missing"),
        ("position_action", None, "position_action_missing"),
        ("incremental_action", None, "incremental_action_missing"),
        ("strategy_identity", None, "strategy_identity_missing"),
    ],
)
def test_replay_requires_complete_frozen_decision_contract(field, value, error) -> None:
    with pytest.raises(ValueError, match=error):
        PortfolioStrategyReplayService().replay(
            dataset=_dataset(_event(**{field: value})),
            strategy=_strategy(),
        )


def test_replay_rejects_insufficient_bars_and_adjustment_drift() -> None:
    insufficient = deepcopy(_event())
    insufficient["bars"]["forward"] = insufficient["bars"]["forward"][:4]
    with pytest.raises(ValueError, match="insufficient_forward_bars"):
        PortfolioStrategyReplayService().replay(
            dataset=_dataset(insufficient), strategy=_strategy()
        )

    drift = deepcopy(_event())
    drift["bars"]["forward"][-1]["adjustment_identity"] = "raw"
    with pytest.raises(ValueError, match="adjustment_identity_changed"):
        PortfolioStrategyReplayService().replay(dataset=_dataset(drift), strategy=_strategy())


def test_replay_is_deterministic_and_performs_zero_network_calls(monkeypatch) -> None:
    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    service = PortfolioStrategyReplayService()

    first = service.replay(dataset=_dataset(), strategy=_strategy())
    second = service.replay(dataset=_dataset(), strategy=_strategy())

    assert first == second
    assert first["status"] == "complete"
    assert first["events"][0]["horizons"]["5d"]["stock_return_pct"] == pytest.approx(
        (105.0 / 101.0 - 1) * 100
    )
    assert first["events"][0]["portfolio_return_status"] == "unable"
