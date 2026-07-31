# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from src.config import Config
from src.repositories.strategy_validation_repo import StrategyValidationRepository
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'strategy_validation.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _version_fields(**overrides):
    fields = {
        "strategy_key": "portfolio-hold-baseline",
        "version": "1.0.0",
        "name": "持有基线",
        "initial_status": "draft",
        "manifest_json": json.dumps({"policy": "hold"}, sort_keys=True),
        "manifest_hash": "a" * 64,
    }
    fields.update(overrides)
    return fields


def _run_fields(**overrides):
    fields = {
        "run_id": "run-001",
        "strategy_key": "portfolio-hold-baseline",
        "strategy_version": "1.0.0",
        "validation_kind": "historical_backtest",
        "protocol_json": json.dumps({"execution_price": "next_bar_open"}, sort_keys=True),
        "dataset_hash": "b" * 64,
        "engine_version": "portfolio-strategy-v1",
        "status": "completed",
        "qualifying": True,
        "result_json": json.dumps({"sample_count": 12}, sort_keys=True),
        "run_hash": "c" * 64,
    }
    fields.update(overrides)
    return fields


def test_repository_creates_three_additive_tables(isolated_db) -> None:
    with isolated_db.get_session() as session:
        names = {
            row[0]
            for row in session.execute(
                __import__("sqlalchemy").text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ).all()
        }

    assert {
        "portfolio_strategy_versions",
        "portfolio_strategy_validation_runs",
        "portfolio_strategy_transitions",
    }.issubset(names)


def test_strategy_version_is_idempotent_by_manifest_and_immutable(isolated_db) -> None:
    repo = StrategyValidationRepository(isolated_db)

    first, created = repo.create_strategy_version(_version_fields())
    repeated, repeated_created = repo.create_strategy_version(_version_fields())

    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id
    with pytest.raises(ValueError, match="strategy_version_immutable"):
        repo.create_strategy_version(_version_fields(name="改写后的名称"))


def test_completed_validation_run_is_idempotent_and_immutable(isolated_db) -> None:
    repo = StrategyValidationRepository(isolated_db)
    repo.create_strategy_version(_version_fields())

    first, created = repo.create_validation_run(_run_fields())
    repeated, repeated_created = repo.create_validation_run(_run_fields())

    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id
    with pytest.raises(ValueError, match="validation_run_immutable"):
        repo.create_validation_run(_run_fields(result_json=json.dumps({"sample_count": 99})))


def test_not_available_history_cannot_be_a_qualifying_backtest(isolated_db) -> None:
    repo = StrategyValidationRepository(isolated_db)
    repo.create_strategy_version(_version_fields())
    repo.create_validation_run(
        _run_fields(
            result_json=json.dumps({"historical_status": "not_available"}),
        )
    )

    assert repo.has_qualifying_run(
        strategy_key="portfolio-hold-baseline",
        strategy_version="1.0.0",
        validation_kind="historical_backtest",
    ) is False


def test_transitions_are_append_only(isolated_db) -> None:
    repo = StrategyValidationRepository(isolated_db)
    repo.create_strategy_version(_version_fields())

    first = repo.append_transition(
        strategy_key="portfolio-hold-baseline",
        strategy_version="1.0.0",
        from_status="draft",
        to_status="backtest_running",
        human_reason="开始固定规则回测",
    )
    second = repo.append_transition(
        strategy_key="portfolio-hold-baseline",
        strategy_version="1.0.0",
        from_status="backtest_running",
        to_status="simulation",
        human_reason="回测符合进入模拟观察的条件",
    )

    rows = repo.list_transitions(
        strategy_key="portfolio-hold-baseline", strategy_version="1.0.0"
    )
    assert [row.id for row in rows] == [first.id, second.id]
    assert [row.to_status for row in rows] == ["backtest_running", "simulation"]
