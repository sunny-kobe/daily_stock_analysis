# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from src.config import Config
from src.services.strategy_registry_service import StrategyRegistryService
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'strategy_registry.db'}")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _manifest():
    return {
        "strategy_key": "portfolio-hold-baseline",
        "version": "1.0.0",
        "name": "持有基线",
        "change_summary": "固定持有作为比较基准",
        "changed_dimension": "baseline",
        "markets": ["cn", "hk", "us"],
        "instrument_types": ["equity", "etf"],
        "horizons": ["5d", "20d", "60d"],
        "evaluation_mode": "historical_and_forward",
        "policy": {"instruction": "hold"},
        "cost_model": {
            "commission_bps": 3.0,
            "tax_bps": 0.0,
            "slippage_bps": 5.0,
            "fx_bps": 0.0,
            "product_cost_bps": 0.0,
        },
        "benchmark_policy": {
            "selection": "decision_time_market",
            "benchmarks": {"cn": "000300", "hk": "HSI", "us": "SPY"},
        },
        "status": "draft",
    }


def _forward_only_manifest():
    manifest = _manifest()
    manifest.update(
        {
            "strategy_key": "portfolio-current-policy",
            "name": "当前策略",
            "evaluation_mode": "forward_only",
        }
    )
    return manifest


def _run(*, kind="historical_backtest", qualifying=True, status="completed"):
    return {
        "strategy_key": "portfolio-hold-baseline",
        "strategy_version": "1.0.0",
        "validation_kind": kind,
        "protocol": {"execution_price": "next_bar_open"},
        "dataset_hash": ("a" if kind == "historical_backtest" else "b") * 64,
        "engine_version": "portfolio-strategy-v1",
        "status": status,
        "qualifying": qualifying,
        "result": {"historical_status": "complete", "sample_count": 20},
    }


def test_create_version_is_idempotent_and_starts_in_draft(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)

    first = service.create_version(_manifest())
    repeated = service.create_version(_manifest())

    assert first["manifest_hash"] == repeated["manifest_hash"]
    assert service.get_version("portfolio-hold-baseline", "1.0.0")["status"] == "draft"


def test_transition_requires_explicit_human_reason(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)
    service.create_version(_manifest())

    with pytest.raises(ValueError, match="human_reason_required"):
        service.transition(
            strategy_key="portfolio-hold-baseline",
            version="1.0.0",
            to_status="backtest_running",
            human_reason="",
        )


def test_simulation_requires_a_completed_qualifying_run(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)
    service.create_version(_manifest())
    service.transition(
        strategy_key="portfolio-hold-baseline",
        version="1.0.0",
        to_status="backtest_running",
        human_reason="开始回测",
    )

    with pytest.raises(ValueError, match="qualifying_backtest_required"):
        service.transition(
            strategy_key="portfolio-hold-baseline",
            version="1.0.0",
            to_status="simulation",
            human_reason="申请模拟观察",
        )

    service.record_validation_run(_run())
    transitioned = service.transition(
        strategy_key="portfolio-hold-baseline",
        version="1.0.0",
        to_status="simulation",
        human_reason="回测合格，进入模拟观察",
    )
    assert transitioned["status"] == "simulation"


def test_forward_only_strategy_starts_simulation_with_a_human_reason(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)
    service.create_version(_forward_only_manifest())

    version = service.get_version("portfolio-current-policy", "1.0.0")
    assert version["allowed_transitions"] == ["simulation"]

    with pytest.raises(ValueError, match="human_reason_required"):
        service.transition(
            strategy_key="portfolio-current-policy",
            version="1.0.0",
            to_status="simulation",
            human_reason="",
        )

    transitioned = service.transition(
        strategy_key="portfolio-current-policy",
        version="1.0.0",
        to_status="simulation",
        human_reason="从今天开始记录模拟建议",
    )

    assert transitioned["status"] == "simulation"


def test_small_capital_requires_completed_forward_observation(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)
    service.create_version(_manifest())
    service.transition(
        strategy_key="portfolio-hold-baseline",
        version="1.0.0",
        to_status="backtest_running",
        human_reason="开始回测",
    )
    service.record_validation_run(_run())
    service.transition(
        strategy_key="portfolio-hold-baseline",
        version="1.0.0",
        to_status="simulation",
        human_reason="进入模拟观察",
    )

    with pytest.raises(ValueError, match="forward_observation_required"):
        service.transition(
            strategy_key="portfolio-hold-baseline",
            version="1.0.0",
            to_status="small_capital",
            human_reason="申请小额验证",
        )

    service.record_validation_run(_run(kind="forward_observation"))
    result = service.transition(
        strategy_key="portfolio-hold-baseline",
        version="1.0.0",
        to_status="small_capital",
        human_reason="模拟样本合格，人工批准小额验证",
    )
    assert result["status"] == "small_capital"


def test_saving_a_run_never_changes_strategy_stage(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)
    service.create_version(_manifest())

    service.record_validation_run(_run())

    assert service.get_version("portfolio-hold-baseline", "1.0.0")["status"] == "draft"


def test_only_the_manual_lifecycle_is_allowed(isolated_db) -> None:
    service = StrategyRegistryService(db_manager=isolated_db)
    service.create_version(_manifest())

    with pytest.raises(ValueError, match="invalid_strategy_transition"):
        service.transition(
            strategy_key="portfolio-hold-baseline",
            version="1.0.0",
            to_status="active",
            human_reason="跳过验证",
        )
