# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from src.config import Config
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.services.decision_quality_service import (
    DECISION_QUALITY_ENGINE_VERSION,
    DecisionQualityService,
)
from src.storage import DatabaseManager, StockDaily


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'quality_outcomes.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _context(db, *, signal_id=101, position_action="hold", incremental_action="wait", **overrides):
    fields = {
        "signal_id": signal_id,
        "account_id": 2,
        "market": "us",
        "stock_code": "AAPL",
        "instrument_type": "equity",
        "frozen_snapshot_hash": "a" * 64,
        "material_event_fingerprint": f"{signal_id:064x}",
        "position_action": position_action,
        "incremental_action": incremental_action,
        "confidence_by_horizon_json": json.dumps({"5d": 0.5, "20d": 0.6, "60d": 0.55}),
        "benchmark_market": "us",
        "benchmark_code": "SPY",
        "benchmark_type": "market_index",
        "benchmark_evidence_url": None,
        "benchmark_evidence_as_of": None,
        "decision_cutoff": datetime(2026, 1, 2, 21, 0),
        "decision_market_phase": "postmarket",
        "strategy_version": "champion-v1",
        "context_status": "complete",
        "unable_reasons_json": "[]",
    }
    fields.update(overrides)
    return DecisionQualityRepository(db).create_context_if_absent(fields)[0]


def _seed_series(db, *, code, start, closes, source="fixture:qfq"):
    with db.session_scope() as session:
        for offset, close in enumerate(closes):
            session.add(
                StockDaily(
                    code=code,
                    date=start + timedelta(days=offset),
                    open=close,
                    high=close + 2,
                    low=close - 3,
                    close=close,
                    data_source=source,
                )
            )


def _seed_paired_bars(db, *, count=60, stock_source="fixture:qfq", benchmark_source="fixture:qfq"):
    anchor = date(2026, 1, 2)
    _seed_series(
        db,
        code="AAPL",
        start=anchor,
        closes=[100.0] + [100.0 + index for index in range(1, count + 1)],
        source=stock_source,
    )
    _seed_series(
        db,
        code="SPY",
        start=anchor,
        closes=[200.0] + [200.0 + index for index in range(1, count + 1)],
        source=benchmark_source,
    )


@pytest.mark.parametrize(
    ("horizon", "days"),
    [("5d", 5), ("20d", 20), ("60d", 60)],
)
def test_evaluates_exact_forward_trading_bars_and_persists_metrics(
    isolated_db, horizon, days
) -> None:
    _context(isolated_db)
    _seed_paired_bars(isolated_db)
    service = DecisionQualityService(db_manager=isolated_db)

    result = service.evaluate_outcome(signal_id=101, horizon=horizon)

    assert result["eval_status"] == "complete"
    assert result["eval_window_days"] == days
    stock_return = ((100.0 + days) / 101.0 - 1) * 100
    benchmark_return = ((200.0 + days) / 201.0 - 1) * 100
    assert result["start_price"] == 101.0
    assert result["end_close"] == 100.0 + days
    assert result["stock_return_pct"] == pytest.approx(stock_return)
    assert result["benchmark_return_pct"] == pytest.approx(benchmark_return)
    assert result["excess_return_pct"] == pytest.approx(stock_return - benchmark_return)
    assert result["max_favorable_excursion_pct"] == pytest.approx(
        ((100.0 + days + 2.0) / 101.0 - 1) * 100
    )
    assert result["max_adverse_excursion_pct"] == pytest.approx((98.0 / 101.0 - 1) * 100)
    assert result["instrument_type"] == "equity"
    persisted = DecisionQualityRepository(isolated_db).list_quality_outcomes(
        signal_id=101,
        horizon=horizon,
        engine_version=DECISION_QUALITY_ENGINE_VERSION,
    )
    assert len(persisted) == 1


def test_hold_and_exit_normalized_action_returns(isolated_db) -> None:
    _context(isolated_db, signal_id=101, position_action="hold")
    _context(isolated_db, signal_id=102, position_action="exit")
    _seed_paired_bars(isolated_db, count=5)
    service = DecisionQualityService(db_manager=isolated_db)

    hold = service.evaluate_outcome(signal_id=101, horizon="5d")
    exited = service.evaluate_outcome(signal_id=102, horizon="5d")

    expected_return = (105.0 / 101.0 - 1) * 100
    assert hold["normalized_action_return_pct"] == pytest.approx(expected_return)
    assert hold["decision_value_vs_hold_pct"] == pytest.approx(0.0)
    assert hold["hindsight_regret_pct"] == pytest.approx(0.0)
    assert exited["normalized_action_return_pct"] == 0.0
    assert exited["decision_value_vs_hold_pct"] == pytest.approx(-expected_return)
    assert exited["hindsight_regret_pct"] == pytest.approx(expected_return)


@pytest.mark.parametrize(
    ("position_action", "incremental_action"),
    [("reduce", "wait"), ("hold", "add_in_batches")],
)
def test_missing_exposure_or_tranche_contract_keeps_decision_value_unable(
    isolated_db, position_action, incremental_action
) -> None:
    _context(
        isolated_db,
        position_action=position_action,
        incremental_action=incremental_action,
    )
    _seed_paired_bars(isolated_db, count=5)

    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101, horizon="5d"
    )

    assert result["eval_status"] == "complete"
    assert result["decision_value_status"] == "unable"
    assert result["unable_reason"] == "exposure_contract_missing"
    assert result["normalized_action_return_pct"] is None


def test_missing_context_is_explicitly_unable(isolated_db) -> None:
    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=999, horizon="5d"
    )
    assert result["eval_status"] == "unable"
    assert result["unable_reason"] == "missing_context"


def test_missing_benchmark_identity_is_explicitly_unable(isolated_db) -> None:
    _context(isolated_db, benchmark_code=None)
    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101, horizon="5d"
    )
    assert result["unable_reason"] == "missing_benchmark_identity"


def test_missing_frozen_instrument_type_is_explicitly_unable(isolated_db) -> None:
    _context(isolated_db, instrument_type=None)

    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101, horizon="5d"
    )

    assert result["unable_reason"] == "instrument_type_missing"


def test_missing_anchor_or_benchmark_anchor_is_explicitly_unable(isolated_db) -> None:
    _context(isolated_db)
    service = DecisionQualityService(db_manager=isolated_db)
    assert service.evaluate_outcome(signal_id=101, horizon="5d")["unable_reason"] == "missing_anchor_price"

    _seed_series(
        isolated_db,
        code="AAPL",
        start=date(2026, 1, 2),
        closes=[100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
    )
    assert service.evaluate_outcome(signal_id=101, horizon="5d")["unable_reason"] == "missing_benchmark_anchor"


def test_insufficient_forward_bars_never_calculates_partial_horizon(isolated_db) -> None:
    _context(isolated_db)
    _seed_paired_bars(isolated_db, count=4)

    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101, horizon="5d"
    )

    assert result["eval_status"] == "unable"
    assert result["unable_reason"] == "insufficient_forward_bars"
    assert result["end_close"] is None
    assert result["stock_return_pct"] is None


def test_adjustment_marker_mismatch_fails_closed(isolated_db) -> None:
    _context(isolated_db)
    _seed_paired_bars(
        isolated_db,
        count=5,
        stock_source="fixture:qfq",
        benchmark_source="fixture:raw",
    )

    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101, horizon="5d"
    )

    assert result["eval_status"] == "unable"
    assert result["unable_reason"] == "corporate_action_adjustment_unknown"


def test_intraday_cutoff_uses_previous_observation_and_next_tradable_execution(isolated_db) -> None:
    _context(
        isolated_db,
        decision_cutoff=datetime(2026, 1, 2, 16, 0),
        decision_market_phase="intraday",
    )
    _seed_series(
        isolated_db,
        code="AAPL",
        start=date(2026, 1, 1),
        closes=[90.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
    )
    _seed_series(
        isolated_db,
        code="SPY",
        start=date(2026, 1, 1),
        closes=[190.0, 200.0, 201.0, 202.0, 203.0, 204.0, 205.0],
    )

    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101,
        horizon="5d",
    )

    assert result["eval_status"] == "complete"
    assert result["observation_anchor_date"] == date(2026, 1, 1)
    assert result["shadow_execution_date"] == date(2026, 1, 3)
    assert result["start_price"] == 101.0
    assert result["end_close"] == 105.0


def test_market_symbol_mismatch_is_instrument_identity_ambiguous(isolated_db) -> None:
    _context(isolated_db, market="hk", stock_code="AAPL")
    _seed_paired_bars(isolated_db, count=5)

    result = DecisionQualityService(db_manager=isolated_db).evaluate_outcome(
        signal_id=101,
        horizon="5d",
    )

    assert result["eval_status"] == "unable"
    assert result["unable_reason"] == "instrument_identity_ambiguous"
