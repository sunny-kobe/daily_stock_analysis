# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import datetime


class _FakePortfolioRepository:
    def __init__(self, identities=None, *, error: Exception | None = None):
        self.identities = list(identities or [])
        self.error = error

    def list_cached_position_identities(self, *, account_id=None):
        if self.error is not None:
            raise self.error
        return list(self.identities)

    def get_cached_positions_updated_at(self, *, account_id=None):
        return datetime(2026, 7, 22, 8, 30, 0)


class PortfolioUniverseServiceTestCase(unittest.TestCase):
    def _service(self, repo):
        module_name = "src.services.portfolio_universe_service"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "portfolio universe service module is required",
        )
        module = importlib.import_module(module_name)
        return module.PortfolioUniverseService(repo=repo)

    def test_resolves_holdings_watchlist_and_union_with_canonical_order(self) -> None:
        service = self._service(
            _FakePortfolioRepository(
                [("us", "aapl"), ("cn", "600519"), ("hk", "hk00700"), ("us", "AAPL")]
            )
        )

        holdings = service.resolve(
            source="portfolio_holdings",
            watchlist=["TSLA", "600519"],
        )
        watchlist = service.resolve(
            source="watchlist",
            watchlist=[" tsla ", "600519", "TSLA"],
        )
        union = service.resolve(
            source="union",
            watchlist=["TSLA", "600519"],
        )

        self.assertEqual(holdings["symbols"], ["600519", "HK00700", "AAPL"])
        self.assertEqual(watchlist["symbols"], ["600519", "TSLA"])
        self.assertEqual(union["symbols"], ["600519", "HK00700", "AAPL", "TSLA"])
        self.assertEqual(holdings["ledger_as_of"], "2026-07-22T08:30:00")
        self.assertEqual(union["coverage"]["deduplicated_count"], 1)

    def test_holdings_mode_never_falls_back_to_watchlist_when_ledger_is_empty(self) -> None:
        service = self._service(_FakePortfolioRepository([]))

        with self.assertRaisesRegex(ValueError, "no non-zero positions"):
            service.resolve(source="portfolio_holdings", watchlist=["AAPL"])

    def test_holdings_mode_fails_closed_when_ledger_read_fails(self) -> None:
        service = self._service(
            _FakePortfolioRepository(error=RuntimeError("database unavailable"))
        )

        with self.assertRaisesRegex(ValueError, "ledger read failed"):
            service.resolve(source="portfolio_holdings", watchlist=["AAPL"])

    def test_invalid_universe_source_is_rejected(self) -> None:
        service = self._service(_FakePortfolioRepository([]))

        with self.assertRaisesRegex(ValueError, "analysis universe source"):
            service.resolve(source="agents", watchlist=["AAPL"])


if __name__ == "__main__":
    unittest.main()
