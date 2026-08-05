# -*- coding: utf-8 -*-
"""Regression tests for pipeline data-fetch error handling."""

from datetime import date, datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.core.pipeline import StockAnalysisPipeline
from src.services.history_loader import (
    is_cache_read_only,
    reset_cache_read_only,
    set_cache_read_only,
    reset_frozen_market_evidence,
    set_frozen_market_evidence,
)


def _frozen_portfolio_context(
    cutoff: str = "2026-08-03T02:00:00Z",
) -> dict:
    return {
        "_frozen_research_snapshot": {
            "schema_version": "portfolio-research-snapshot-v1",
            "snapshot_hash": "a" * 64,
            "cutoff": cutoff,
            "positions": [
                {
                    "account_id": 1,
                    "symbol": "600519",
                    "price_evidence_batch_hash": "b" * 64,
                }
            ],
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
        batch = MagicMock(
            code="600519",
            batch_hash="b" * 64,
            rows=tuple(MagicMock() for _ in range(20)),
        )

        with patch(
            "src.repositories.portfolio_market_evidence_repo.PortfolioMarketEvidenceRepository"
        ) as repository_type:
            repository_type.return_value.get_batch.return_value = batch
            success, error = pipeline.fetch_and_save_stock_data("600519")

        self.assertTrue(success)
        self.assertIsNone(error)
        repository_type.return_value.get_batch.assert_called_once_with("b" * 64)
        pipeline.db.has_today_data.assert_not_called()
        pipeline.fetcher_manager.get_stock_name.assert_not_called()
        pipeline.fetcher_manager.get_daily_data.assert_not_called()
        pipeline.db.save_daily_data.assert_not_called()

    def test_bound_research_snapshot_rejects_short_evidence_batch(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = _frozen_portfolio_context()
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        batch = MagicMock(
            code="600519",
            batch_hash="b" * 64,
            rows=tuple(MagicMock() for _ in range(19)),
        )

        with patch(
            "src.repositories.portfolio_market_evidence_repo.PortfolioMarketEvidenceRepository"
        ) as repository_type:
            repository_type.return_value.get_batch.return_value = batch
            success, error = pipeline.fetch_and_save_stock_data("600519")

        self.assertFalse(success)
        self.assertEqual(error, "冻结研究快照绑定的行情数据不足")
        pipeline.fetcher_manager.get_daily_data.assert_not_called()

    def test_bound_trend_history_uses_exact_evidence_loader(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.db = MagicMock()
        frame = pd.DataFrame(
            [{"date": date(2026, 7, 31), "close": 101.0}]
        )
        token = set_frozen_market_evidence(code="AAPL", batch_hash="b" * 64)
        try:
            with patch(
                "src.services.history_loader.load_history_df",
                return_value=(frame, "portfolio_market_evidence"),
            ) as loader:
                result = pipeline._load_trend_history(
                    "AAPL",
                    start_date=date(2026, 6, 1),
                    end_date=date(2026, 7, 31),
                )
        finally:
            reset_frozen_market_evidence(token)

        self.assertEqual(result["close"].tolist(), [101.0])
        loader.assert_called_once_with("AAPL", days=60, target_date=date(2026, 7, 31))
        pipeline.db.get_data_range.assert_not_called()

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

    def test_process_single_stock_resets_cache_read_only_when_diagnostics_setup_fails(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = _frozen_portfolio_context()
        pipeline.query_id = None
        pipeline.trace_id = None
        pipeline.query_source = "api"
        pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 8, 3))
        pipeline.fetch_and_save_stock_data = MagicMock(return_value=(True, None))
        pipeline.analyze_stock = MagicMock()
        leaked = None
        cleanup_token = set_cache_read_only(False)

        try:
            with patch(
                "src.core.pipeline.get_current_diagnostic_context",
                return_value=None,
            ), patch(
                "src.core.pipeline.activate_run_diagnostic_context",
                side_effect=RuntimeError("diagnostics unavailable"),
            ):
                result = pipeline.process_single_stock("600519")
            leaked = is_cache_read_only()
        finally:
            reset_cache_read_only(cleanup_token)

        self.assertIsNone(result)
        self.assertFalse(leaked)
        pipeline.fetch_and_save_stock_data.assert_not_called()
        pipeline.analyze_stock.assert_not_called()

    def test_bound_research_snapshot_stops_when_prepared_daily_data_is_missing(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = _frozen_portfolio_context()
        pipeline.query_id = None
        pipeline.trace_id = None
        pipeline.query_source = "api"
        pipeline._emit_progress = MagicMock()
        pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 8, 3))
        pipeline.fetch_and_save_stock_data = MagicMock(
            return_value=(False, "冻结研究快照绑定的行情数据不足")
        )
        pipeline.analyze_stock = MagicMock()

        result = pipeline.process_single_stock("600519")

        self.assertIsNone(result)
        pipeline.analyze_stock.assert_not_called()
        self.assertFalse(is_cache_read_only())

    def test_unbound_analysis_continues_with_existing_data_after_fetch_failure(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = None
        pipeline.query_id = None
        pipeline.trace_id = None
        pipeline.query_source = "api"
        pipeline._emit_progress = MagicMock()
        pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 8, 3))
        pipeline.fetch_and_save_stock_data = MagicMock(
            return_value=(False, "provider unavailable")
        )
        expected = MagicMock(success=True, operation_advice="持有", sentiment_score=60)
        pipeline.analyze_stock = MagicMock(return_value=expected)

        result = pipeline.process_single_stock("600519")

        self.assertIs(result, expected)
        pipeline.analyze_stock.assert_called_once()
        self.assertFalse(is_cache_read_only())

    def test_bound_research_snapshot_uses_cutoff_as_process_target_time(self):
        cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        friday = date(2026, 7, 31)
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.portfolio_context = _frozen_portfolio_context("2026-07-31T08:00:00Z")
        pipeline.query_id = None
        pipeline.trace_id = None
        pipeline.query_source = "api"
        pipeline._emit_progress = MagicMock()
        pipeline._resolve_resume_target_date = MagicMock(return_value=friday)
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        expected = MagicMock(success=True, operation_advice="持有", sentiment_score=60)
        pipeline.analyze_stock = MagicMock(return_value=expected)

        batch = MagicMock(
            code="600519",
            batch_hash="b" * 64,
            rows=tuple(MagicMock() for _ in range(20)),
        )
        with patch(
            "src.repositories.portfolio_market_evidence_repo.PortfolioMarketEvidenceRepository"
        ) as repository_type:
            repository_type.return_value.get_batch.return_value = batch
            result = pipeline.process_single_stock("600519")

        self.assertIs(result, expected)
        self.assertEqual(
            pipeline._resolve_resume_target_date.call_args_list,
            [
                unittest.mock.call("600519", current_time=cutoff),
            ],
        )
        pipeline.db.has_today_data.assert_not_called()
        pipeline.fetcher_manager.get_daily_data.assert_not_called()
        pipeline.analyze_stock.assert_called_once_with(
            "600519",
            unittest.mock.ANY,
            query_id=unittest.mock.ANY,
            current_time=cutoff,
        )

    def test_unbound_and_malformed_contexts_keep_explicit_worker_time(self):
        worker_time = datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)
        malformed = _frozen_portfolio_context()
        malformed["_frozen_research_snapshot"]["snapshot_hash"] = "not-a-hash"

        for portfolio_context in (None, malformed):
            with self.subTest(portfolio_context=portfolio_context):
                pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
                pipeline.portfolio_context = portfolio_context
                pipeline.query_id = None
                pipeline.trace_id = None
                pipeline.query_source = "api"
                pipeline._emit_progress = MagicMock()
                pipeline._resolve_resume_target_date = MagicMock(
                    return_value=date(2026, 8, 3)
                )
                pipeline.fetch_and_save_stock_data = MagicMock(return_value=(True, None))
                expected = MagicMock(
                    success=True,
                    operation_advice="持有",
                    sentiment_score=60,
                )
                pipeline.analyze_stock = MagicMock(return_value=expected)

                result = pipeline.process_single_stock(
                    "600519",
                    current_time=worker_time,
                )

                self.assertIs(result, expected)
                pipeline._resolve_resume_target_date.assert_called_once_with(
                    "600519",
                    current_time=worker_time,
                )
                pipeline.fetch_and_save_stock_data.assert_called_once_with(
                    "600519",
                    current_time=worker_time,
                )
                pipeline.analyze_stock.assert_called_once_with(
                    "600519",
                    unittest.mock.ANY,
                    query_id=unittest.mock.ANY,
                    current_time=worker_time,
                )

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
