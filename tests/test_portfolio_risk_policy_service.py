# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import importlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import DatabaseManager


class PortfolioRiskPolicyServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "risk_policy_service.db"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _service(self):
        module_name = "src.services.portfolio_risk_policy_service"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "portfolio risk policy service module is required",
        )
        module = importlib.import_module(module_name)
        return module.PortfolioRiskPolicyService(
            repo=PortfolioRepository(db_manager=self.db)
        )

    def test_first_save_requires_complete_explicit_policy(self) -> None:
        service = self._service()
        self.assertIsNone(service.get_policy())

        with self.assertRaisesRegex(ValueError, "max_sector_pct"):
            service.save_policy(
                {
                    "min_cash_buffer_pct": 10,
                    "max_single_position_pct": 20,
                }
            )

    def test_policy_percentages_must_stay_within_bounds(self) -> None:
        payload = {
            "min_cash_buffer_pct": 10,
            "max_single_position_pct": 20,
            "max_sector_pct": 35,
            "max_high_risk_product_pct": 5,
            "max_portfolio_drawdown_pct": 15,
        }

        with self.assertRaisesRegex(ValueError, "max_single_position_pct"):
            self._service().save_policy({**payload, "max_single_position_pct": 101})

    def test_partial_update_preserves_unspecified_risk_limits(self) -> None:
        service = self._service()
        created = service.save_policy(
            {
                "min_cash_buffer_pct": 10,
                "max_single_position_pct": 20,
                "max_sector_pct": 35,
                "max_high_risk_product_pct": 5,
                "max_portfolio_drawdown_pct": 15,
            }
        )

        updated = service.save_policy({"max_single_position_pct": 18})

        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["max_single_position_pct"], 18.0)
        self.assertEqual(updated["max_sector_pct"], 35.0)


if __name__ == "__main__":
    unittest.main()
