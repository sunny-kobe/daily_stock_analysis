from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pandas as pd

from data_provider.yfinance_fetcher import YfinanceFetcher


def _history(*, timezone: str | None) -> pd.DataFrame:
    index = pd.DatetimeIndex(["2026-08-05 20:00:00", "2026-08-06 20:00:00"])
    if timezone is not None:
        index = index.tz_localize(timezone)
    return pd.DataFrame(
        {
            "Close": [100.0, 101.0],
            "Open": [99.0, 100.0],
            "High": [101.0, 102.0],
            "Low": [98.0, 99.0],
            "Volume": [1000, 1200],
        },
        index=index,
    )


def _fast_info() -> SimpleNamespace:
    return SimpleNamespace(
        lastPrice=101.0,
        previousClose=100.0,
        open=100.0,
        dayHigh=102.0,
        dayLow=99.0,
        lastVolume=1200,
        marketCap=1000000,
    )


def test_yfinance_realtime_uses_regular_market_time(monkeypatch) -> None:
    ticker = SimpleNamespace(
        fast_info=_fast_info(),
        info={
            "shortName": "Apple",
            "currency": "USD",
            "regularMarketTime": 1786048140,
        },
    )
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda _symbol: ticker))

    quote = YfinanceFetcher().get_realtime_quote("AAPL")

    assert quote is not None
    assert quote.provider_timestamp == "2026-08-06T20:29:00+00:00"


def test_yfinance_realtime_uses_only_timezone_aware_history_index(monkeypatch) -> None:
    class Ticker:
        info = {"shortName": "Apple", "currency": "USD"}

        @property
        def fast_info(self):
            raise RuntimeError("fast_info unavailable")

        def history(self, *, period: str) -> pd.DataFrame:
            assert period == "2d"
            return _history(timezone="America/New_York")

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda _symbol: Ticker()))

    quote = YfinanceFetcher().get_realtime_quote("AAPL")

    assert quote is not None
    assert quote.provider_timestamp == "2026-08-07T00:00:00+00:00"


def test_yfinance_realtime_rejects_naive_history_timestamp(monkeypatch) -> None:
    class Ticker:
        info = {"shortName": "Apple", "currency": "USD"}

        @property
        def fast_info(self):
            raise RuntimeError("fast_info unavailable")

        def history(self, *, period: str) -> pd.DataFrame:
            return _history(timezone=None)

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda _symbol: Ticker()))

    quote = YfinanceFetcher().get_realtime_quote("AAPL")

    assert quote is not None
    assert quote.provider_timestamp is None


def test_yfinance_realtime_accepts_native_hk_index_symbol(monkeypatch) -> None:
    ticker = SimpleNamespace(
        fast_info=_fast_info(),
        info={"shortName": "Hang Seng Index", "currency": "HKD", "regularMarketTime": 1786048140},
    )
    calls: list[str] = []

    def load(symbol: str):
        calls.append(symbol)
        return ticker

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=load))

    quote = YfinanceFetcher().get_realtime_quote("^HSI")

    assert calls == ["^HSI"]
    assert quote is not None
    assert quote.code == "^HSI"
    assert quote.provider_timestamp == "2026-08-06T20:29:00+00:00"


def test_yfinance_realtime_fx_quote_preserves_provider_timestamp(monkeypatch) -> None:
    ticker = SimpleNamespace(
        fast_info=SimpleNamespace(lastPrice=7.20, previousClose=7.18),
        info={"regularMarketTime": 1786048140},
    )
    calls: list[str] = []

    def load(symbol: str):
        calls.append(symbol)
        return ticker

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=load))

    quote = YfinanceFetcher().get_realtime_fx_quote("USD", "CNY")

    assert calls == ["CNY=X"]
    assert quote is not None
    assert quote.code == "USD/CNY"
    assert quote.market == "fx"
    assert quote.currency == "CNY"
    assert quote.price == 7.20
    assert quote.pre_close == 7.18
    assert quote.provider_timestamp == "2026-08-06T20:29:00+00:00"


def test_yfinance_realtime_fx_quote_does_not_use_local_fetch_time(monkeypatch) -> None:
    ticker = SimpleNamespace(
        fast_info=SimpleNamespace(lastPrice=7.20, previousClose=7.18),
        info={},
    )
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda _symbol: ticker))

    quote = YfinanceFetcher().get_realtime_fx_quote("USD", "CNY")

    assert quote is not None
    assert quote.provider_timestamp is None


def test_yfinance_execution_quote_exposes_spread_volume_and_vwap(monkeypatch) -> None:
    index = pd.DatetimeIndex(
        ["2026-08-06 16:28:00", "2026-08-06 16:29:00"],
        tz="America/New_York",
    )
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5],
            "High": [10.2, 11.1],
            "Low": [9.9, 10.4],
            "Close": [10.0, 11.0],
            "Volume": [100, 200],
        },
        index=index,
    )

    class Ticker:
        fast_info = SimpleNamespace(previousClose=9.8)
        info = {
            "shortName": "GraniteShares 2x Long PLTR Daily ETF",
            "currency": "USD",
            "bid": 10.98,
            "ask": 11.02,
            "regularMarketTime": 1786048140,
        }

        def history(self, **kwargs):
            assert kwargs == {
                "period": "1d",
                "interval": "1m",
                "prepost": False,
                "auto_adjust": False,
            }
            return history

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda _symbol: Ticker()))

    quote = YfinanceFetcher().get_realtime_execution_quote("PTIR")

    assert quote is not None
    assert quote.code == "PTIR"
    assert quote.price == 11.0
    assert quote.pre_close == 9.8
    assert quote.bid == 10.98
    assert quote.ask == 11.02
    assert quote.volume == 300
    assert quote.amount == 3200.0
    assert quote.vwap == 10.666666666666666
    assert quote.provider_timestamp == "2026-08-06T20:29:00+00:00"


def test_us_execution_quote_uses_nasdaq_book_with_explicit_yahoo_vwap_provenance(
    monkeypatch,
) -> None:
    index = pd.DatetimeIndex(
        ["2026-08-06 10:28:00", "2026-08-06 10:29:00"],
        tz="America/New_York",
    )
    history = pd.DataFrame(
        {
            "Close": [10.0, 11.0],
            "Volume": [100, 200],
        },
        index=index,
    )

    class Ticker:
        def history(self, **kwargs):
            assert kwargs == {
                "period": "1d",
                "interval": "1m",
                "prepost": False,
                "auto_adjust": False,
            }
            return history

    payloads = {
        "https://api.nasdaq.com/api/autocomplete/slookup/10?search=PTIR": {
            "status": {"rCode": 200},
            "data": [
                {
                    "symbol": "PTIR",
                    "name": "GraniteShares 2x Long PLTR Daily ETF",
                    "asset": "ETF",
                }
            ],
        },
        "https://api.nasdaq.com/api/quote/PTIR/info?assetclass=etf": {
            "status": {"rCode": 200},
            "data": {
                "symbol": "PTIR",
                "assetClass": "ETF",
                "marketStatus": "Open",
                "primaryData": {
                    "lastSalePrice": "$11.00",
                    "lastTradeTimestamp": "Aug 06, 2026 10:29 AM ET",
                    "isRealTime": True,
                    "bidPrice": "$10.98",
                    "askPrice": "$11.02",
                    "volume": "15,000",
                },
            },
        },
        "https://api.nasdaq.com/api/quote/PTIR/summary?assetclass=etf": {
            "status": {"rCode": 200},
            "data": {
                "symbol": "PTIR",
                "assetClass": "ETF",
                "summaryData": {
                    "PreviousClose": {"value": "$9.80"},
                },
            },
        },
    }
    calls: list[str] = []

    class Response:
        def __init__(self, payload: dict):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def load(request, *, timeout: int):
        calls.append(request.full_url)
        assert timeout == 10
        return Response(payloads[request.full_url])

    monkeypatch.setattr("data_provider.yfinance_fetcher.urlopen", load)
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda _symbol: Ticker()))

    quote = YfinanceFetcher().get_realtime_us_execution_quote("PTIR")

    assert calls == list(payloads)
    assert quote is not None
    assert quote.source.value == "nasdaq"
    assert quote.price == 11.0
    assert quote.pre_close == 9.8
    assert quote.bid == 10.98
    assert quote.ask == 11.02
    assert quote.volume == 15000
    assert quote.provider_timestamp == "2026-08-06T14:29:00+00:00"
    assert quote.price_source == "nasdaq"
    assert quote.bid_ask_source == "nasdaq"
    assert quote.volume_source == "nasdaq"
    assert quote.vwap == 10.666666666666666
    assert quote.vwap_source == "yfinance_1m_bars"
    assert quote.vwap_provider_timestamp == "2026-08-06T14:29:00+00:00"
    assert quote.vwap_method == "one_minute_close_volume_weighted"


def test_kr_execution_quote_uses_zero_delay_naver_provider_timestamp(monkeypatch) -> None:
    payload = {
        "itemCode": "000660",
        "stockName": "SK hynix",
        "closePrice": "204,000",
        "compareToPreviousClosePrice": "4,000",
        "fluctuationsRatio": "2.00",
        "marketStatus": "OPEN",
        "localTradedAt": "2026-08-06T15:29:20+09:00",
        "delayTime": 0,
        "stockExchangeType": {
            "code": "KS",
            "zoneId": "Asia/Seoul",
            "delayTime": 0,
            "name": "KOSPI",
        },
    }
    calls: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    def load(request, *, timeout: int):
        calls.append(request.full_url)
        assert timeout == 10
        return Response()

    monkeypatch.setattr("data_provider.yfinance_fetcher.urlopen", load)

    loader = getattr(
        YfinanceFetcher(),
        "get_realtime_kr_execution_quote",
        lambda _symbol: None,
    )
    quote = loader("000660.KS")

    assert calls == ["https://m.stock.naver.com/api/stock/000660/basic"]
    assert quote is not None
    assert quote.code == "000660.KS"
    assert quote.source.value == "naver_finance"
    assert quote.market == "kr"
    assert quote.currency == "KRW"
    assert quote.price == 204000.0
    assert quote.pre_close == 200000.0
    assert quote.provider_timestamp == "2026-08-06T06:29:20+00:00"


def test_kr_execution_quote_rejects_closed_provider_update(monkeypatch) -> None:
    payload = {
        "itemCode": "000660",
        "stockName": "SK hynix",
        "closePrice": "204,000",
        "compareToPreviousClosePrice": "4,000",
        "fluctuationsRatio": "2.00",
        "marketStatus": "CLOSE",
        "localTradedAt": "2026-08-06T16:10:20+09:00",
        "delayTime": 0,
        "stockExchangeType": {
            "code": "KS",
            "zoneId": "Asia/Seoul",
            "delayTime": 0,
            "name": "KOSPI",
        },
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        "data_provider.yfinance_fetcher.urlopen",
        lambda _request, *, timeout: Response(),
    )

    quote = YfinanceFetcher().get_realtime_kr_execution_quote("000660.KS")

    assert quote is None
