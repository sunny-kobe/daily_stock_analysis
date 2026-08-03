# -*- coding: utf-8 -*-
"""Regression tests for pipeline data-fetch error handling."""

from datetime import date, datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.core.pipeline import StockAnalysisPipeline
from src.services.history_loader import is_cache_read_only


def _frozen_portfolio_context() -> dict:
    return {
        "_frozen_research_snapshot": {
            "schema_version": "portfolio-research-snapshot-v1",
            "snapshot_hash": "a" * 64,
            "cutoff": "2026-08-03T02:00:00Z",
        },
    }


class PipelineFetchErrorTestCase(unittest.TestCase):
    """`fetch_and_save_stock_data` should preserve the original exception."""

    def test_fetch_and_save_handles_stock_name_lookup_failure(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        pipeline.fetcher_manager.get_stock_name.side_effect = RuntimeError("name lookup failed")

        success, error = StockAnalysisPipeline.fetch_and_save_stock_data(pipeline, "600519")

        self.assertFalse(success)
        self.assertIn("name lookup failed", error or "")

    @patch.object(
        StockAnalysisPipeline,
        "_resolve_resume_target_date",
        return_value=date(2026, 3, 27),
    )
    def test_fetch_and_save_uses_effective_trading_date_for_resume_check(self, _mock_target):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        pipeline.fetcher_manager.get_stock_name.return_value = "贵州茅台"
        pipeline.db.has_today_data.return_value = True
        current_time = datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)

        success, error = StockAnalysisPipeline.fetch_and_save_stock_data(
            pipeline,
            "600519",
            current_time=current_time,
        )

        self.assertTrue(success)
        self.assertIsNone(error)
        _mock_target.assert_called_once_with("600519", current_time=current_time)
        pipeline.db.has_today_data.assert_called_once_with("600519", date(2026, 3, 27))
        pipeline.fetcher_manager.get_daily_data.assert_not_called()

    def test_bound_research_snapshot_uses_only_prepared_daily_data(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = _frozen_portfolio_context()
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        pipeline.fetcher_manager.get_stock_name.return_value = "贵州茅台"
        pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 8, 3))
        pipeline.db.has_today_data.return_value = False

        success, error = pipeline.fetch_and_save_stock_data("600519")

        self.assertFalse(success)
        self.assertIn("冻结研究快照", error or "")
        pipeline.db.has_today_data.assert_called_once_with("600519", date(2026, 8, 3))
        pipeline.fetcher_manager.get_stock_name.assert_not_called()
        pipeline.fetcher_manager.get_daily_data.assert_not_called()
        pipeline.db.save_daily_data.assert_not_called()

    def test_malformed_research_snapshot_does_not_disable_legacy_cache_write(self):
        malformed_envelopes = (
            {"snapshot_hash": "a" * 64, "cutoff": "2026-08-03T02:00:00Z"},
            {
                "schema_version": "unknown-v1",
                "snapshot_hash": "a" * 64,
                "cutoff": "2026-08-03T02:00:00Z",
            },
            {
                "schema_version": "portfolio-research-snapshot-v1",
                "snapshot_hash": "not-a-hash",
                "cutoff": "2026-08-03T02:00:00Z",
            },
            {
                "schema_version": "portfolio-research-snapshot-v1",
                "snapshot_hash": "a" * 64,
                "cutoff": "not-a-cutoff",
            },
        )
        daily_df = pd.DataFrame([{"date": date(2026, 8, 3), "close": 10.0}])

        for envelope in malformed_envelopes:
            with self.subTest(envelope=envelope):
                pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
                pipeline.portfolio_context = {"_frozen_research_snapshot": envelope}
                pipeline.fetcher_manager = MagicMock()
                pipeline.db = MagicMock()
                pipeline.fetcher_manager.get_stock_name.return_value = "贵州茅台"
                pipeline.fetcher_manager.get_daily_data.return_value = (daily_df, "Fetcher")
                pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 8, 3))
                pipeline.db.has_today_data.return_value = False
                pipeline.db.save_daily_data.return_value = 1

                success, error = pipeline.fetch_and_save_stock_data("600519")

                self.assertTrue(success)
                self.assertIsNone(error)
                pipeline.fetcher_manager.get_daily_data.assert_called_once_with("600519", days=30)
                pipeline.db.save_daily_data.assert_called_once_with(daily_df, "600519", "Fetcher")

    def test_process_single_stock_resets_cache_read_only_after_analysis_error(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = _frozen_portfolio_context()
        pipeline.query_id = None
        pipeline.trace_id = None
        pipeline.query_source = "api"
        pipeline._emit_progress = MagicMock()
        pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 8, 3))
        pipeline.fetch_and_save_stock_data = MagicMock(return_value=(True, None))
        observed = []

        def fail_analysis(*_args, **_kwargs):
            observed.append(is_cache_read_only())
            raise RuntimeError("analysis failed")

        pipeline.analyze_stock = MagicMock(side_effect=fail_analysis)

        result = pipeline.process_single_stock(
            "600519",
            analysis_query_id="q-cache-read-only",
        )

        self.assertIsNone(result)
        self.assertEqual(observed, [True])
        self.assertFalse(is_cache_read_only())

    def test_resolve_resume_target_date_normalizes_supported_a_share_formats(self):
        with patch("src.core.pipeline.get_market_for_stock", return_value="cn") as mock_market, patch(
            "src.core.pipeline.get_effective_trading_date",
            return_value=date(2026, 3, 27),
        ) as mock_target:
            for code in ("SH600519", "000001.SZ", "BJ920748"):
                result = StockAnalysisPipeline._resolve_resume_target_date(code)
                self.assertEqual(result, date(2026, 3, 27))

        self.assertEqual(
            [args.args[0] for args in mock_market.call_args_list],
            ["600519", "000001", "920748"],
        )
        self.assertEqual(mock_target.call_count, 3)


if __name__ == "__main__":
    unittest.main()
