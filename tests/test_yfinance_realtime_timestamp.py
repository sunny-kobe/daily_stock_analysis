from __future__ import annotations

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
