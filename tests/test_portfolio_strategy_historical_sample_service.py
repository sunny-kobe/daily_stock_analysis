# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/strategy_validation/synthetic_frozen_historical_source_v1.json"


def _sha256_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refresh_claimed_source_hash(source: dict) -> None:
    payload = copy.deepcopy(source)
    payload.pop("source_snapshot_hash", None)
    source["source_snapshot_hash"] = _sha256_json(payload)


def _source_with_valid_bars() -> dict:
    source = json.loads(FIXTURE.read_text())
    candidate = source["candidates"][0]
    start = datetime.fromisoformat("2025-01-06T09:30:00+08:00")
    candidate["bars"].extend(
        {
            "timestamp": (start + timedelta(days=index)).isoformat(),
            "tradable": True,
            "open": 100.0 + index,
            "close": 101.0 + index,
            "high": 102.0 + index,
            "low": 99.0 + index,
            "as_of": (start + timedelta(days=index)).isoformat(),
            "source": "synthetic-fixture",
            "source_hash": _sha256_json({"instrument": index}),
        }
        for index in range(61)
    )
    candidate["benchmark"]["bars"] = [
        {
            "timestamp": bar["timestamp"],
            "tradable": bar["tradable"],
            "open": 200.0 + index,
            "close": 201.0 + index,
            "high": 202.0 + index,
            "low": 199.0 + index,
            "as_of": bar["as_of"],
            "source": "synthetic-fixture",
            "source_hash": _sha256_json({"benchmark": index}),
        }
        for index, bar in enumerate(candidate["bars"])
    ]
    _refresh_claimed_source_hash(source)
    return source


def _build(source: dict) -> dict:
    from src.services.portfolio_strategy_historical_sample_service import (
        PortfolioStrategyHistoricalSampleService,
    )

    return PortfolioStrategyHistoricalSampleService().build(source=source)


def test_build_event_uses_first_tradable_open_strictly_after_cutoff() -> None:
    dataset = _build(_source_with_valid_bars())

    event = dataset["eligible_events"][0]
    assert event["execution"]["timestamp"] == "2025-01-06T09:30:00+08:00"
    assert event["execution"]["price"] == 100.0
    assert event["execution"]["benchmark_price"] == 201.0
    assert event["execution"]["instrument_bar_hash"] == _sha256_json({"instrument": 0})
    assert event["execution"]["benchmark_bar_hash"] == _sha256_json({"benchmark": 1})


def test_build_event_requires_5_20_60_valid_trading_bars() -> None:
    source = _source_with_valid_bars()
    source["candidates"][0]["bars"] = source["candidates"][0]["bars"][:21]
    source["candidates"][0]["benchmark"]["bars"] = source["candidates"][0]["benchmark"]["bars"][:21]
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_events"] == []
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "horizon_bars_insufficient"}
    ]


def test_build_manifest_records_candidate_and_exclusion_reasons() -> None:
    source = _source_with_valid_bars()
    source["candidates"].append({"candidate_id": "synthetic-incomplete"})
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["candidate_count"] == 2
    assert dataset["eligible_event_ids"] == ["synthetic-cn-hold-001"]
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-incomplete", "reason_code": "decision_evidence_invalid"}
    ]


def test_invalid_decision_cutoff_is_excluded_without_raising() -> None:
    source = _source_with_valid_bars()
    source["candidates"][0]["decision"]["decision_cutoff"] = "not-a-timestamp"
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_events"] == []
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "decision_evidence_invalid"}
    ]


def test_semantic_duplicate_decisions_are_excluded() -> None:
    source = _source_with_valid_bars()
    duplicate = copy.deepcopy(source["candidates"][0])
    duplicate["candidate_id"] = "synthetic-cn-hold-duplicate"
    source["candidates"].append(duplicate)
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_event_ids"] == ["synthetic-cn-hold-001"]
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-duplicate", "reason_code": "semantic_duplicate_decision"}
    ]


def test_same_frozen_input_produces_same_source_and_event_set_hash() -> None:
    source = _source_with_valid_bars()

    first = _build(source)
    second = _build(copy.deepcopy(source))

    assert first["source_snapshot_hash"] == source["source_snapshot_hash"]
    assert first["eligible_event_set_hash"] == second["eligible_event_set_hash"]
    assert first["dataset_hash"] == second["dataset_hash"]


def test_builder_makes_no_network_or_database_calls() -> None:
    with patch("socket.create_connection", side_effect=AssertionError("network called")) as network:
        dataset = _build(_source_with_valid_bars())

    assert dataset["eligible_event_ids"] == ["synthetic-cn-hold-001"]
    network.assert_not_called()


def test_rejects_claimed_source_hash_after_bar_is_tampered() -> None:
    source = _source_with_valid_bars()
    source["candidates"][0]["bars"][1]["open"] = 999.0

    with pytest.raises(ValueError, match="source_snapshot_hash_mismatch"):
        _build(source)


def test_synthetic_classification_is_required_and_boolean() -> None:
    source = _source_with_valid_bars()
    source.pop("synthetic")
    _refresh_claimed_source_hash(source)

    with pytest.raises(ValueError, match="synthetic_classification_required"):
        _build(source)


def test_execution_bar_may_be_available_after_its_timestamp_before_freeze() -> None:
    source = _source_with_valid_bars()
    execution_bar = source["candidates"][0]["bars"][1]
    execution_bar["as_of"] = "2025-01-06T12:00:00+08:00"
    source["candidates"][0]["benchmark"]["bars"][1]["as_of"] = execution_bar["as_of"]
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_events"][0]["execution"]["price"] == 100.0
    assert dataset["eligible_events"][0]["horizon_results"]["5d"]["end_close"] == 106.0


def test_bar_after_frozen_at_is_excluded_from_horizon() -> None:
    source = _source_with_valid_bars()
    source["frozen_at"] = "2025-03-01T16:00:00+08:00"
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_events"] == []
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "horizon_bars_insufficient"}
    ]


def test_semantic_duplicate_ignores_distinct_decision_id() -> None:
    source = _source_with_valid_bars()
    duplicate = copy.deepcopy(source["candidates"][0])
    duplicate["candidate_id"] = "synthetic-cn-hold-duplicate-id"
    duplicate["decision"]["decision_id"] = "different-transport-id"
    source["candidates"].append(duplicate)
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_event_ids"] == ["synthetic-cn-hold-001"]
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-duplicate-id", "reason_code": "semantic_duplicate_decision"}
    ]


def test_same_day_duplicate_with_a_different_decision_time_is_excluded() -> None:
    source = _source_with_valid_bars()
    duplicate = copy.deepcopy(source["candidates"][0])
    duplicate["candidate_id"] = "synthetic-cn-hold-same-day"
    duplicate["decision"].update(
        decision_id="same-day-different-id",
        decision_cutoff="2025-01-02T15:30:00+08:00",
        as_of="2025-01-02T15:30:00+08:00",
    )
    source["candidates"].append(duplicate)
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_event_ids"] == ["synthetic-cn-hold-001"]
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-same-day", "reason_code": "semantic_duplicate_decision"}
    ]


def test_decision_input_hash_must_match_canonical_structured_inputs() -> None:
    source = _source_with_valid_bars()
    source["candidates"][0]["structured_inputs"]["risk_gate"] = "blocked"
    _refresh_claimed_source_hash(source)

    dataset = _build(source)

    assert dataset["eligible_events"] == []
    assert dataset["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "decision_input_hash_mismatch"}
    ]


def test_reporting_currency_and_fx_contract_are_propagated_and_validated() -> None:
    source = _source_with_valid_bars()
    cny_dataset = _build(source)
    assert cny_dataset["reporting_currency"] == "CNY"
    assert cny_dataset["eligible_events"][0]["fx"]["pair"] == "CNY/CNY"
    assert cny_dataset["eligible_events"][0]["fx"]["rate"] == 1.0

    candidate = source["candidates"][0]
    candidate["identity"].update(market="us", symbol="QQQM", currency="USD")
    candidate["benchmark"].update(symbol="SPY", currency="USD")
    candidate["fx"].update(pair="USD/CNY", rate=7.2)
    _refresh_claimed_source_hash(source)

    dataset = _build(source)
    assert dataset["reporting_currency"] == "CNY"
    assert dataset["eligible_events"][0]["reporting_currency"] == "CNY"
    assert dataset["eligible_events"][0]["fx"]["pair"] == "USD/CNY"

    invalid_fx = copy.deepcopy(source)
    invalid_fx["candidates"][0]["fx"]["pair"] = "USD/USD"
    _refresh_claimed_source_hash(invalid_fx)
    assert _build(invalid_fx)["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "fx_pair_mismatch"}
    ]

    invalid_benchmark = copy.deepcopy(source)
    invalid_benchmark["candidates"][0]["benchmark"]["currency"] = "CNY"
    _refresh_claimed_source_hash(invalid_benchmark)
    assert _build(invalid_benchmark)["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "benchmark_currency_mismatch"}
    ]


def test_event_set_hash_is_independent_of_candidate_order() -> None:
    source = _source_with_valid_bars()
    second = copy.deepcopy(source["candidates"][0])
    second["candidate_id"] = "synthetic-cn-hold-002"
    second["decision"]["account_id"] = "synthetic-account-002"
    source["candidates"].append(second)
    _refresh_claimed_source_hash(source)
    reversed_source = copy.deepcopy(source)
    reversed_source["candidates"].reverse()
    _refresh_claimed_source_hash(reversed_source)

    assert _build(source)["eligible_event_set_hash"] == _build(reversed_source)["eligible_event_set_hash"]


def test_daily_leveraged_product_requires_and_propagates_evidence() -> None:
    source = _source_with_valid_bars()
    candidate = source["candidates"][0]
    candidate["identity"]["instrument_type"] = "daily_leveraged_product"
    candidate["product"]["instrument_type"] = "daily_leveraged_product"
    _refresh_claimed_source_hash(source)

    missing = _build(source)
    assert missing["eligible_events"] == []
    assert missing["excluded_events"] == [
        {"candidate_id": "synthetic-cn-hold-001", "reason_code": "daily_leveraged_product_evidence_invalid"}
    ]

    candidate["product"].update(
        reset_frequency="daily",
        underlying_identity="000300",
        daily_reset_evidence={
            "reset_frequency": "daily",
            "as_of": candidate["decision"]["decision_cutoff"],
            "source": "synthetic-fixture",
            "source_hash": "3333333333333333333333333333333333333333333333333333333333333333",
        },
        underlying_evidence={
            "underlying_identity": "000300",
            "as_of": candidate["decision"]["decision_cutoff"],
            "source": "synthetic-fixture",
            "source_hash": "4444444444444444444444444444444444444444444444444444444444444444",
        },
    )
    _refresh_claimed_source_hash(source)

    event = _build(source)["eligible_events"][0]
    assert event["daily_reset_evidence"]["reset_frequency"] == "daily"
    assert event["underlying_evidence"]["underlying_identity"] == "000300"
