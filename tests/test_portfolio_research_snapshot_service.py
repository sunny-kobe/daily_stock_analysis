# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from sqlalchemy import func, select

from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.decision_evidence_snapshot_service import (
    DecisionEvidenceSnapshotService,
)
from src.services.portfolio_instrument_service import PortfolioInstrumentService
from src.services.portfolio_risk_policy_service import PortfolioRiskPolicyService
from src.services.strategy_registry_service import (
    StrategyRegistryService,
    load_strategy_manifest,
)
from src.storage import (
    DatabaseManager,
    DecisionSignalOutcomeRecord,
    DecisionSignalRecord,
    PortfolioDailySnapshot,
    PortfolioPosition,
    StockDaily,
)


class PortfolioResearchSnapshotServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "research_snapshot.db"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.repo = PortfolioRepository(db_manager=self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _service(self, **kwargs):
        module_name = "src.services.portfolio_research_snapshot_service"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "portfolio research snapshot service module is required",
        )
        module = importlib.import_module(module_name)
        return module.PortfolioResearchSnapshotService(repo=self.repo, **kwargs)

    def test_benchmark_identity_uses_current_strategy_policy(self) -> None:
        benchmarks = self._service()._benchmark_payload(
            [
                {"market": "cn"},
                {"market": "hk"},
                {"market": "us"},
            ]
        )

        self.assertEqual(
            {item["market"]: item["code"] for item in benchmarks},
            {"cn": "000300", "hk": "HSI", "us": "SPY"},
        )
        self.assertTrue(all(item["type"] == "strategy_benchmark" for item in benchmarks))

    def test_benchmark_identity_ignores_markets_outside_strategy_policy(self) -> None:
        benchmarks = self._service()._benchmark_payload(
            [{"market": "jp"}, {"market": "kr"}, {"market": "us"}]
        )

        self.assertEqual(
            [(item["market"], item["code"]) for item in benchmarks],
            [("us", "SPY")],
        )

    def _seed_active_signal(
        self,
        *,
        account_id: int,
        created_at: datetime,
        updated_at: datetime,
        position_action: str = "hold",
        extra_metadata: dict | None = None,
    ) -> int:
        metadata = {
            "quality_context_status": "complete",
            "portfolio_decision": {
                "account_id": account_id,
                "market": "us",
                "stock_code": "AAPL",
                "position_action": position_action,
                "incremental_action": "wait",
                "evidence_cutoff": "2026-07-22T08:00:00Z",
            },
            **(extra_metadata or {}),
        }
        with self.db.get_session() as session:
            row = DecisionSignalRecord(
                stock_code="AAPL",
                stock_name="Apple",
                market="us",
                source_type="analysis",
                trigger_source="portfolio",
                action="hold",
                reason="Frozen reason",
                status="active",
                created_at=created_at,
                updated_at=updated_at,
                metadata_json=json.dumps(metadata),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id)

    def _seed_cached_position(self) -> int:
        account = self.repo.create_account(
            name="Private Account Name",
            broker="Private Broker",
            market="us",
            base_currency="USD",
            owner_id="private-owner",
        )
        self.repo.replace_positions_lots_and_snapshot(
            account_id=account.id,
            snapshot_date=date(2026, 7, 22),
            cost_method="fifo",
            base_currency="USD",
            total_cash=5000,
            total_market_value=1100,
            total_equity=6100,
            unrealized_pnl=100,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "currency": "USD",
                    "quantity": 10,
                    "avg_cost": 100,
                    "total_cost": 1000,
                    "last_price": 110,
                    "market_value_base": 1100,
                    "unrealized_pnl_base": 100,
                }
            ],
            lots=[],
            valuation_currency="USD",
        )
        StockRepository(self.db).save_dataframe(
            pd.DataFrame(
                [
                    {
                        "date": date(2026, 7, 22),
                        "open": 110.0,
                        "high": 110.0,
                        "low": 110.0,
                        "close": 110.0,
                        "volume": 1.0,
                    }
                ]
            ),
            "AAPL",
            "YfinanceFetcher|adjustment=adjusted",
        )
        return account.id

    def _seed_control_plane(self) -> None:
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "AAPL",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1,
                "verification_status": "verified",
                "evidence_source": "NASDAQ symbol directory",
                "evidence_as_of": datetime(2026, 7, 22, 7, 0, 0, tzinfo=timezone.utc),
                "metadata": {
                    "private_note": "must not leave DSA snapshot",
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-22",
                        "source": "https://investor.apple.com/",
                        "exposures": [
                            {"sector": "Technology Hardware", "weight_pct": 100},
                        ],
                    },
                },
            }
        )
        PortfolioRiskPolicyService(repo=self.repo).save_policy(
            {
                "min_cash_buffer_pct": 10,
                "max_single_position_pct": 25,
                "max_sector_pct": 40,
                "max_high_risk_product_pct": 5,
                "max_portfolio_drawdown_pct": 15,
            }
        )

    def _seed_prior_daily_snapshot(
        self,
        account_id: int,
        *,
        total_equity: float = 6500,
    ) -> None:
        with self.db.get_session() as session:
            session.add(
                PortfolioDailySnapshot(
                    account_id=account_id,
                    snapshot_date=date(2026, 7, 21),
                    cost_method="fifo",
                    base_currency="USD",
                    total_cash=5000,
                    total_market_value=total_equity - 5000,
                    total_equity=total_equity,
                    unrealized_pnl=0,
                    realized_pnl=0,
                    fee_total=0,
                    tax_total=0,
                    fx_stale=False,
                    payload="{}",
                )
            )
            session.commit()

    def _mutable_row_counts(self) -> dict[str, int]:
        models = (
            PortfolioPosition,
            PortfolioDailySnapshot,
            DecisionSignalRecord,
            DecisionSignalOutcomeRecord,
        )
        with self.db.get_session() as session:
            return {
                model.__tablename__: session.execute(
                    select(func.count()).select_from(model)
                ).scalar_one()
                for model in models
            }

    def test_snapshot_is_canonical_hashed_and_excludes_private_account_fields(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "MSFT",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "equity",
                "trade_lot_size": 1,
                "verification_status": "provisional",
            }
        )
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)

        first = self._service().build(cutoff=cutoff)
        second = self._service().build(cutoff=cutoff)

        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual(first["universe_hash"], second["universe_hash"])
        self.assertEqual(first["positions"][0]["symbol"], "AAPL")
        self.assertNotIn("avg_cost", first["positions"][0])
        self.assertNotIn("total_cost", first["positions"][0])
        self.assertNotIn("unrealized_pnl_base", first["positions"][0])
        self.assertEqual(
            [item["symbol"] for item in first["instruments"]],
            ["AAPL", "MSFT"],
        )
        aapl = next(item for item in first["instruments"] if item["symbol"] == "AAPL")
        self.assertEqual(aapl["evidence_as_of"], "2026-07-22T07:00:00+00:00")
        self.assertIn(
            "decision_price_stale",
            {item["code"] for item in first["hard_blockers"]},
        )
        self.assertIn(
            "portfolio_risk_budget_thresholds_not_evaluated",
            first["limitations"],
        )
        rendered = str(first)
        self.assertNotIn("private-owner", rendered)
        self.assertNotIn("Private Broker", rendered)
        self.assertNotIn("Private Account Name", rendered)
        self.assertNotIn("private_note", rendered)

    def test_snapshot_emits_complete_source_metadata_from_prepared_bars(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        StockRepository(self.db).save_dataframe(
            pd.DataFrame(
                [
                    {
                        "date": date(2026, 7, 22),
                        "open": 110.0,
                        "high": 110.0,
                        "low": 110.0,
                        "close": 110.0,
                        "volume": 1.0,
                    }
                ]
            ),
            "AAPL",
            "YfinanceFetcher|adjustment=adjusted",
        )
        StockRepository(self.db).save_dataframe(
            pd.DataFrame(
                [
                    {
                        "date": date(2026, 7, 22),
                        "open": 620.0,
                        "high": 620.0,
                        "low": 620.0,
                        "close": 620.0,
                        "volume": 1.0,
                    }
                ]
            ),
            "SPY",
            "YfinanceFetcher|adjustment=adjusted",
        )

        snapshot = self._service().build(
            cutoff=datetime.now(timezone.utc) + timedelta(seconds=1)
        )

        position = snapshot["positions"][0]
        account = snapshot["accounts"][0]
        instrument = next(
            item for item in snapshot["instruments"] if item["symbol"] == "AAPL"
        )
        benchmark = snapshot["benchmarks"][0]
        for item, fields in (
            (account, ("evidence_source", "evidence_version", "evidence_hash")),
            (position, ("price_source", "price_source_version", "price_source_hash")),
            (instrument, ("evidence_version", "evidence_hash", "adjustment_identity")),
            (benchmark, ("evidence_as_of", "evidence_hash", "adjustment_identity")),
            (snapshot["risk_policy"], ("evidence_source", "evidence_version", "evidence_hash")),
            (snapshot["risk_budget"], ("as_of", "evidence_source", "evidence_version", "evidence_hash")),
        ):
            self.assertTrue(all(item.get(field) for field in fields), (item, fields))
        self.assertEqual(position["last_price"], 110.0)
        self.assertEqual(benchmark["price"], 620.0)
        self.assertEqual(position["adjustment_identity"], "adjusted")

    def test_prepared_snapshot_can_freeze_complete_decision_evidence(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)
        latest_finalized_day = date.today() - timedelta(days=1)
        for code, close in (("AAPL", 210.0), ("SPY", 620.0)):
            StockRepository(self.db).save_dataframe(
                pd.DataFrame(
                    [
                        {
                            "date": latest_finalized_day,
                            "open": close,
                            "high": close,
                            "low": close,
                            "close": close,
                            "volume": 1.0,
                        }
                    ]
                ),
                code,
                "YfinanceFetcher|adjustment=adjusted",
            )
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=2)
        snapshot = self._service().build(cutoff=cutoff)
        StrategyRegistryService(self.db).create_version(load_strategy_manifest())

        result = DecisionEvidenceSnapshotService(db_manager=self.db).freeze(
            signal={
                "id": 101,
                "market": "us",
                "stock_code": "AAPL",
                "created_at": snapshot["cutoff"],
            },
            portfolio_decision={
                "account_id": account_id,
                "position_action": "hold",
                "incremental_action": "wait",
                "confidence_by_horizon": {"5d": 0.5, "20d": 0.5, "60d": 0.5},
                "supporting_evidence": ["盈利保持稳定"],
                "opposing_evidence": ["估值仍高"],
                "watch_conditions": ["等待下一次财报"],
                "invalidation": "盈利趋势反转",
                "next_review": "下一次财报后",
            },
            research_snapshot=snapshot,
            portfolio_context={"account_id": account_id},
            context_snapshot={
                "decision_evidence": [
                    {
                        "schema_version": "decision-source-envelope-v1",
                        "as_of": snapshot["cutoff"],
                        "source": "frozen-analysis",
                        "source_version": "analysis-v1",
                        "source_hash": "7" * 64,
                        "body": {"summary": "冻结研究证据"},
                    }
                ]
            },
        )

        self.assertEqual(result["status"], "complete", result["unable_reasons"])

    def test_snapshot_marks_stale_strategy_benchmark_as_blocking(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        StockRepository(self.db).save_dataframe(
            pd.DataFrame(
                [
                    {
                        "date": date(2026, 7, 22),
                        "open": 620.0,
                        "high": 620.0,
                        "low": 620.0,
                        "close": 620.0,
                        "volume": 1.0,
                    }
                ]
            ),
            "SPY",
            "YfinanceFetcher|adjustment=adjusted",
        )

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)
        )

        self.assertTrue(snapshot["benchmarks"][0]["stale"])
        self.assertIn(
            "benchmark_price_stale",
            {item["code"] for item in snapshot["hard_blockers"]},
        )

    def test_snapshot_recomputes_fx_staleness_at_cutoff(self) -> None:
        self._seed_cached_position()
        cutoff = datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)
        self.repo.save_fx_rate(
            from_currency="USD",
            to_currency="CNY",
            rate_date=date(2026, 7, 23),
            rate=7.2,
            source="test-fx@1",
            is_stale=False,
        )
        position = self.repo.list_cached_positions(cost_method="fifo")[0]
        position.valuation_currency = "CNY"
        blockers: list[dict] = []

        payload = self._service()._position_payload(
            position,
            price_bar=StockRepository(self.db).get_start_daily(
                code="AAPL",
                analysis_date=cutoff.date(),
            ),
            cutoff=cutoff.replace(tzinfo=None),
            blockers=blockers,
        )

        self.assertTrue(payload["fx"]["stale"])
        self.assertIn("fx_rate_stale", {item["code"] for item in blockers})

    def test_point_in_time_contract_marks_current_capture_prospective_only(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)

        snapshot = self._service().build(cutoff=cutoff)

        eligibility = snapshot["point_in_time"]
        self.assertEqual(eligibility["scope"], "current_prospective")
        self.assertTrue(eligibility["prospective_decision_eligible"])
        self.assertFalse(eligibility["historical_replay_eligible"])
        self.assertEqual(eligibility["blockers"], [])
        self.assertEqual(
            set(eligibility["source_cutoffs"]),
            {
                "accounts",
                "position_cache",
                "daily_snapshots",
                "instrument_registry",
                "risk_policy",
                "decision_signals",
            },
        )

    def test_point_in_time_contract_blocks_sources_updated_after_cutoff(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        eligibility = snapshot["point_in_time"]
        self.assertFalse(eligibility["prospective_decision_eligible"])
        self.assertFalse(eligibility["historical_replay_eligible"])
        self.assertEqual(
            eligibility["blockers"],
            [
                "account_state_after_cutoff",
                "daily_snapshot_after_cutoff",
                "instrument_registry_after_cutoff",
                "position_cache_after_cutoff",
                "risk_policy_after_cutoff",
            ],
        )

    def test_frozen_active_signal_is_public_and_changes_snapshot_hash(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
            extra_metadata={"private_note": "must not leave snapshot"},
        )
        cutoff = datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc)

        first = self._service().build(cutoff=cutoff)
        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            metadata["portfolio_decision"]["position_action"] = "reduce"
            row.metadata_json = json.dumps(metadata)
            row.updated_at = datetime(2026, 7, 22, 8, 30, 0)
            session.commit()
        second = self._service().build(cutoff=cutoff)

        self.assertNotEqual(first["snapshot_hash"], second["snapshot_hash"])
        frozen = first["decision_signals"]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(
            set(frozen[0]),
            {
                "id",
                "market",
                "stock_code",
                "stock_name",
                "reason",
                "status",
                "created_at",
                "updated_at",
                "metadata",
            },
        )
        self.assertEqual(
            frozen[0]["metadata"]["portfolio_decision"]["position_action"],
            "hold",
        )
        self.assertNotIn("private_note", frozen[0]["metadata"])

    def test_point_in_time_blocks_active_signal_after_cutoff(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 8, 30, 0),
            updated_at=datetime(2026, 7, 22, 9, 30, 0),
        )

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            "decision_signal_after_cutoff",
            snapshot["point_in_time"]["blockers"],
        )

    def test_point_in_time_blocks_present_signal_with_missing_timestamp(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        signal_id = self._seed_active_signal(
            account_id=account_id,
            created_at=datetime(2026, 7, 22, 7, 30, 0),
            updated_at=datetime(2026, 7, 22, 8, 0, 0),
        )
        with self.db.get_session() as session:
            session.get(DecisionSignalRecord, signal_id).updated_at = None
            session.commit()

        snapshot = self._service().build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            "decision_signal_cutoff_missing",
            snapshot["point_in_time"]["blockers"],
        )

    def test_point_in_time_blocks_truncated_signal_capture(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        for minute in (0, 1):
            self._seed_active_signal(
                account_id=account_id,
                created_at=datetime(2026, 7, 22, 8, minute, 0),
                updated_at=datetime(2026, 7, 22, 8, minute, 0),
            )

        snapshot = self._service(max_decision_signals=1).build(
            cutoff=datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(len(snapshot["decision_signals"]), 1)
        self.assertIn(
            "decision_signal_snapshot_truncated",
            snapshot["point_in_time"]["blockers"],
        )

    def test_snapshot_reports_missing_identity_and_risk_policy_as_hard_blockers(self) -> None:
        self._seed_cached_position()

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        blocker_codes = {item["code"] for item in snapshot["hard_blockers"]}
        self.assertIn("portfolio_risk_policy_missing", blocker_codes)
        self.assertIn("instrument_identity_missing", blocker_codes)
        self.assertEqual(snapshot["completeness"], "INSUFFICIENT_EVIDENCE")

    def test_snapshot_build_is_read_only(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        before = self._mutable_row_counts()

        self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertEqual(self._mutable_row_counts(), before)

    def test_snapshot_proves_effective_agent_execution_architecture(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()
        cutoff = datetime(2026, 7, 22, 9, 0, 0)

        with patch.dict(os.environ, {"AGENT_ARCH": "single"}, clear=False):
            Config.reset_instance()
            single_snapshot = self._service().build(cutoff=cutoff)

        with patch.dict(os.environ, {"AGENT_ARCH": "multi"}, clear=False):
            Config.reset_instance()
            multi_snapshot = self._service().build(cutoff=cutoff)

        Config.reset_instance()
        self.assertEqual(
            single_snapshot["analysis_runtime"],
            {"architecture": "single", "automatic_multi_agent": False},
        )
        self.assertEqual(
            multi_snapshot["analysis_runtime"],
            {"architecture": "multi", "automatic_multi_agent": True},
        )

    def test_risk_budget_evaluates_complete_native_currency_scope(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        self.assertEqual(snapshot["risk_budget"]["base_scope"], "currency")
        self.assertEqual(snapshot["risk_budget"]["breaches"], [])
        usd_scope = snapshot["risk_budget"]["scopes"][0]
        self.assertEqual(usd_scope["currency"], "USD")
        self.assertAlmostEqual(usd_scope["cash_buffer_pct"], 5000 / 6100 * 100, places=4)
        self.assertAlmostEqual(usd_scope["max_single_position_pct"], 1100 / 6100 * 100, places=4)
        self.assertAlmostEqual(usd_scope["max_sector_pct"], 1100 / 6100 * 100, places=4)
        self.assertEqual(usd_scope["high_risk_product_pct"], 0.0)
        self.assertAlmostEqual(usd_scope["max_drawdown_pct"], 400 / 6500 * 100, places=4)
        self.assertNotIn(
            "portfolio_risk_budget_thresholds_not_evaluated",
            snapshot["limitations"],
        )

    def test_risk_budget_reports_breaches_without_becoming_unevaluated(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)
        PortfolioRiskPolicyService(repo=self.repo).save_policy(
            {
                "min_cash_buffer_pct": 90,
                "max_single_position_pct": 15,
                "max_sector_pct": 15,
                "max_portfolio_drawdown_pct": 5,
            }
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        self.assertEqual(
            {item["code"] for item in snapshot["risk_budget"]["breaches"]},
            {
                "cash_buffer_below_minimum",
                "single_position_above_maximum",
                "sector_exposure_above_maximum",
                "portfolio_drawdown_above_maximum",
            },
        )

    def test_risk_budget_keeps_usd_and_cny_in_independent_scopes(self) -> None:
        usd_account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(usd_account_id)
        cny_account = self.repo.create_account(
            name="CNY Account",
            broker="Broker",
            market="cn",
            base_currency="CNY",
        )
        self.repo.replace_positions_lots_and_snapshot(
            account_id=cny_account.id,
            snapshot_date=date(2026, 7, 22),
            cost_method="fifo",
            base_currency="CNY",
            total_cash=900,
            total_market_value=100,
            total_equity=1000,
            unrealized_pnl=0,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": "515880",
                    "market": "cn",
                    "currency": "CNY",
                    "quantity": 100,
                    "avg_cost": 1,
                    "total_cost": 100,
                    "last_price": 1,
                    "market_value_base": 100,
                    "unrealized_pnl_base": 0,
                }
            ],
            lots=[],
            valuation_currency="CNY",
        )
        with self.db.get_session() as session:
            session.add(
                PortfolioDailySnapshot(
                    account_id=cny_account.id,
                    snapshot_date=date(2026, 7, 21),
                    cost_method="fifo",
                    base_currency="CNY",
                    total_cash=900,
                    total_market_value=100,
                    total_equity=1000,
                    unrealized_pnl=0,
                    realized_pnl=0,
                    fee_total=0,
                    tax_total=0,
                    fx_stale=False,
                    payload="{}",
                )
            )
            session.commit()
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "515880",
                "market": "cn",
                "quote_currency": "CNY",
                "instrument_type": "etf",
                "trade_lot_size": 100,
                "verification_status": "verified",
                "evidence_source": "https://www.csindex.com.cn/",
                "evidence_as_of": datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc),
                "metadata": {
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-22",
                        "source": "https://www.csindex.com.cn/",
                        "exposures": [
                            {"sector": "Semiconductors", "weight_pct": 100},
                        ],
                    }
                },
            }
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        self.assertEqual(
            [scope["currency"] for scope in snapshot["risk_budget"]["scopes"]],
            ["CNY", "USD"],
        )
        cny_scope = snapshot["risk_budget"]["scopes"][0]
        self.assertEqual(cny_scope["total_equity"], 1000.0)
        self.assertEqual(cny_scope["cash_buffer_pct"], 90.0)
        self.assertEqual(cny_scope["max_single_position_pct"], 10.0)

    def test_risk_budget_counts_daily_reset_products_as_high_risk(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id, total_equity=1000)
        self.repo.replace_positions_lots_and_snapshot(
            account_id=account_id,
            snapshot_date=date(2026, 7, 22),
            cost_method="fifo",
            base_currency="USD",
            total_cash=900,
            total_market_value=100,
            total_equity=1000,
            unrealized_pnl=0,
            realized_pnl=0,
            fee_total=0,
            tax_total=0,
            fx_stale=False,
            payload="{}",
            positions=[
                {
                    "symbol": "TQQQ",
                    "market": "us",
                    "currency": "USD",
                    "quantity": 1,
                    "avg_cost": 100,
                    "total_cost": 100,
                    "last_price": 100,
                    "market_value_base": 100,
                    "unrealized_pnl_base": 0,
                }
            ],
            lots=[],
            valuation_currency="USD",
        )
        PortfolioInstrumentService(repo=self.repo).create_instrument(
            {
                "symbol": "TQQQ",
                "market": "us",
                "quote_currency": "USD",
                "instrument_type": "daily_leveraged_product",
                "underlying_symbol": "QQQ",
                "underlying_market": "us",
                "underlying_currency": "USD",
                "leverage_factor": 3,
                "daily_reset": True,
                "trade_lot_size": 1,
                "verification_status": "verified",
                "evidence_source": "https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq",
                "evidence_as_of": datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc),
                "metadata": {
                    "risk_sector": {
                        "taxonomy": "portfolio-risk-v1",
                        "as_of": "2026-07-22",
                        "source": "https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq",
                        "exposures": [
                            {"sector": "Technology", "weight_pct": 100},
                        ],
                    }
                },
            }
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertTrue(snapshot["risk_budget"]["evaluated"])
        usd_scope = snapshot["risk_budget"]["scopes"][0]
        self.assertEqual(usd_scope["high_risk_product_pct"], 10.0)
        self.assertIn(
            "high_risk_product_above_maximum",
            {item["code"] for item in snapshot["risk_budget"]["breaches"]},
        )

    def test_risk_budget_fails_closed_when_sector_evidence_is_missing(self) -> None:
        account_id = self._seed_cached_position()
        self._seed_control_plane()
        self._seed_prior_daily_snapshot(account_id)
        PortfolioInstrumentService(repo=self.repo).update_instrument(
            symbol="AAPL",
            market="us",
            payload={"metadata": {"name": "Apple Inc."}},
        )

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertFalse(snapshot["risk_budget"]["evaluated"])
        self.assertIn(
            "risk_sector_evidence_missing",
            {item["code"] for item in snapshot["risk_budget"]["evidence_blockers"]},
        )

    def test_risk_budget_fails_closed_when_drawdown_history_is_incomplete(self) -> None:
        self._seed_cached_position()
        self._seed_control_plane()

        snapshot = self._service().build(cutoff=datetime(2026, 7, 22, 9, 0, 0))

        self.assertFalse(snapshot["risk_budget"]["evaluated"])
        self.assertIn(
            "risk_drawdown_history_insufficient",
            {item["code"] for item in snapshot["risk_budget"]["evidence_blockers"]},
        )


if __name__ == "__main__":
    unittest.main()
