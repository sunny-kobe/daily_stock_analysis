# -*- coding: utf-8 -*-
"""
Regression tests for HK stock name fallback when stock_hk_spot_em fails.

Covers: data_provider/akshare_fetcher.py _get_hk_realtime_quote
"""

import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()
try:
    import json_repair  # noqa: F401
except ImportError:
    if "json_repair" not in sys.modules:
        sys.modules["json_repair"] = MagicMock()

from data_provider import akshare_fetcher as akshare_fetcher_module
from data_provider.akshare_fetcher import AkshareFetcher, RealtimeSource


class _DummyCircuitBreaker:
    def __init__(self):
        self.failures = []
        self.successes = []

    def is_available(self, source: str) -> bool:
        return True

    def record_success(self, source: str) -> None:
        self.successes.append(source)

    def record_failure(self, source: str, error=None) -> None:
        self.failures.append((source, error))


def _make_spot_em_df():
    """Simulate stock_hk_spot_em() return value."""
    return pd.DataFrame([{
        '代码': '00700',
        '名称': '腾讯控股',
        '最新价': 370.0,
        '涨跌幅': 1.5,
        '涨跌额': 5.5,
        '成交量': 10000,
        '成交额': 3700000.0,
        '量比': 1.2,
        '换手率': 0.3,
        '振幅': 2.0,
        '市盈率': 20.0,
        '市净率': 3.5,
        '总市值': 3.5e12,
        '流通市值': 3.5e12,
        '52周最高': 400.0,
        '52周最低': 280.0,
    }])


def _make_spot_df():
    """Simulate stock_hk_spot() return value (sina source)."""
    return pd.DataFrame([{
        '代码': '00700',
        '名称': '腾讯控股',
        '最新价': 368.0,
        '涨跌额': 3.5,
        '涨跌幅': 0.96,
        '买入': 367.8,
        '卖出': 368.2,
        '昨收': 364.5,
        '今开': 365.0,
        '最高': 370.0,
        '最低': 364.0,
        '成交量': 9800,
        '成交额': 3606400.0,
    }])


class TestHKRealtimeFallback(unittest.TestCase):
    """stock_hk_spot_em 失败时应 fallback 到 stock_hk_spot。"""

    def setUp(self):
        self.fetcher = AkshareFetcher()
        akshare_fetcher_module._hk_realtime_cache.update({
            "data": None,
            "timestamp": 0,
            "ttl": 1200,
            "failure_ttl": 30,
            "last_result": None,
        })
        # Bypass rate limiting
        self.fetcher._enforce_rate_limit = lambda: None
        self.fetcher._set_random_user_agent = lambda: None

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_em_success_returns_quote_with_name(self, mock_cb):
        """腾讯失败后，stock_hk_spot_em 成功时返回含名称的 quote。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.return_value = _make_spot_em_df()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ):
            quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.name, "腾讯控股")
        self.assertAlmostEqual(quote.price, 370.0)

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_repeated_em_lookup_uses_hot_cache_without_network_delay(self, mock_cb):
        """首次拉取后，TTL 内查询不应再次调用接口或执行限速等待。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.return_value = _make_spot_em_df()
        self.fetcher._set_random_user_agent = MagicMock()
        self.fetcher._enforce_rate_limit = MagicMock()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ):
            first_quote = self.fetcher._get_hk_realtime_quote("HK00700")
            cached_quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNotNone(first_quote)
        self.assertIsNotNone(cached_quote)
        ak_mock.stock_hk_spot_em.assert_called_once()
        self.fetcher._set_random_user_agent.assert_called_once()
        self.fetcher._enforce_rate_limit.assert_called_once()

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_parallel_cold_lookups_share_one_em_request(self, mock_cb):
        """组合快照并发冷查询时，全市场主接口只能由一个线程刷新。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        codes = ["00700", "09988", "03690", "01810"]
        em_df = pd.concat(
            [_make_spot_em_df().assign(代码=code) for code in codes],
            ignore_index=True,
        )
        ak_mock = MagicMock()

        def delayed_em_response():
            time.sleep(0.05)
            return em_df

        ak_mock.stock_hk_spot_em.side_effect = delayed_em_response
        start = threading.Barrier(len(codes))

        def fetch(code: str):
            fetcher = AkshareFetcher()
            fetcher._set_random_user_agent = lambda: None
            fetcher._enforce_rate_limit = lambda: None
            start.wait(timeout=2)
            with patch.object(fetcher, "_get_stock_realtime_quote_tencent", return_value=None):
                return fetcher._get_hk_realtime_quote(f"HK{code}")

        with patch.dict(sys.modules, {"akshare": ak_mock}):
            with ThreadPoolExecutor(max_workers=len(codes)) as executor:
                quotes = list(executor.map(fetch, codes))

        self.assertTrue(all(quote is not None for quote in quotes))
        ak_mock.stock_hk_spot_em.assert_called_once()
        ak_mock.stock_hk_spot.assert_not_called()

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_em_failure_falls_back_to_spot(self, mock_cb):
        """腾讯和 stock_hk_spot_em 失败后应 fallback 到 stock_hk_spot。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.side_effect = Exception("接口异常：数据源不可用")
        ak_mock.stock_hk_spot.return_value = _make_spot_df()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ):
            quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.name, "腾讯控股")
        self.assertAlmostEqual(quote.price, 368.0)
        ak_mock.stock_hk_spot.assert_called_once()

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_parallel_em_failure_reuses_negative_cache_before_fallback(self, mock_cb):
        """并发冷启动失败时应只尝试一次主接口，其余线程复用失败结果后走备用链路。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        codes = ["00700", "09988", "03690", "01810"]
        ak_mock = MagicMock()

        def delayed_em_failure():
            time.sleep(0.05)
            raise Exception("东方财富接口超时")

        ak_mock.stock_hk_spot_em.side_effect = delayed_em_failure
        ak_mock.stock_hk_spot.return_value = pd.concat(
            [_make_spot_df().assign(代码=code) for code in codes],
            ignore_index=True,
        )
        start = threading.Barrier(len(codes))

        def fetch(code: str):
            fetcher = AkshareFetcher()
            fetcher._set_random_user_agent = lambda: None
            fetcher._enforce_rate_limit = lambda: None
            start.wait(timeout=2)
            with patch.object(fetcher, "_get_stock_realtime_quote_tencent", return_value=None):
                return fetcher._get_hk_realtime_quote(f"HK{code}")

        with patch.dict(sys.modules, {"akshare": ak_mock}):
            with ThreadPoolExecutor(max_workers=len(codes)) as executor:
                quotes = list(executor.map(fetch, codes))

        self.assertTrue(all(quote is not None for quote in quotes))
        ak_mock.stock_hk_spot_em.assert_called_once()
        self.assertEqual(ak_mock.stock_hk_spot.call_count, len(codes))
        self.assertEqual(akshare_fetcher_module._hk_realtime_cache["last_result"], "failure")

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_all_sources_fail_returns_none(self, mock_cb):
        """东方财富、新浪和腾讯都失败时返回 None，不抛异常。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.side_effect = Exception("东方财富接口超时")
        ak_mock.stock_hk_spot.side_effect = Exception("新浪接口超时")

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ) as tencent_quote:
            quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNone(quote)
        tencent_quote.assert_called_once_with("HK00700")

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_akshare_failures_fall_back_to_tencent_hk_quote(self, mock_cb):
        mock_cb.return_value = _DummyCircuitBreaker()
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.side_effect = Exception("东方财富接口超时")
        ak_mock.stock_hk_spot.side_effect = Exception("新浪接口超时")
        fields = ["0"] * 50
        fields[1] = "XL二南方海力士"
        fields[2] = "07709"
        fields[3] = "56.560"
        fields[4] = "71.780"
        fields[5] = "61.020"
        fields[6] = "192324651"
        fields[30] = "2026/07/16 16:00:02"
        fields[31] = "-15.220"
        fields[32] = "-21.20"
        fields[33] = "61.680"
        fields[34] = "55.100"
        fields[35] = "56.540/192324651.0/11222281220.880"
        fields[38] = "9.17"
        fields[44] = "524.5940"
        fields[45] = "524.5940"
        fields[49] = "0.78"
        response = MagicMock(
            status_code=200,
            text=f'v_hk07709="{"~".join(fields)}";',
        )

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch(
            "data_provider.akshare_fetcher.requests.get",
            return_value=response,
        ) as request_get:
            quote = self.fetcher._get_hk_realtime_quote("HK07709")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, RealtimeSource.TENCENT)
        self.assertEqual(quote.name, "XL二南方海力士")
        self.assertAlmostEqual(quote.price, 56.56)
        self.assertAlmostEqual(quote.change_pct, -21.2)
        self.assertAlmostEqual(quote.pre_close, 71.78)
        request_get.assert_called_once()
        self.assertIn("q=r_hk07709", request_get.call_args.args[0])

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_em_returns_empty_df_falls_back_to_spot(self, mock_cb):
        """腾讯失败且 stock_hk_spot_em 为空时应 fallback 到 stock_hk_spot。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.return_value = pd.DataFrame(columns=['代码', '名称', '最新价'])
        ak_mock.stock_hk_spot.return_value = _make_spot_df()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ):
            quote = self.fetcher._get_hk_realtime_quote("HK00700")
            cached_fallback_quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNotNone(quote)
        self.assertIsNotNone(cached_fallback_quote)
        self.assertEqual(quote.name, "腾讯控股")
        self.assertEqual(cached_fallback_quote.name, "腾讯控股")
        self.assertIsNone(akshare_fetcher_module._hk_realtime_cache["data"])
        self.assertEqual(akshare_fetcher_module._hk_realtime_cache["last_result"], "failure")
        ak_mock.stock_hk_spot_em.assert_called_once()
        self.assertEqual(ak_mock.stock_hk_spot.call_count, 2)

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_em_missing_code_column_falls_back_without_poisoning_cache(self, mock_cb):
        """主接口结构异常时应保留 fallback，且异常结果不得写入缓存。"""
        cb = _DummyCircuitBreaker()
        mock_cb.return_value = cb
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot_em.return_value = pd.DataFrame([{
            "名称": "腾讯控股",
            "最新价": 370.0,
        }])
        ak_mock.stock_hk_spot.return_value = _make_spot_df()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ):
            quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote.price, 368.0)
        ak_mock.stock_hk_spot.assert_called_once()
        self.assertIsNone(akshare_fetcher_module._hk_realtime_cache["data"])
        self.assertTrue(any(source == "akshare_hk_em" for source, _ in cb.failures))

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_malformed_hot_cache_falls_back_without_refetching_em(self, mock_cb):
        """缓存解析失败时应跳过昂贵的主接口重拉，并使用备用接口。"""
        mock_cb.return_value = _DummyCircuitBreaker()
        akshare_fetcher_module._hk_realtime_cache.update({
            "data": pd.DataFrame([{"名称": "腾讯控股"}]),
            "timestamp": akshare_fetcher_module.time.time(),
        })
        ak_mock = MagicMock()
        ak_mock.stock_hk_spot.return_value = _make_spot_df()
        self.fetcher._set_random_user_agent = MagicMock()
        self.fetcher._enforce_rate_limit = MagicMock()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ):
            quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote.price, 368.0)
        ak_mock.stock_hk_spot_em.assert_not_called()
        ak_mock.stock_hk_spot.assert_called_once()
        self.fetcher._set_random_user_agent.assert_called_once()
        self.fetcher._enforce_rate_limit.assert_called_once()

    @patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker")
    def test_circuit_breaker_open_returns_none(self, mock_cb):
        """东方财富/新浪熔断时仍尝试腾讯，腾讯失败则返回 None。"""
        cb = _DummyCircuitBreaker()
        cb.is_available = lambda source: False
        mock_cb.return_value = cb
        ak_mock = MagicMock()

        with patch.dict(sys.modules, {"akshare": ak_mock}), patch.object(
            self.fetcher,
            "_get_stock_realtime_quote_tencent",
            return_value=None,
        ) as tencent_quote:
            quote = self.fetcher._get_hk_realtime_quote("HK00700")

        self.assertIsNone(quote)
        ak_mock.stock_hk_spot_em.assert_not_called()
        tencent_quote.assert_called_once_with("HK00700")


if __name__ == "__main__":
    unittest.main()
