# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_instrument_service import PortfolioInstrumentService
from src.services.portfolio_risk_policy_service import PortfolioRiskPolicyService
from src.storage import (
    DatabaseManager,
    DecisionSignalOutcomeRecord,
    DecisionSignalRecord,
    PortfolioDailySnapshot,
    PortfolioPosition,
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

    def _service(self):
        module_name = "src.services.portfolio_research_snapshot_service"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "portfolio research snapshot service module is required",
        )
        module = importlib.import_module(module_name)
        return module.PortfolioResearchSnapshotService(repo=self.repo)

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
        cutoff = datetime(2026, 7, 22, 9, 0, 0)

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
        self.assertEqual(first["hard_blockers"], [])
        self.assertIn(
            "portfolio_risk_budget_thresholds_not_evaluated",
            first["limitations"],
        )
        rendered = str(first)
        self.assertNotIn("private-owner", rendered)
        self.assertNotIn("Private Broker", rendered)
        self.assertNotIn("Private Account Name", rendered)
        self.assertNotIn("private_note", rendered)

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
