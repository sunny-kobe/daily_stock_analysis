# -*- coding: utf-8 -*-
"""Unit tests for src.services.history_loader (Issue #1066)."""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class HistoryLoaderTestCase(unittest.TestCase):
    """Tests for load_history_df and frozen target date ContextVar."""

    # ------------------------------------------------------------------
    # ContextVar lifecycle
    # ------------------------------------------------------------------
    def test_frozen_target_date_lifecycle(self):
        from src.services.history_loader import (
            get_frozen_target_date,
            reset_frozen_target_date,
            set_frozen_target_date,
        )

        self.assertIsNone(get_frozen_target_date())
        d = date(2026, 4, 18)
        token = set_frozen_target_date(d)
        self.assertEqual(get_frozen_target_date(), d)
        reset_frozen_target_date(token)
        self.assertIsNone(get_frozen_target_date())

    def test_frozen_market_evidence_reads_exact_batch_without_fallback(self):
        from src.services.history_loader import (
            load_history_df,
            reset_frozen_market_evidence,
            set_frozen_market_evidence,
        )

        batch_hash = "b" * 64
        rows = []
        for day, close in ((date(2026, 7, 30), 100.0), (date(2026, 7, 31), 101.0)):
            row = MagicMock()
            row.date = day
            row.to_dict.return_value = {
                "code": "AAPL",
                "date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1.0,
            }
            rows.append(row)
        repository = MagicMock()
        repository.get_batch.return_value = SimpleNamespace(
            batch_hash=batch_hash,
            code="AAPL",
            rows=tuple(rows),
        )

        token = set_frozen_market_evidence(code="AAPL", batch_hash=batch_hash)
        try:
            with patch(
                "src.repositories.portfolio_market_evidence_repo.PortfolioMarketEvidenceRepository",
                return_value=repository,
            ), patch("src.storage.get_db") as get_db, patch(
                "src.services.history_loader._get_fetcher_manager"
            ) as get_fetcher:
                frame, source = load_history_df(
                    "AAPL",
                    days=60,
                    target_date=date(2026, 7, 31),
                )
        finally:
            reset_frozen_market_evidence(token)

        self.assertEqual(source, "portfolio_market_evidence")
        self.assertEqual(frame["close"].tolist(), [100.0, 101.0])
        repository.get_batch.assert_called_once_with(batch_hash)
        get_db.assert_not_called()
        get_fetcher.assert_not_called()

    def test_bound_market_evidence_prefers_explicit_history_batch(self):
        from src.services.history_loader import get_bound_market_evidence_identity

        context = {
            "_frozen_research_snapshot": {
                "schema_version": "portfolio-research-snapshot-v1",
                "snapshot_hash": "a" * 64,
                "cutoff": "2026-08-14T02:00:00Z",
                "positions": [
                    {
                        "symbol": "600519",
                        "history_evidence_batch_hash": "c" * 64,
                        "price_evidence_batch_hash": "b" * 64,
                    }
                ],
            }
        }

        identity = get_bound_market_evidence_identity(context, "600519")

        self.assertIsNotNone(identity)
        self.assertEqual(identity.batch_hash, "c" * 64)

    def test_bound_market_evidence_accepts_legacy_price_batch(self):
        from src.services.history_loader import get_bound_market_evidence_identity

        context = {
            "_frozen_research_snapshot": {
                "schema_version": "portfolio-research-snapshot-v1",
                "snapshot_hash": "a" * 64,
                "cutoff": "2026-08-14T02:00:00Z",
                "positions": [
                    {
                        "symbol": "600519",
                        "price_evidence_batch_hash": "b" * 64,
                    }
                ],
            }
        }

        identity = get_bound_market_evidence_identity(context, "600519")

        self.assertIsNotNone(identity)
        self.assertEqual(identity.batch_hash, "b" * 64)

    # ------------------------------------------------------------------
    # DB hit path
    # ------------------------------------------------------------------
    @patch("src.storage.get_db")
    def test_returns_db_data_when_sufficient(self, mock_get_db):
        from src.services.history_loader import load_history_df

        fake_bar = MagicMock()
        fake_bar.to_dict.return_value = {
            "date": "2026-04-18",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        }
        mock_db = MagicMock()
        mock_db.get_data_range.return_value = [fake_bar] * 40
        mock_get_db.return_value = mock_db

        df, source = load_history_df("600519", days=60, target_date=date(2026, 4, 18))

        self.assertIsNotNone(df)
        self.assertEqual(source, "db_cache")
        self.assertEqual(len(df), 40)
        mock_db.get_data_range.assert_called_once()

    # ------------------------------------------------------------------
    # DB miss → DFM fallback
    # ------------------------------------------------------------------
    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_falls_back_to_dfm_when_db_empty(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        fake_df = pd.DataFrame(
            {
                "date": ["2026-04-16", "2026-04-17", "2026-04-18"],
                "close": [1, 2, 3],
            }
        )
        mock_fm = MagicMock()
        mock_fm.get_daily_data.return_value = (fake_df, "eastmoney")
        mock_get_fm.return_value = mock_fm

        df, source = load_history_df("600519", days=60, target_date=date(2026, 4, 18))

        self.assertIsNotNone(df)
        self.assertEqual(source, "eastmoney")
        self.assertEqual(len(df), 3)
        self.assertEqual(df.iloc[-1]["date"], "2026-04-18")
        mock_fm.get_daily_data.assert_called_once_with("600519", days=60)

    # ------------------------------------------------------------------
    # ContextVar integration
    # ------------------------------------------------------------------
    @patch("src.storage.get_db")
    def test_uses_frozen_target_date_from_contextvar(self, mock_get_db):
        from src.services.history_loader import (
            load_history_df,
            reset_frozen_target_date,
            set_frozen_target_date,
        )

        frozen_date = date(2026, 4, 15)
        token = set_frozen_target_date(frozen_date)
        try:
            mock_db = MagicMock()
            fake_bar = MagicMock()
            fake_bar.to_dict.return_value = {"date": "2026-04-15", "close": 10}
            mock_db.get_data_range.return_value = [fake_bar] * 30
            mock_get_db.return_value = mock_db

            df, source = load_history_df("600519", days=30)

            self.assertEqual(source, "db_cache")
            call_args = mock_db.get_data_range.call_args
            _code, _start, end = call_args[0]
            self.assertEqual(end, frozen_date)
        finally:
            reset_frozen_target_date(token)

    # ------------------------------------------------------------------
    # normalize_stock_code fallback for prefixed codes
    # ------------------------------------------------------------------
    @patch("src.storage.get_db")
    def test_uses_normalize_fallback_for_prefixed_code(self, mock_get_db):
        from src.services.history_loader import load_history_df

        fake_bar = MagicMock()
        fake_bar.to_dict.return_value = {"date": "2026-04-18", "close": 10}

        mock_db = MagicMock()

        def side_effect(code, start, end):
            if code == "SH600519":
                return []
            return [fake_bar] * 30

        mock_db.get_data_range.side_effect = side_effect
        mock_get_db.return_value = mock_db

        df, source = load_history_df("SH600519", days=30, target_date=date(2026, 4, 18))

        self.assertEqual(source, "db_cache")
        self.assertEqual(mock_db.get_data_range.call_count, 2)

    # ------------------------------------------------------------------
    # Both paths fail gracefully
    # ------------------------------------------------------------------
    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_graceful_when_both_fail(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.side_effect = Exception("DB down")
        mock_get_db.return_value = mock_db

        mock_fm = MagicMock()
        mock_fm.get_daily_data.side_effect = Exception("API down")
        mock_get_fm.return_value = mock_fm

        df, source = load_history_df("600519", days=60, target_date=date(2026, 4, 18))

        self.assertIsNone(df)
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
