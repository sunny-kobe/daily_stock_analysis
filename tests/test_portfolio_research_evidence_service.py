# -*- coding: utf-8 -*-
"""Focused tests for bounded current portfolio evidence preparation."""

from __future__ import annotations

import os
import math
import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import func, select

from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.tencent_fetcher import TencentFetcher
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.portfolio_research_evidence_service import (
    PortfolioResearchEvidenceService,
)
from src.services.portfolio_service import PortfolioService
from src.storage import (
    DatabaseManager,
    DecisionSignalRecord,
    PortfolioCashLedger,
    PortfolioDailySnapshot,
    PortfolioFxRate,
    PortfolioMarketEvidenceBar,
    PortfolioPosition,
    PortfolioRiskPolicy,
    PortfolioStrategyTransitionRecord,
    PortfolioStrategyValidationRunRecord,
    PortfolioStrategyVersionRecord,
    PortfolioTrade,
    StockDaily,
)


AS_OF = date(2026, 7, 31)


def _product_component(as_of: datetime, **values: Any) -> Dict[str, Any]:
    component = {
        "available": True,
        "as_of": as_of.isoformat(),
        "source": "verified-product-fixture",
        "source_version": "v1",
        **values,
    }
    component["source_hash"] = hashlib.sha256(
        json.dumps(
            component,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return component


def _qdii_product_evidence(cutoff: datetime) -> Dict[str, Any]:
    return {
        "schema_version": "portfolio-product-evidence-v1",
        "instrument_type": "qdii",
        "market": "cn",
        "symbol": "513870",
        "evidence_cutoff": cutoff.isoformat(),
        "nav_iopv": _product_component(cutoff, nav=1.025, iopv=1.026),
        "premium_discount": _product_component(cutoff, premium_discount_pct=-0.1),
        "underlying_fx": _product_component(cutoff, pair="USD/CNY", rate=7.18),
        "spread": _product_component(cutoff, spread_bps=8.0),
        "tracking": _product_component(cutoff, tracking_difference_pct=-0.25),
    }


def _daily_reset_product_evidence(cutoff: datetime) -> Dict[str, Any]:
    return {
        "schema_version": "portfolio-product-evidence-v1",
        "instrument_type": "daily_leveraged_product",
        "market": "hk",
        "symbol": "HK07709",
        "evidence_cutoff": cutoff.isoformat(),
        "official_terms": _product_component(
            cutoff,
            terms_url="https://issuer.example/HK07709",
            daily_reset=True,
            leverage_factor=2.0,
        ),
        "underlying_same_cutoff": _product_component(
            cutoff,
            market="kr",
            symbol="000660.KS",
            currency="KRW",
            completed_session=True,
        ),
        "completed_session_leverage": _completed_session_leverage_component(cutoff),
        "path_decay_rebalance": _product_component(
            cutoff,
            path_dependency_disclosed=True,
            rebalance_frequency="daily",
        ),
        "liquidity": _product_component(cutoff, spread_bps=12.0),
        "horizon_fit": _product_component(
            cutoff,
            evaluated=True,
            fits_holding_period=True,
        ),
    }


def _completed_session_leverage_component(cutoff: datetime) -> Dict[str, Any]:
    return _product_component(
        cutoff,
        leverage_factor=2.0,
        product_return_pct=1.8,
        underlying_return_pct=1.0,
        observed_leverage=1.8,
    )


@pytest.mark.parametrize(
    ("market", "cutoff", "expected"),
    [
        ("cn", datetime(2026, 8, 6, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 8, 5)),
        ("cn", datetime(2026, 8, 6, 19, 3, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 8, 6)),
        ("hk", datetime(2026, 8, 6, 15, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")), date(2026, 8, 5)),
        ("hk", datetime(2026, 8, 6, 17, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")), date(2026, 8, 6)),
        ("us", datetime(2026, 8, 6, 14, 0, tzinfo=ZoneInfo("America/New_York")), date(2026, 8, 5)),
        ("us", datetime(2026, 8, 6, 17, 0, tzinfo=ZoneInfo("America/New_York")), date(2026, 8, 6)),
        ("us", datetime(2026, 7, 3, 17, 0, tzinfo=ZoneInfo("America/New_York")), date(2026, 7, 2)),
        ("cn", datetime(2026, 10, 1, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 9, 30)),
        ("cn", datetime(2026, 8, 8, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 8, 7)),
        ("cn", datetime(2026, 8, 6, 11, 3, tzinfo=timezone.utc), date(2026, 8, 6)),
        ("us", datetime(2026, 8, 6, 18, 0, tzinfo=timezone.utc), date(2026, 8, 5)),
    ],
)
def test_expected_daily_bar_date_uses_timezone_aware_cutoff(
    market: str,
    cutoff: datetime,
    expected: date,
) -> None:
    assert PortfolioResearchEvidenceService._expected_daily_bar_date(
        market=market,
        cutoff=cutoff,
    ) == expected


def test_expected_daily_bar_date_rejects_naive_cutoff() -> None:
    with pytest.raises(ValueError, match="research_cutoff_timezone_missing"):
        PortfolioResearchEvidenceService._expected_daily_bar_date(
            market="cn",
            cutoff=datetime(2026, 8, 6, 19, 3),
        )


def _daily_frame(
    close: float = 100.0,
    *,
    bar_date: date = AS_OF - timedelta(days=1),
    pct_chg: float = 0.0,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": bar_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 100.0,
                "amount": close * 100,
                "pct_chg": pct_chg,
            }
        ]
    )


def _daily_window(*rows: tuple[date, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": bar_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 100.0,
                "amount": close * 100,
                "pct_chg": 0.0,
            }
            for bar_date, close in rows
        ]
    )


def _provider_normalized_window(
    provider_name: str,
    *rows: tuple[date, float],
) -> pd.DataFrame:
    if provider_name == "YfinanceFetcher":
        closes = [close for _, close in rows]
        raw = pd.DataFrame(
            {
                "Open": closes,
                "High": closes,
                "Low": closes,
                "Close": closes,
                "Volume": [100.0] * len(rows),
            },
            index=pd.DatetimeIndex([bar_date for bar_date, _ in rows], name="Date"),
        )
        return YfinanceFetcher()._normalize_data(raw, "AAPL")
    raw = _daily_window(*rows).drop(columns=["pct_chg"])
    return TencentFetcher()._normalize_data(raw, "600519")


class StubPortfolioService:
    def __init__(self, accounts: list[Dict[str, Any]]) -> None:
        self.accounts = accounts
        self.calls: list[Dict[str, Any]] = []

    def get_portfolio_snapshot(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"accounts": self.accounts}


def _with_provider_warmup(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    if source not in {"YfinanceFetcher", "TencentFetcher"} or len(frame) != 1:
        return frame
    warmup = frame.iloc[[0]].copy()
    warmup["date"] = pd.to_datetime(warmup["date"]) - pd.Timedelta(days=1)
    return pd.concat([warmup, frame], ignore_index=True)


class StubFetcherManager:
    def __init__(self, responses: Dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.days_by_code: Dict[str, list[int]] = {}
        self.realtime_calls: list[tuple[str, Dict[str, Any]]] = []

    def get_daily_data(self, code: str, *, days: int) -> Any:
        self.calls.append(code)
        self.days_by_code.setdefault(code, []).append(days)
        result = self.responses[code]
        if isinstance(result, Exception):
            raise result
        frame, source = result
        return _with_provider_warmup(frame, source), source

    def get_realtime_quote(self, code: str, **kwargs: Any) -> None:
        self.realtime_calls.append((code, kwargs))
        return None


class StubDirectFetcher:
    def __init__(self, name: str, responses: Dict[str, Any]) -> None:
        self.name = name
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    def get_daily_data(self, code: str, *, days: int) -> pd.DataFrame:
        self.calls.append((code, days))
        response_code = "000300" if code == "sh000300" and code not in self.responses else code
        result = self.responses[response_code]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            result = result[0]
        return _with_provider_warmup(result, self.name)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    db_path = tmp_path / "research_evidence.db"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "STOCK_LIST=600519",
                "GEMINI_API_KEY=test",
                "ADMIN_AUTH_ENABLED=false",
                f"DATABASE_PATH={db_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_path))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    Config.reset_instance()
    DatabaseManager.reset_instance()
    manager = DatabaseManager.get_instance()
    yield manager
    DatabaseManager.reset_instance()
    Config.reset_instance()
    os.environ.pop("ENV_FILE", None)
    os.environ.pop("DATABASE_PATH", None)


def _account(
    *positions: Dict[str, Any],
    account_id: int = 1,
    base_currency: str = "CNY",
) -> Dict[str, Any]:
    return {
        "account_id": account_id,
        "base_currency": base_currency,
        "positions": list(positions),
    }


def _position(
    symbol: str,
    *,
    market: str,
    currency: str,
    quantity: float = 10.0,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market": market,
        "currency": currency,
        "quantity": quantity,
    }


def _service(
    db: DatabaseManager,
    *,
    accounts: list[Dict[str, Any]],
    responses: Dict[str, Any],
    fx_fetcher: Any = None,
    qdii_nav_fetcher: Any = None,
    qdii_completed_tracking_fetcher: Any = None,
    realtime_quote_fetcher: Any = None,
    holding_period_evaluator: Any = None,
    collect_product_evidence: bool = False,
    fixed_position_fetchers: Any = None,
    cutoff_provider: Any = None,
) -> tuple[PortfolioResearchEvidenceService, StubPortfolioService, StubFetcherManager]:
    portfolio_service = StubPortfolioService(accounts)
    fetcher_manager = StubFetcherManager(responses)
    baostock_fetcher = StubDirectFetcher("BaostockFetcher", responses)
    tencent_fetcher = StubDirectFetcher("TencentFetcher", responses)
    yfinance_fetcher = StubDirectFetcher("YfinanceFetcher", responses)
    service = PortfolioResearchEvidenceService(
        portfolio_service=portfolio_service,
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=fetcher_manager,
        baostock_benchmark_fetcher=baostock_fetcher,
        tencent_benchmark_fetcher=tencent_fetcher,
        yfinance_benchmark_fetcher=yfinance_fetcher,
        cutoff_provider=cutoff_provider or (lambda: datetime.now(timezone.utc)),
        as_of_provider=lambda: AS_OF,
        fx_fetcher=fx_fetcher,
        qdii_nav_fetcher=qdii_nav_fetcher,
        qdii_completed_tracking_fetcher=(
            qdii_completed_tracking_fetcher or (lambda **_: None)
        ),
        realtime_quote_fetcher=realtime_quote_fetcher,
        holding_period_evaluator=holding_period_evaluator,
        collect_product_evidence=collect_product_evidence,
        fixed_position_fetchers=(
            {} if fixed_position_fetchers is None else fixed_position_fetchers
        ),
    )
    return service, portfolio_service, fetcher_manager


def test_prepare_saves_known_source_adjustment_identity_and_benchmark(db: DatabaseManager) -> None:
    service, portfolio_service, fetcher = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={
            "600519": (_daily_frame(150.0), "EfinanceFetcher"),
            "000300": (_daily_frame(4000.0), "AkshareFetcher"),
        },
    )

    result = service.prepare()

    assert result["status"] == "ready"
    assert result["ready_count"] == 1
    assert result["items"][0]["benchmark_code"] == "000300"
    assert result["items"][0]["price"]["adjustment"] == "qfq"
    assert result["items"][0]["benchmark"]["adjustment"] == "qfq"
    assert result["items"][0]["fx"]["rate"] == 1.0
    assert fetcher.calls == ["600519"]
    assert portfolio_service.calls == [
        {
            "as_of": AS_OF,
            "cost_method": "fifo",
            "include_realtime": False,
            "persist_snapshot": False,
        }
    ]
    with db.get_session() as session:
        rows = {
            row.code: row
            for row in session.execute(select(PortfolioMarketEvidenceBar)).scalars().all()
        }
    assert rows["600519"].data_source == "EfinanceFetcher"
    assert rows["600519"].adjustment_identity == "qfq"
    assert rows["000300"].data_source == "BaostockFetcher"
    assert rows["000300"].adjustment_identity == "qfq"


def test_prepare_uses_fixed_baostock_route_for_cn_position_evidence(
    db: DatabaseManager,
) -> None:
    responses = {
        "600519": (_daily_frame(150.0), "BaostockFetcher"),
        "000300": (_daily_frame(4000.0), "BaostockFetcher"),
    }
    manager = StubFetcherManager({"600519": AssertionError("manager must not be called")})
    baostock = StubDirectFetcher("BaostockFetcher", responses)
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService(
            [_account(_position("600519", market="cn", currency="CNY"))]
        ),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        baostock_benchmark_fetcher=baostock,
        yfinance_benchmark_fetcher=StubDirectFetcher("YfinanceFetcher", {}),
        as_of_provider=lambda: AS_OF,
        collect_product_evidence=False,
        fixed_position_fetchers={"cn": baostock},
    )

    item = service.prepare()["items"][0]

    assert item["status"] == "ready"
    assert manager.calls == []
    assert baostock.calls[0][0] == "600519"
    with db.get_session() as session:
        rows = {
            row.code: row
            for row in session.execute(select(PortfolioMarketEvidenceBar)).scalars().all()
        }
    assert rows["600519"].data_source == "BaostockFetcher"
    assert rows["600519"].adjustment_identity == "qfq"
    assert rows["000300"].data_source == "BaostockFetcher"
    assert rows["000300"].adjustment_identity == "qfq"


def test_prepare_fetches_only_requested_positive_ledger_scope(db: DatabaseManager) -> None:
    service, _, fetcher = _service(
        db,
        accounts=[
            _account(
                _position("600519", market="cn", currency="CNY"),
                account_id=1,
            ),
            _account(
                _position("AAPL", market="us", currency="USD"),
                account_id=2,
                base_currency="USD",
            ),
        ],
        responses={
            "600519": (_daily_frame(150.0), "EfinanceFetcher"),
            "000300": (_daily_frame(4000.0), "BaostockFetcher"),
            "AAPL": (_daily_frame(200.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(600.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare(
        scope=[{"account_id": 2, "market": "US", "symbol": "aapl"}],
    )

    assert result["scope"] == [{"account_id": 2, "market": "us", "symbol": "AAPL"}]
    assert result["position_count"] == 1
    assert [(item["account_id"], item["market"], item["symbol"]) for item in result["items"]] == [
        (2, "us", "AAPL")
    ]
    assert fetcher.calls == ["AAPL"]


def test_prepare_rejects_requested_scope_that_is_not_in_ledger_snapshot(
    db: DatabaseManager,
) -> None:
    service, _, fetcher = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={
            "600519": (_daily_frame(150.0), "EfinanceFetcher"),
            "000300": (_daily_frame(4000.0), "BaostockFetcher"),
        },
    )

    with pytest.raises(ValueError, match="research scope contains non-held positions"):
        service.prepare(
            scope=[{"account_id": 1, "market": "cn", "symbol": "000001"}],
        )

    assert fetcher.calls == []


def test_prepare_reuses_fresh_position_and_benchmark_batches(db: DatabaseManager) -> None:
    service, _, fetcher = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                base_currency="USD",
            )
        ],
        responses={
            "AAPL": (_daily_frame(210.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    first = service.prepare()
    with db.get_session() as session:
        first_row_count = session.scalar(
            select(func.count()).select_from(PortfolioMarketEvidenceBar)
        )
    second = service.prepare()
    with db.get_session() as session:
        second_row_count = session.scalar(
            select(func.count()).select_from(PortfolioMarketEvidenceBar)
        )

    assert first["status"] == second["status"] == "ready"
    assert first["items"][0]["price"] == second["items"][0]["price"]
    assert first["items"][0]["benchmark"] == second["items"][0]["benchmark"]
    assert fetcher.calls == ["AAPL"]
    assert service._benchmark_fetchers["yfinance"].calls == [("SPY", 261)]
    assert first_row_count == second_row_count


def test_prepare_rejects_bar_older_than_last_completed_market_session(
    db: DatabaseManager,
) -> None:
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                base_currency="USD",
            )
        ],
        responses={
            "AAPL": (
                _daily_frame(210.0, bar_date=AS_OF - timedelta(days=2)),
                "YfinanceFetcher",
            ),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    item = result["items"][0]
    assert item["status"] == "insufficient"
    assert item["price"]["status"] == "insufficient"
    assert item["price"]["date"] == (AS_OF - timedelta(days=2)).isoformat()
    assert item["price"]["expected_date"] == (AS_OF - timedelta(days=1)).isoformat()
    assert "position_market_data_stale" in item["blockers"]


def test_prepare_accepts_completed_same_day_bar_after_market_close(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 19, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    service, portfolio_service, _ = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={
            "600519": (_daily_frame(150.0, bar_date=AS_OF), "EfinanceFetcher"),
            "000300": (_daily_frame(4000.0, bar_date=AS_OF), "BaostockFetcher"),
        },
    )

    result = service.prepare(cutoff=cutoff)

    item = result["items"][0]
    assert result["cutoff"] == cutoff.isoformat()
    assert item["status"] == "ready"
    assert item["price"]["date"] == AS_OF.isoformat()
    assert item["price"]["expected_date"] == AS_OF.isoformat()
    assert item["benchmark"]["date"] == AS_OF.isoformat()
    assert portfolio_service.calls[0]["as_of"] == AS_OF


def test_prepare_excludes_unfinished_same_day_bar_during_market_hours(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai"))
    previous = date(2026, 7, 30)
    frame = _daily_window((previous, 149.0), (AS_OF, 150.0))
    benchmark = _daily_window((previous, 3990.0), (AS_OF, 4000.0))
    service, _, _ = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={
            "600519": (frame, "EfinanceFetcher"),
            "000300": (benchmark, "BaostockFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "ready"
    assert item["price"]["date"] == previous.isoformat()
    assert item["price"]["expected_date"] == previous.isoformat()
    assert item["benchmark"]["date"] == previous.isoformat()


def test_prepare_marks_previous_bar_stale_when_completed_session_is_missing(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 19, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    previous = date(2026, 7, 30)
    service, _, _ = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={
            "600519": (_daily_frame(149.0, bar_date=previous), "EfinanceFetcher"),
            "000300": (_daily_frame(3990.0, bar_date=previous), "BaostockFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "insufficient"
    assert item["price"]["date"] == previous.isoformat()
    assert item["price"]["expected_date"] == AS_OF.isoformat()
    assert "position_market_data_stale" in item["blockers"]
    assert "benchmark_market_data_stale" in item["blockers"]


def test_prepare_rejects_naive_cutoff(db: DatabaseManager) -> None:
    service, _, _ = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={},
    )

    with pytest.raises(ValueError, match="research_cutoff_timezone_missing"):
        service.prepare(cutoff=datetime(2026, 7, 31, 19, 3))


def test_prepare_saves_adjusted_yfinance_bar_and_complete_fx_metadata(db: DatabaseManager) -> None:
    fx_calls: list[tuple[str, str, date]] = []

    def fetch_fx(from_currency: str, to_currency: str, as_of: date) -> Dict[str, Any]:
        fx_calls.append((from_currency, to_currency, as_of))
        return {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": 7.2,
            "rate_date": AS_OF,
            "source": "test-fx",
            "source_version": "1",
        }

    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                base_currency="CNY",
            )
        ],
        responses={
            "AAPL": (_daily_frame(210.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
        fx_fetcher=fetch_fx,
    )

    result = service.prepare()

    item = result["items"][0]
    assert item["status"] == "ready"
    assert item["price"]["adjustment"] == "adjusted"
    assert item["benchmark"]["adjustment"] == "adjusted"
    assert item["fx"] == {
        "status": "ready",
        "from_currency": "USD",
        "to_currency": "CNY",
        "rate": 7.2,
        "rate_date": AS_OF.isoformat(),
        "source": "test-fx",
        "source_version": "1",
    }
    assert fx_calls == [("USD", "CNY", AS_OF)]
    with db.get_session() as session:
        fx_row = session.execute(select(PortfolioFxRate)).scalar_one()
        sources = {
            row.code: row.data_source
            for row in session.execute(select(PortfolioMarketEvidenceBar)).scalars().all()
        }
    assert fx_row.source == "test-fx@1"
    assert fx_row.is_stale is False
    assert sources == {
        "AAPL": "YfinanceFetcher",
        "SPY": "YfinanceFetcher",
    }


def test_prepare_fails_closed_for_unknown_adjustment_but_keeps_other_symbol_running(
    db: DatabaseManager,
) -> None:
    service, _, fetcher = _service(
        db,
        accounts=[
            _account(
                _position("600519", market="cn", currency="CNY"),
                _position("AAPL", market="us", currency="USD"),
                base_currency="USD",
            )
        ],
        responses={
            "600519": (_daily_frame(150.0), "MysteryFetcher"),
            "000300": (_daily_frame(4000.0), "AkshareFetcher"),
            "AAPL": (_daily_frame(210.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
        fx_fetcher=lambda from_currency, to_currency, as_of: {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": 0.14,
            "rate_date": as_of,
            "source": "test-fx",
            "source_version": "1",
        },
    )

    result = service.prepare()

    assert result["status"] == "partial"
    assert result["ready_count"] == 1
    assert result["insufficient_count"] == 1
    by_symbol = {item["symbol"]: item for item in result["items"]}
    assert by_symbol["600519"]["status"] == "insufficient"
    assert "position_adjustment_identity_unknown" in by_symbol["600519"]["blockers"]
    assert by_symbol["AAPL"]["status"] == "ready"
    assert fetcher.calls == ["600519", "AAPL"]
    with db.get_session() as session:
        unknown = session.execute(
            select(PortfolioMarketEvidenceBar).where(
                PortfolioMarketEvidenceBar.code == "600519"
            )
        ).scalar_one()
    assert unknown.data_source == "MysteryFetcher"
    assert unknown.adjustment_identity == "unknown"


def test_prepare_rejects_stale_fx_without_writing_cache(db: DatabaseManager) -> None:
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                base_currency="CNY",
            )
        ],
        responses={
            "AAPL": (_daily_frame(210.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
        fx_fetcher=lambda from_currency, to_currency, as_of: {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": 7.2,
            "rate_date": as_of - timedelta(days=8),
            "source": "test-fx",
            "source_version": "1",
        },
    )

    result = service.prepare()

    item = result["items"][0]
    assert item["status"] == "insufficient"
    assert "fx_evidence_unavailable" in item["blockers"]
    with db.get_session() as session:
        assert session.execute(select(func.count()).select_from(PortfolioFxRate)).scalar_one() == 0


def test_prepare_maps_hsi_to_yahoo_symbol_and_isolates_fetch_failure(db: DatabaseManager) -> None:
    service, _, fetcher = _service(
        db,
        accounts=[
            _account(
                _position("HK00700", market="hk", currency="HKD"),
                _position("HK09988", market="hk", currency="HKD"),
                base_currency="HKD",
            )
        ],
        responses={
            "HK00700": RuntimeError("provider unavailable"),
            "HK09988": (_daily_frame(120.0), "TencentFetcher"),
            "^HSI": (_daily_frame(25000.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    by_symbol = {item["symbol"]: item for item in result["items"]}
    assert by_symbol["HK00700"]["status"] == "insufficient"
    assert "position_market_data_unavailable" in by_symbol["HK00700"]["blockers"]
    assert by_symbol["HK09988"]["status"] == "ready"
    assert by_symbol["HK09988"]["benchmark_code"] == "HSI"
    assert fetcher.calls == ["HK00700", "HK09988"]
    with db.get_session() as session:
        benchmark = session.execute(
            select(PortfolioMarketEvidenceBar).where(
                PortfolioMarketEvidenceBar.code == "HSI"
            )
        ).scalar_one()
    assert benchmark.data_source == "YfinanceFetcher"
    assert benchmark.adjustment_identity == "adjusted"


def test_prepare_never_backfills_adjustment_identity_into_existing_bar(
    db: DatabaseManager,
) -> None:
    db.save_daily_data(
        _daily_frame(200.0),
        code="AAPL",
        data_source="LegacyFetcher",
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                base_currency="USD",
            )
        ],
        responses={
            "AAPL": (_daily_frame(210.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    item = result["items"][0]
    assert item["status"] == "ready"
    assert item["price"]["evidence_batch_hash"]
    with db.get_session() as session:
        existing = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalar_one()
        evidence = session.execute(
            select(PortfolioMarketEvidenceBar)
            .where(PortfolioMarketEvidenceBar.code == "AAPL")
            .order_by(PortfolioMarketEvidenceBar.date)
        ).scalars().all()
    assert existing.close == 200.0
    assert existing.data_source == "LegacyFetcher"
    assert evidence[-1].close == 210.0
    assert evidence[-1].batch_hash == item["price"]["evidence_batch_hash"]
    assert evidence[-1].captured_at <= datetime.now(timezone.utc).replace(tzinfo=None)


def test_prepare_rejects_unfinalized_same_day_bar(db: DatabaseManager) -> None:
    service, _, _ = _service(
        db,
        accounts=[_account(_position("600519", market="cn", currency="CNY"))],
        responses={
            "600519": (
                _daily_frame(150.0, bar_date=AS_OF),
                "EfinanceFetcher",
            ),
            "000300": (
                _daily_frame(4000.0, bar_date=AS_OF),
                "AkshareFetcher",
            ),
        },
    )

    result = service.prepare()

    item = result["items"][0]
    assert item["status"] == "insufficient"
    assert "position_market_data_unavailable" in item["blockers"]
    assert "benchmark_market_data_unavailable" in item["blockers"]
    with db.get_session() as session:
        assert session.execute(select(func.count()).select_from(StockDaily)).scalar_one() == 0


def test_prepare_rejects_conflicting_existing_bar_without_overwrite(
    db: DatabaseManager,
) -> None:
    db.save_daily_data(
        _daily_frame(200.0),
        code="AAPL",
        data_source="YfinanceFetcher|adjustment=adjusted",
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                base_currency="USD",
            )
        ],
        responses={
            "AAPL": (_daily_frame(210.0), "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    item = result["items"][0]
    assert item["status"] == "ready"
    assert item["price"]["evidence_batch_hash"]
    with db.get_session() as session:
        existing = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalar_one()
    assert existing.close == 200.0
    assert existing.data_source == "YfinanceFetcher|adjustment=adjusted"


def test_prepare_rejects_conflict_in_any_overlapping_bar_without_partial_write(
    db: DatabaseManager,
) -> None:
    warmup = AS_OF - timedelta(days=3)
    older = AS_OF - timedelta(days=2)
    latest = AS_OF - timedelta(days=1)
    db.save_daily_data(
        _daily_window((older, 200.0)),
        code="AAPL",
        data_source="YfinanceFetcher|adjustment=adjusted",
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("AAPL", market="us", currency="USD"),
                _position("MSFT", market="us", currency="USD"),
                base_currency="USD",
            )
        ],
        responses={
            "AAPL": (
                _daily_window((warmup, 190.0), (older, 201.0), (latest, 210.0)),
                "YfinanceFetcher",
            ),
            "MSFT": (
                _daily_window((warmup, 490.0), (older, 500.0), (latest, 510.0)),
                "YfinanceFetcher",
            ),
            "SPY": (
                _daily_window((warmup, 600.0), (older, 610.0), (latest, 620.0)),
                "YfinanceFetcher",
            ),
        },
    )

    result = service.prepare()

    items = {item["symbol"]: item for item in result["items"]}
    assert items["AAPL"]["status"] == "ready"
    assert items["MSFT"]["status"] == "ready"
    with db.get_session() as session:
        aapl_rows = session.execute(
            select(StockDaily)
            .where(StockDaily.code == "AAPL")
            .order_by(StockDaily.date)
        ).scalars().all()
        msft_rows = session.execute(
            select(StockDaily)
            .where(StockDaily.code == "MSFT")
            .order_by(StockDaily.date)
        ).scalars().all()
    assert [(row.date, row.close) for row in aapl_rows] == [(older, 200.0)]
    assert msft_rows == []
    with db.get_session() as session:
        evidence = session.execute(
            select(PortfolioMarketEvidenceBar)
            .where(PortfolioMarketEvidenceBar.code == "MSFT")
            .order_by(PortfolioMarketEvidenceBar.date)
        ).scalars().all()
    assert [(row.date, row.close) for row in evidence] == [
        (older, 500.0),
        (latest, 510.0),
    ]


def test_prepare_does_not_modify_portfolio_or_decision_state(db: DatabaseManager) -> None:
    portfolio = PortfolioService(repo=PortfolioRepository(db))
    account = portfolio.create_account(
        name="Main",
        broker="Demo",
        market="cn",
        base_currency="CNY",
    )
    portfolio.record_trade(
        account_id=account["id"],
        symbol="600519",
        trade_date=date(2026, 1, 2),
        side="buy",
        quantity=10,
        price=100,
        market="cn",
        currency="CNY",
    )
    fetcher = StubFetcherManager(
        {
            "600519": (_daily_frame(150.0), "EfinanceFetcher"),
            "000300": (_daily_frame(4000.0), "AkshareFetcher"),
        }
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=portfolio,
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=fetcher,
        tencent_benchmark_fetcher=StubDirectFetcher(
            "TencentFetcher",
            fetcher.responses,
        ),
        yfinance_benchmark_fetcher=StubDirectFetcher(
            "YfinanceFetcher",
            fetcher.responses,
        ),
        as_of_provider=lambda: AS_OF,
    )

    tables = (
        PortfolioTrade,
        PortfolioCashLedger,
        PortfolioPosition,
        PortfolioDailySnapshot,
        PortfolioRiskPolicy,
        PortfolioStrategyVersionRecord,
        PortfolioStrategyValidationRunRecord,
        PortfolioStrategyTransitionRecord,
        DecisionSignalRecord,
    )
    with db.get_session() as session:
        before = {
            table.__tablename__: session.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            for table in tables
        }

    result = service.prepare()

    assert result["status"] == "ready"
    with db.get_session() as session:
        after = {
            table.__tablename__: session.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            for table in tables
        }
    assert after == before


@pytest.mark.parametrize(
    ("market", "position", "storage_code", "fetch_code", "provider_name", "adjustment"),
    [
        ("cn", _position("600519", market="cn", currency="CNY"), "000300", "sh000300", "BaostockFetcher", "qfq"),
        ("hk", _position("HK00700", market="hk", currency="HKD"), "HSI", "^HSI", "YfinanceFetcher", "adjusted"),
        ("us", _position("AAPL", market="us", currency="USD"), "SPY", "SPY", "YfinanceFetcher", "adjusted"),
    ],
)
def test_prepare_routes_benchmark_to_fixed_injected_provider(
    db: DatabaseManager,
    market: str,
    position: Dict[str, Any],
    storage_code: str,
    fetch_code: str,
    provider_name: str,
    adjustment: str,
) -> None:
    manager = StubFetcherManager(
        {position["symbol"]: (_daily_frame(100.0), "YfinanceFetcher")}
    )
    baostock = StubDirectFetcher(
        "BaostockFetcher",
        {"sh000300": _daily_frame(4000.0)},
    )
    tencent = StubDirectFetcher(
        "TencentFetcher",
        {"sh000300": _daily_frame(4000.0)},
    )
    yahoo = StubDirectFetcher(
        "YfinanceFetcher",
        {"^HSI": _daily_frame(25000.0), "SPY": _daily_frame(620.0)},
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService(
            [_account(position, base_currency=position["currency"])]
        ),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        baostock_benchmark_fetcher=baostock,
        tencent_benchmark_fetcher=tencent,
        yfinance_benchmark_fetcher=yahoo,
        as_of_provider=lambda: AS_OF,
        fixed_position_fetchers={},
    )

    result = service.prepare()

    assert result["items"][0]["benchmark"]["data_source"] == (
        f"{provider_name}|adjustment={adjustment}"
    )
    assert manager.calls == [position["symbol"]]
    routed = baostock if market == "cn" else yahoo
    assert routed.calls == [(fetch_code, 261)]
    with db.get_session() as session:
        row = session.execute(
            select(PortfolioMarketEvidenceBar).where(
                PortfolioMarketEvidenceBar.code == storage_code
            )
        ).scalar_one()
    assert row.data_source == provider_name
    assert row.adjustment_identity == adjustment


def test_prepare_routes_cn_benchmark_to_complete_baostock_evidence(
    db: DatabaseManager,
) -> None:
    manager = StubFetcherManager(
        {"600519": (_daily_frame(150.0), "EfinanceFetcher")}
    )
    tencent = StubDirectFetcher(
        "TencentFetcher",
        {"sh000300": _daily_frame(4000.0)},
    )
    baostock = StubDirectFetcher(
        "BaostockFetcher",
        {"sh000300": _daily_frame(4000.0)},
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService(
            [_account(_position("600519", market="cn", currency="CNY"))]
        ),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        tencent_benchmark_fetcher=tencent,
        yfinance_benchmark_fetcher=StubDirectFetcher("YfinanceFetcher", {}),
        as_of_provider=lambda: AS_OF,
    )
    service._benchmark_fetchers["baostock"] = baostock

    result = service.prepare()

    assert result["items"][0]["benchmark"]["source"] == "BaostockFetcher"
    assert result["items"][0]["benchmark"]["adjustment"] == "qfq"
    assert baostock.calls == [("sh000300", 261)]
    assert tencent.calls == []


def test_baostock_source_has_explicit_qfq_adjustment_identity() -> None:
    assert PortfolioResearchEvidenceService._source_adjustment("BaostockFetcher") == "qfq"


def test_prepare_refetches_fresh_batch_with_unknown_adjustment(
    db: DatabaseManager,
) -> None:
    manager = StubFetcherManager(
        {
            "600519": (
                _daily_frame(150.0),
                "BaostockFetcher|adjustment=qfq",
            )
        }
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )
    service.market_evidence_repo.append_batch(
        _daily_frame(150.0),
        code="600519",
        data_source="BaostockFetcher",
        source_version=service.SCHEMA_VERSION,
        adjustment_identity="unknown",
        captured_at=datetime.now(timezone.utc),
    )

    result = service._prepare_bar(
        fetch_code="600519",
        storage_code="600519",
        as_of=AS_OF,
        blocker_prefix="position",
        market="cn",
    )

    assert manager.calls == ["600519"]
    assert result["status"] == "ready"
    assert result["adjustment"] == "qfq"


def test_prepare_blocks_qdii_without_same_cutoff_premium_evidence(
    db: DatabaseManager,
) -> None:
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "513870",
            "market": "cn",
            "quote_currency": "CNY",
            "instrument_type": "qdii",
            "trade_lot_size": 100.0,
            "requires_premium_check": True,
            "verification_status": "verified",
        }
    )
    service, _, _ = _service(
        db,
        accounts=[_account(_position("513870", market="cn", currency="CNY"))],
        responses={
            "513870": (
                _daily_frame(2.0),
                "BaostockFetcher|adjustment=qfq",
            ),
            "000300": (_daily_frame(4000.0), "BaostockFetcher"),
        },
    )

    result = service.prepare()

    assert result["status"] == "partial"
    assert result["ready_count"] == 0
    assert result["insufficient_count"] == 1
    assert result["items"][0]["price"]["status"] == "ready"
    assert result["items"][0]["benchmark"]["status"] == "ready"
    assert result["items"][0]["status"] == "insufficient"
    assert set(result["items"][0]["blockers"]) == {
        "qdii_nav_iopv_missing",
        "qdii_premium_discount_missing",
        "qdii_underlying_fx_missing",
        "qdii_spread_missing",
        "qdii_tracking_evidence_missing",
    }
    assert result["items"][0]["product_evidence"] == {
        "instrument_type": "qdii",
        "status": "insufficient",
        "nav_iopv_available": False,
        "premium_discount_available": False,
        "underlying_fx_available": False,
        "spread_available": False,
        "tracking_available": False,
    }


def test_prepare_accepts_complete_same_cutoff_qdii_product_evidence(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "513870",
            "market": "cn",
            "quote_currency": "CNY",
            "instrument_type": "qdii",
            "trade_lot_size": 100.0,
            "requires_premium_check": True,
            "verification_status": "verified",
            "metadata_json": json.dumps(
                {"product_evidence": _qdii_product_evidence(cutoff)}
            ),
        }
    )
    service, _, _ = _service(
        db,
        accounts=[_account(_position("513870", market="cn", currency="CNY"))],
        responses={
            "513870": (_daily_frame(2.0, bar_date=AS_OF), "BaostockFetcher|adjustment=qfq"),
            "000300": (_daily_frame(4000.0, bar_date=AS_OF), "BaostockFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "ready"
    assert item["blockers"] == []
    assert item["product_evidence"]["status"] == "ready"
    assert item["product_evidence"]["nav_iopv_available"] is True
    assert item["product_evidence"]["evidence_hash"]


def test_prepare_does_not_fallback_to_nav_ndx_when_sse_tracking_is_missing(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "513870",
            "market": "cn",
            "quote_currency": "CNY",
            "instrument_type": "qdii",
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "trade_lot_size": 100.0,
            "requires_premium_check": True,
            "verification_status": "verified",
            "evidence_source": "https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=513870",
            "evidence_as_of": datetime(2026, 7, 30, 0, 0),
            "metadata_json": json.dumps({"name": "纳指ETF富国"}),
        }
    )
    service, _, fetcher = _service(
        db,
        accounts=[_account(_position("513870", market="cn", currency="CNY"))],
        responses={
            "513870": (
                _daily_frame(2.0, bar_date=AS_OF, pct_chg=1.0),
                "BaostockFetcher|adjustment=qfq",
            ),
            "000300": (_daily_frame(4000.0, bar_date=AS_OF), "BaostockFetcher"),
            "NDX": (
                _daily_frame(20000.0, bar_date=AS_OF - timedelta(days=1), pct_chg=1.5),
                "YfinanceFetcher",
            ),
        },
        fx_fetcher=lambda *_: {
            "from_currency": "USD",
            "to_currency": "CNY",
            "rate": 7.18,
            "rate_date": AS_OF,
            "return_pct": 0.2,
            "source": "verified-fx-fixture",
            "source_version": "v1",
        },
        qdii_nav_fetcher=lambda **_: {
            "nav": 1.99,
            "nav_date": AS_OF,
            "nav_return_pct": 1.8,
            "source": "verified-nav-fixture",
            "source_version": "v1",
        },
        realtime_quote_fetcher=lambda **_: {
            "price": 2.0,
            "bid": 1.99,
            "ask": 2.01,
            "provider_timestamp": (cutoff - timedelta(minutes=1)).isoformat(),
            "source": "verified-quote-fixture",
            "source_version": "v1",
        },
        collect_product_evidence=True,
    )
    service.qdii_reference_fetcher = lambda **_: {
        "reference_type": "iopv",
        "reference_value": 1.99,
        "source": "sse-yunhq",
        "provider_timestamp": (cutoff - timedelta(seconds=40)).isoformat(),
    }
    service.realtime_fx_quote_fetcher = lambda **_: {
        "pair": "USD/CNY",
        "rate": 7.18,
        "source": "verified-realtime-fx-fixture",
        "source_version": "v1",
        "provider_timestamp": (cutoff - timedelta(seconds=30)).isoformat(),
    }

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "insufficient", item
    assert item["blockers"] == ["qdii_tracking_evidence_missing"]
    assert item["product_evidence"]["status"] == "insufficient"
    assert item["product_evidence"]["nav_iopv_available"] is True
    assert item["product_evidence"]["premium_discount_available"] is True
    assert item["product_evidence"]["underlying_fx_available"] is True
    assert item["product_evidence"]["spread_available"] is True
    assert item["product_evidence"]["tracking_available"] is False
    observation = item["product_evidence"]["components"][
        "execution_reference_observation"
    ]
    assert observation["market_price"]["source"] == "verified-quote-fixture"
    assert observation["reference_value"]["source"] == "sse-yunhq"
    assert observation["fx"]["source"] == "verified-realtime-fx-fixture"
    assert observation["timestamp_alignment_seconds"] == 30.0
    assert "NDX" not in fetcher.calls
    instrument = next(row for row in portfolio_repo.list_instruments() if row.symbol == "513870")
    assert "product_evidence" not in json.loads(instrument.metadata_json)


def test_prepare_uses_sse_completed_tracking_without_current_nav_or_ndx(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 8, 10, 6, 20, tzinfo=timezone.utc)
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "513870",
            "market": "cn",
            "quote_currency": "CNY",
            "instrument_type": "qdii",
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "trade_lot_size": 100.0,
            "requires_premium_check": True,
            "verification_status": "verified",
            "metadata_json": json.dumps({"name": "纳指ETF富国"}),
        }
    )
    service, _, fetcher = _service(
        db,
        accounts=[_account(_position("513870", market="cn", currency="CNY"))],
        responses={
            "513870": (
                _daily_frame(2.088, bar_date=date(2026, 8, 7), pct_chg=0.431),
                "BaostockFetcher|adjustment=qfq",
            ),
            "000300": (
                _daily_frame(4702.0247, bar_date=date(2026, 8, 7)),
                "BaostockFetcher",
            ),
        },
        qdii_nav_fetcher=lambda **_: (_ for _ in ()).throw(
            ValueError("same-day NAV unpublished")
        ),
        qdii_completed_tracking_fetcher=lambda **_: {
            "symbol": "513870",
            "reference_type": "iopv",
            "session_date": "2026-08-07",
            "previous_session_date": "2026-08-06",
            "product_reference_price": 2.079,
            "product_current_price": 2.088,
            "reference_reference_value": 1.9003,
            "reference_current_value": 1.8931,
            "product_return_pct": 0.4329,
            "reference_return_pct": -0.378888,
            "tracking_difference_pct": 0.811788,
            "formula": "product_return-reference_return",
            "fx_incorporated_in_reference": True,
            "source": "sse-yunhq-dayk",
            "source_version": "sse-yunhq-v1",
        },
        realtime_quote_fetcher=lambda **_: {
            "price": 2.098,
            "bid": 2.097,
            "ask": 2.099,
            "provider_timestamp": (cutoff - timedelta(seconds=30)).isoformat(),
            "source": "verified-product-fixture",
            "source_version": "v1",
        },
        collect_product_evidence=True,
    )
    service.qdii_reference_fetcher = lambda **_: {
        "reference_type": "iopv",
        "reference_value": 1.916,
        "source": "sse-yunhq",
        "provider_timestamp": (cutoff - timedelta(seconds=20)).isoformat(),
    }
    service.realtime_fx_quote_fetcher = lambda **_: {
        "pair": "USD/CNY",
        "rate": 6.73,
        "source": "verified-realtime-fx-fixture",
        "source_version": "v1",
        "provider_timestamp": (cutoff - timedelta(seconds=10)).isoformat(),
    }
    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "ready", item
    assert item["blockers"] == []
    product = item["product_evidence"]
    assert product["nav_iopv_available"] is True
    assert product["premium_discount_available"] is True
    assert product["underlying_fx_available"] is True
    assert product["spread_available"] is True
    assert product["tracking_available"] is True
    tracking = product["components"]["tracking"]
    assert tracking["session_date"] == "2026-08-07"
    assert tracking["formula"] == "product_return-reference_return"
    assert tracking["fx_incorporated_in_reference"] is True
    assert "NDX" not in fetcher.calls


def test_prepare_establishes_cutoff_after_prefetching_qdii_dynamic_inputs(
    db: DatabaseManager,
) -> None:
    requested_cutoff = datetime(2026, 8, 10, 6, 20, tzinfo=timezone.utc)
    established_cutoff = requested_cutoff + timedelta(seconds=40)
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "513870",
            "market": "cn",
            "quote_currency": "CNY",
            "instrument_type": "qdii",
            "underlying_symbol": "NDX",
            "underlying_market": "us",
            "underlying_currency": "USD",
            "trade_lot_size": 100.0,
            "requires_premium_check": True,
            "verification_status": "verified",
            "metadata_json": json.dumps({"name": "纳指ETF富国"}),
        }
    )
    quote_calls = []
    reference_calls = []
    fx_calls = []

    def realtime_quote(**kwargs):
        quote_calls.append(kwargs)
        return {
            "price": 2.098,
            "bid": 2.097,
            "ask": 2.099,
            "provider_timestamp": (requested_cutoff + timedelta(seconds=10)).isoformat(),
            "source": "verified-product-fixture",
            "source_version": "v1",
        }

    service, _, fetcher = _service(
        db,
        accounts=[_account(_position("513870", market="cn", currency="CNY"))],
        responses={
            "513870": (
                _daily_frame(2.088, bar_date=date(2026, 8, 7), pct_chg=0.431),
                "BaostockFetcher|adjustment=qfq",
            ),
            "000300": (
                _daily_frame(4702.0247, bar_date=date(2026, 8, 7)),
                "BaostockFetcher",
            ),
        },
        qdii_nav_fetcher=lambda **_: None,
        qdii_completed_tracking_fetcher=lambda **_: {
            "symbol": "513870",
            "reference_type": "iopv",
            "session_date": "2026-08-07",
            "previous_session_date": "2026-08-06",
            "product_reference_price": 2.079,
            "product_current_price": 2.088,
            "reference_reference_value": 1.9003,
            "reference_current_value": 1.8931,
            "product_return_pct": 0.4329,
            "reference_return_pct": -0.378888,
            "tracking_difference_pct": 0.811788,
            "formula": "product_return-reference_return",
            "fx_incorporated_in_reference": True,
            "source": "sse-yunhq-dayk",
            "source_version": "sse-yunhq-v1",
        },
        realtime_quote_fetcher=realtime_quote,
        collect_product_evidence=True,
        cutoff_provider=lambda: established_cutoff,
    )

    def reference(**kwargs):
        reference_calls.append(kwargs)
        return {
            "reference_type": "iopv",
            "reference_value": 1.916,
            "source": "sse-yunhq",
            "provider_timestamp": (requested_cutoff + timedelta(seconds=20)).isoformat(),
        }

    def realtime_fx(**kwargs):
        fx_calls.append(kwargs)
        return {
            "pair": "USD/CNY",
            "rate": 6.73,
            "source": "verified-realtime-fx-fixture",
            "source_version": "v1",
            "provider_timestamp": (requested_cutoff + timedelta(seconds=30)).isoformat(),
        }

    service.qdii_reference_fetcher = reference
    service.realtime_fx_quote_fetcher = realtime_fx

    result = service.prepare(
        cutoff=requested_cutoff,
        establish_cutoff=True,
    )

    item = result["items"][0]
    assert result["cutoff"] == established_cutoff.isoformat()
    assert item["status"] == "ready", item
    assert item["product_evidence"]["status"] == "ready"
    assert item["product_evidence"]["evidence_cutoff"] == established_cutoff.isoformat().replace(
        "+00:00", "Z"
    )
    assert len(quote_calls) == 1
    assert len(reference_calls) == 1
    assert len(fx_calls) == 1
    assert "NDX" not in fetcher.calls


@pytest.mark.parametrize(
    ("established_cutoff", "expected_error"),
    [
        (
            datetime(2026, 8, 10, 6, 19, 59, tzinfo=timezone.utc),
            "research_cutoff_cannot_move_backward",
        ),
        (
            datetime(2026, 8, 10, 16, 0, 0, tzinfo=timezone.utc),
            "research_cutoff_date_changed_during_preparation",
        ),
    ],
)
def test_prepare_rejects_invalid_server_established_cutoff(
    db: DatabaseManager,
    established_cutoff: datetime,
    expected_error: str,
) -> None:
    requested_cutoff = datetime(2026, 8, 10, 6, 20, tzinfo=timezone.utc)
    service, _, _ = _service(
        db,
        accounts=[],
        responses={},
        cutoff_provider=lambda: established_cutoff,
    )

    with pytest.raises(ValueError, match=expected_error):
        service.prepare(cutoff=requested_cutoff, establish_cutoff=True)


def test_product_evidence_quote_fetch_stops_after_first_complete_source(
    db: DatabaseManager,
) -> None:
    service, _, fetcher = _service(
        db,
        accounts=[],
        responses={},
        collect_product_evidence=True,
    )

    service._fetch_realtime_quote(symbol="513870", market="cn", cutoff=datetime.now(timezone.utc))

    assert fetcher.realtime_calls == [
        (
            "513870",
            {
                "log_final_failure": False,
                "supplement": False,
            },
        )
    ]


def test_prepare_default_realtime_fx_loader_prefers_tencent_provider_timestamp(
    monkeypatch,
) -> None:
    expected = object()
    monkeypatch.setattr(
        AkshareFetcher,
        "get_realtime_fx_quote",
        lambda _self, from_currency, to_currency: expected,
        raising=False,
    )
    monkeypatch.setattr(
        YfinanceFetcher,
        "get_realtime_fx_quote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Yahoo fallback should not run when Tencent FX is available")
        ),
    )

    result = PortfolioResearchEvidenceService._fetch_realtime_fx_quote(
        from_currency="USD",
        to_currency="CNY",
    )

    assert result is expected


def test_prepare_rejects_qdii_product_evidence_bound_to_another_cutoff(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    stale_cutoff = cutoff - timedelta(minutes=1)
    PortfolioRepository(db).create_instrument(
        {
            "symbol": "513870",
            "market": "cn",
            "quote_currency": "CNY",
            "instrument_type": "qdii",
            "trade_lot_size": 100.0,
            "requires_premium_check": True,
            "verification_status": "verified",
            "metadata_json": json.dumps(
                {"product_evidence": _qdii_product_evidence(stale_cutoff)}
            ),
        }
    )
    service, _, _ = _service(
        db,
        accounts=[_account(_position("513870", market="cn", currency="CNY"))],
        responses={
            "513870": (_daily_frame(2.0, bar_date=AS_OF), "BaostockFetcher|adjustment=qfq"),
            "000300": (_daily_frame(4000.0, bar_date=AS_OF), "BaostockFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "insufficient"
    assert "product_evidence_cutoff_mismatch" in item["blockers"]


def test_prepare_blocks_daily_reset_product_without_execution_evidence(
    db: DatabaseManager,
) -> None:
    PortfolioRepository(db).create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
        }
    )
    service, _, _ = _service(
        db,
        accounts=[_account(_position("HK07709", market="hk", currency="HKD"), base_currency="HKD")],
        responses={
            "HK07709": (_daily_frame(8.0), "TencentFetcher"),
            "^HSI": (_daily_frame(25000.0), "YfinanceFetcher"),
        },
    )

    item = service.prepare()["items"][0]

    assert item["status"] == "insufficient"
    assert set(item["blockers"]) >= {
        "daily_reset_official_terms_missing",
        "daily_reset_underlying_same_cutoff_missing",
        "daily_reset_completed_session_leverage_missing",
        "daily_reset_path_decay_rebalance_missing",
        "daily_reset_liquidity_missing",
        "daily_reset_horizon_fit_missing",
    }
    assert item["product_evidence"]["instrument_type"] == "daily_leveraged_product"
    assert item["product_evidence"]["daily_reset"] is True
    assert item["product_evidence"]["leverage_factor"] == 2.0
    assert item["product_evidence"]["underlying_identity"] == "kr:000660.KS"
    assert item["product_evidence"]["official_terms_available"] is False


def test_prepare_accepts_complete_daily_reset_product_evidence(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    PortfolioRepository(db).create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
            "evidence_source": "issuer terms",
            "evidence_as_of": cutoff.replace(tzinfo=None),
            "metadata_json": json.dumps(
                {"product_evidence": _daily_reset_product_evidence(cutoff)}
            ),
        }
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("HK07709", market="hk", currency="HKD"),
                base_currency="HKD",
            )
        ],
        responses={
            "HK07709": (_daily_frame(8.0, bar_date=AS_OF), "TencentFetcher"),
            "^HSI": (_daily_frame(25000.0, bar_date=AS_OF), "YfinanceFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "ready"
    assert item["blockers"] == []
    assert item["product_evidence"]["status"] == "ready"
    assert item["product_evidence"]["underlying_same_cutoff_available"] is True


def test_prepare_collects_daily_reset_product_evidence_without_registry_payload(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
            "evidence_source": "https://www.hkex.com.hk/Market-Data/Securities-Prices/Exchange-Traded-Products/Exchange-Traded-Products-Quote?sym=7709&sc_lang=en",
            "evidence_as_of": datetime(2026, 7, 30, 0, 0),
            "metadata_json": json.dumps({"name": "CSOP SK Hynix Daily (2x) Leveraged Product"}),
        }
    )
    service, _, fetcher = _service(
        db,
        accounts=[
            _account(
                _position("HK07709", market="hk", currency="HKD"),
                account_id=5,
                base_currency="HKD",
            )
        ],
        responses={
            "HK07709": (
                _daily_frame(30.0, bar_date=AS_OF, pct_chg=2.4),
                "TencentFetcher",
            ),
            "^HSI": (_daily_frame(25000.0, bar_date=AS_OF), "YfinanceFetcher"),
            "000660.KS": (
                _daily_frame(200000.0, bar_date=AS_OF, pct_chg=1.2),
                "YfinanceFetcher",
            ),
        },
        realtime_quote_fetcher=lambda **_: {
            "bid": 29.98,
            "ask": 30.02,
            "provider_timestamp": (cutoff - timedelta(minutes=1)).isoformat(),
            "source": "verified-quote-fixture",
            "source_version": "v1",
        },
        holding_period_evaluator=lambda **_: {
            "evaluated": True,
            "fits_holding_period": False,
            "first_open_date": "2026-07-16",
            "source": "verified-ledger-fixture",
            "source_version": "v1",
        },
        collect_product_evidence=True,
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "ready"
    assert item["blockers"] == []
    assert item["product_evidence"]["status"] == "ready"
    assert item["product_evidence"]["official_terms_available"] is True
    assert item["product_evidence"]["underlying_same_cutoff_available"] is True
    assert item["product_evidence"]["completed_session_leverage_available"] is True
    assert "intraday_leverage" not in item["product_evidence"]["components"]
    assert item["product_evidence"]["path_decay_rebalance_available"] is True
    assert item["product_evidence"]["liquidity_available"] is True
    assert item["product_evidence"]["horizon_fit_evaluated"] is True
    assert item["product_evidence"]["components"]["horizon_fit"]["fits_holding_period"] is False
    assert "000660.KS" in fetcher.calls
    instrument = next(row for row in portfolio_repo.list_instruments() if row.symbol == "HK07709")
    assert "product_evidence" not in json.loads(instrument.metadata_json)


def test_prepare_establishes_cutoff_after_prefetching_daily_reset_liquidity(
    db: DatabaseManager,
) -> None:
    requested_cutoff = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
    established_cutoff = requested_cutoff + timedelta(seconds=20)
    portfolio_repo = PortfolioRepository(db)
    portfolio_repo.create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
            "evidence_source": "https://www.hkex.com.hk/Market-Data/Securities-Prices/Exchange-Traded-Products/Exchange-Traded-Products-Quote?sym=7709&sc_lang=en",
            "evidence_as_of": datetime(2026, 7, 30, 0, 0),
            "metadata_json": json.dumps({"name": "CSOP SK Hynix Daily (2x) Leveraged Product"}),
        }
    )
    quote_calls = []

    def realtime_quote(**kwargs):
        quote_calls.append(kwargs)
        return {
            "bid": 29.98,
            "ask": 30.02,
            "provider_timestamp": (requested_cutoff + timedelta(seconds=10)).isoformat(),
            "source": "verified-quote-fixture",
            "source_version": "v1",
        }

    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("HK07709", market="hk", currency="HKD"),
                account_id=5,
                base_currency="HKD",
            )
        ],
        responses={
            "HK07709": (
                _daily_frame(30.0, bar_date=AS_OF, pct_chg=2.4),
                "TencentFetcher",
            ),
            "^HSI": (_daily_frame(25000.0, bar_date=AS_OF), "YfinanceFetcher"),
            "000660.KS": (
                _daily_frame(200000.0, bar_date=AS_OF, pct_chg=1.2),
                "YfinanceFetcher",
            ),
        },
        realtime_quote_fetcher=realtime_quote,
        holding_period_evaluator=lambda **_: {
            "evaluated": True,
            "fits_holding_period": False,
            "first_open_date": "2026-07-16",
            "source": "verified-ledger-fixture",
            "source_version": "v1",
        },
        collect_product_evidence=True,
        cutoff_provider=lambda: established_cutoff,
    )

    result = service.prepare(
        cutoff=requested_cutoff,
        establish_cutoff=True,
    )

    item = result["items"][0]
    assert result["cutoff"] == established_cutoff.isoformat()
    assert item["status"] == "ready", item
    assert item["product_evidence"]["liquidity_available"] is True
    assert item["product_evidence"]["evidence_cutoff"] == established_cutoff.isoformat().replace(
        "+00:00", "Z"
    )
    assert len(quote_calls) == 1


def test_prepare_rejects_daily_reset_target_without_observed_leverage(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    evidence = _daily_reset_product_evidence(cutoff)
    evidence["completed_session_leverage"] = _product_component(
        cutoff,
        leverage_factor=2.0,
    )
    PortfolioRepository(db).create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
            "evidence_source": "issuer terms",
            "evidence_as_of": cutoff.replace(tzinfo=None),
            "metadata_json": json.dumps({"product_evidence": evidence}),
        }
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("HK07709", market="hk", currency="HKD"),
                base_currency="HKD",
            )
        ],
        responses={
            "HK07709": (_daily_frame(8.0, bar_date=AS_OF), "TencentFetcher"),
            "^HSI": (_daily_frame(25000.0, bar_date=AS_OF), "YfinanceFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "insufficient"
    assert "daily_reset_completed_session_leverage_missing" in item["blockers"]


def test_prepare_does_not_treat_legacy_intraday_field_as_completed_session_evidence(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    evidence = _daily_reset_product_evidence(cutoff)
    evidence["intraday_leverage"] = evidence.pop("completed_session_leverage")
    PortfolioRepository(db).create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
            "evidence_source": "issuer terms",
            "evidence_as_of": cutoff.replace(tzinfo=None),
            "metadata_json": json.dumps({"product_evidence": evidence}),
        }
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("HK07709", market="hk", currency="HKD"),
                base_currency="HKD",
            )
        ],
        responses={
            "HK07709": (_daily_frame(8.0, bar_date=AS_OF), "TencentFetcher"),
            "^HSI": (_daily_frame(25000.0, bar_date=AS_OF), "YfinanceFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "insufficient"
    assert "daily_reset_completed_session_leverage_missing" in item["blockers"]
    assert item["product_evidence"]["completed_session_leverage_available"] is False


def test_prepare_accepts_evaluated_incompatible_daily_reset_horizon(
    db: DatabaseManager,
) -> None:
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    evidence = _daily_reset_product_evidence(cutoff)
    evidence["horizon_fit"] = _product_component(
        cutoff,
        evaluated=True,
        fits_holding_period=False,
    )
    PortfolioRepository(db).create_instrument(
        {
            "symbol": "HK07709",
            "market": "hk",
            "quote_currency": "HKD",
            "instrument_type": "daily_leveraged_product",
            "underlying_symbol": "000660.KS",
            "underlying_market": "kr",
            "underlying_currency": "KRW",
            "leverage_factor": 2.0,
            "daily_reset": True,
            "trade_lot_size": 100.0,
            "verification_status": "verified",
            "evidence_source": "issuer terms",
            "evidence_as_of": cutoff.replace(tzinfo=None),
            "metadata_json": json.dumps({"product_evidence": evidence}),
        }
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(
                _position("HK07709", market="hk", currency="HKD"),
                base_currency="HKD",
            )
        ],
        responses={
            "HK07709": (_daily_frame(8.0, bar_date=AS_OF), "TencentFetcher"),
            "^HSI": (_daily_frame(25000.0, bar_date=AS_OF), "YfinanceFetcher"),
        },
    )

    item = service.prepare(cutoff=cutoff)["items"][0]

    assert item["status"] == "ready"
    assert item["product_evidence"]["horizon_fit_evaluated"] is True
    assert item["product_evidence"]["components"]["horizon_fit"][
        "fits_holding_period"
    ] is False


def test_prepare_fixed_benchmark_failure_does_not_fallback_to_manager(
    db: DatabaseManager,
) -> None:
    manager = StubFetcherManager(
        {
            "600519": (_daily_frame(150.0), "TencentFetcher"),
            "000300": (_daily_frame(4000.0), "AkshareFetcher"),
        }
    )
    baostock = StubDirectFetcher(
        "BaostockFetcher",
        {"sh000300": RuntimeError("fixed provider unavailable")},
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService(
            [_account(_position("600519", market="cn", currency="CNY"))]
        ),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        baostock_benchmark_fetcher=baostock,
        yfinance_benchmark_fetcher=StubDirectFetcher("YfinanceFetcher", {}),
        as_of_provider=lambda: AS_OF,
        fixed_position_fetchers={},
    )

    result = service.prepare()

    assert result["items"][0]["benchmark"] == {
        "status": "insufficient",
        "code": "000300",
    }
    assert "benchmark_market_data_unavailable" in result["items"][0]["blockers"]
    assert manager.calls == ["600519"]
    assert baostock.calls == [("sh000300", 261)]


def test_prepare_accepts_exact_legacy_overlap_only_when_new_explicit_bar_exists(
    db: DatabaseManager,
) -> None:
    warmup_date = AS_OF - timedelta(days=3)
    legacy_date = AS_OF - timedelta(days=2)
    new_date = AS_OF - timedelta(days=1)
    legacy_frame = _daily_window((legacy_date, 200.0))
    db.save_daily_data(legacy_frame, code="AAPL", data_source="YfinanceFetcher")
    with db.get_session() as session:
        before = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalar_one()
        before_values = (
            before.open,
            before.high,
            before.low,
            before.close,
            before.volume,
            before.amount,
            before.pct_chg,
            before.data_source,
            before.created_at,
            before.updated_at,
        )
    service, _, _ = _service(
        db,
        accounts=[
            _account(_position("AAPL", market="us", currency="USD"), base_currency="USD")
        ],
        responses={
            "AAPL": (
                _daily_window(
                    (warmup_date, 190.0),
                    (legacy_date, 200.0),
                    (new_date, 210.0),
                ),
                "YfinanceFetcher",
            ),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    assert result["items"][0]["price"]["status"] == "ready"
    with db.get_session() as session:
        rows = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalars().all()
        evidence = session.execute(
            select(PortfolioMarketEvidenceBar)
            .where(PortfolioMarketEvidenceBar.code == "AAPL")
            .order_by(PortfolioMarketEvidenceBar.date)
        ).scalars().all()
    assert len(rows) == 1
    assert (
        rows[0].open,
        rows[0].high,
        rows[0].low,
        rows[0].close,
        rows[0].volume,
        rows[0].amount,
        rows[0].pct_chg,
        rows[0].data_source,
        rows[0].created_at,
        rows[0].updated_at,
    ) == before_values
    assert [(row.date, row.close) for row in evidence] == [
        (legacy_date, 200.0),
        (new_date, 210.0),
    ]
    assert all(row.adjustment_identity == "adjusted" for row in evidence)


def test_prepare_keeps_only_legacy_target_insufficient(db: DatabaseManager) -> None:
    legacy_date = AS_OF - timedelta(days=1)
    frame = _daily_window((legacy_date, 200.0))
    db.save_daily_data(frame, code="AAPL", data_source="YfinanceFetcher")
    service, _, _ = _service(
        db,
        accounts=[
            _account(_position("AAPL", market="us", currency="USD"), base_currency="USD")
        ],
        responses={
            "AAPL": (frame, "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    assert result["items"][0]["price"]["status"] == "ready"
    assert result["items"][0]["price"]["adjustment"] == "adjusted"


@pytest.mark.parametrize("existing_source", ["TencentFetcher", "YfinanceFetcher-v1", "OtherFetcher"])
def test_prepare_rejects_legacy_overlap_from_different_or_noncanonical_provider(
    db: DatabaseManager,
    existing_source: str,
) -> None:
    warmup_date = AS_OF - timedelta(days=3)
    bar_date = AS_OF - timedelta(days=2)
    db.save_daily_data(
        _daily_window((bar_date, 200.0)),
        code="AAPL",
        data_source=existing_source,
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(_position("AAPL", market="us", currency="USD"), base_currency="USD")
        ],
        responses={
            "AAPL": (
                _daily_window(
                    (warmup_date, 190.0),
                    (bar_date, 200.0),
                    (AS_OF - timedelta(days=1), 210.0),
                ),
                "YfinanceFetcher",
            ),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    assert result["items"][0]["status"] == "ready"


@pytest.mark.parametrize(
    ("field", "existing_value", "fetched_value"),
    [
        ("open", 200.0, 201.0),
        ("open", 200.0, 200.0000000000001),
        ("high", 200.0, 201.0),
        ("low", 200.0, 201.0),
        ("close", 200.0, 201.0),
        ("volume", 100.0, 101.0),
        ("amount", 20000.0, 20100.0),
        ("pct_chg", 0.0, 0.1),
        ("amount", None, 20000.0),
        ("amount", 20000.0, None),
        ("amount", 20000.0, float("nan")),
        ("amount", 20000.0, float("inf")),
    ],
)
def test_prepare_rejects_any_nonfinite_missing_or_different_legacy_field(
    db: DatabaseManager,
    field: str,
    existing_value: Any,
    fetched_value: Any,
) -> None:
    overlap_date = AS_OF - timedelta(days=1)
    existing = _daily_window((overlap_date, 200.0))
    existing.loc[0, field] = existing_value
    fetched = _daily_window((overlap_date, 200.0))
    fetched.loc[0, field] = fetched_value
    db.save_daily_data(existing, code="AAPL", data_source="YfinanceFetcher")
    service, _, _ = _service(
        db,
        accounts=[
            _account(_position("AAPL", market="us", currency="USD"), base_currency="USD")
        ],
        responses={
            "AAPL": (fetched, "YfinanceFetcher"),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    fetched_is_valid = (
        fetched_value is not None
        and not pd.isna(fetched_value)
        and math.isfinite(float(fetched_value))
    )
    if fetched_is_valid:
        assert result["items"][0]["status"] == "ready"
    else:
        assert "position_market_data_unavailable" in result["items"][0]["blockers"]


def test_prepare_validates_all_overlaps_before_saving_any_absent_date(
    db: DatabaseManager,
) -> None:
    warmup_date = AS_OF - timedelta(days=4)
    absent_date = AS_OF - timedelta(days=3)
    conflict_date = AS_OF - timedelta(days=2)
    latest_date = AS_OF - timedelta(days=1)
    db.save_daily_data(
        _daily_window((conflict_date, 200.0)),
        code="AAPL",
        data_source="YfinanceFetcher",
    )
    service, _, _ = _service(
        db,
        accounts=[
            _account(_position("AAPL", market="us", currency="USD"), base_currency="USD")
        ],
        responses={
            "AAPL": (
                _daily_window(
                    (warmup_date, 180.0),
                    (absent_date, 190.0),
                    (conflict_date, 201.0),
                    (latest_date, 210.0),
                ),
                "YfinanceFetcher",
            ),
            "SPY": (_daily_frame(620.0), "YfinanceFetcher"),
        },
    )

    result = service.prepare()

    assert result["items"][0]["status"] == "ready"
    with db.get_session() as session:
        rows = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalars().all()
    assert [(row.date, row.close, row.data_source) for row in rows] == [
        (conflict_date, 200.0, "YfinanceFetcher")
    ]
    with db.get_session() as session:
        evidence = session.execute(
            select(PortfolioMarketEvidenceBar)
            .where(PortfolioMarketEvidenceBar.code == "AAPL")
            .order_by(PortfolioMarketEvidenceBar.date)
        ).scalars().all()
    assert [(row.date, row.close) for row in evidence] == [
        (absent_date, 190.0),
        (conflict_date, 201.0),
        (latest_date, 210.0),
    ]


def test_prepare_never_uses_legacy_cache_to_shorten_frozen_batch(
    db: DatabaseManager,
) -> None:
    manager = StubFetcherManager(
        {"AAPL": (_daily_frame(210.0), "YfinanceFetcher")}
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    first = service._prepare_bar(
        fetch_code="AAPL",
        storage_code="AAPL",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert first["status"] == "ready"
    assert manager.days_by_code["AAPL"] == [261]

    history = _daily_window(
        *[
            (AS_OF - timedelta(days=days_ago), 100.0 + days_ago)
            for days_ago in range(2, 202)
        ]
    )
    db.save_daily_data(
        history,
        code="MSFT",
        data_source="YfinanceFetcher|adjustment=adjusted",
    )
    manager.responses["MSFT"] = (_daily_frame(510.0), "YfinanceFetcher")

    second = service._prepare_bar(
        fetch_code="MSFT",
        storage_code="MSFT",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert second["status"] == "ready"
    assert manager.days_by_code["MSFT"] == [261]


@pytest.mark.parametrize(
    ("provider_name", "code"),
    [("YfinanceFetcher", "AAPL"), ("TencentFetcher", "600519")],
)
def test_prepare_uses_warmup_bar_for_stable_boundary_pct_change(
    db: DatabaseManager,
    provider_name: str,
    code: str,
) -> None:
    warmup_date = AS_OF - timedelta(days=3)
    legacy_date = AS_OF - timedelta(days=2)
    new_date = AS_OF - timedelta(days=1)
    wide = _provider_normalized_window(
        provider_name,
        (warmup_date, 100.0),
        (legacy_date, 110.0),
        (new_date, 121.0),
    )
    short = _provider_normalized_window(
        provider_name,
        (legacy_date, 110.0),
        (new_date, 121.0),
    )
    legacy_mask = pd.to_datetime(wide["date"]).dt.date == legacy_date
    assert float(wide.loc[legacy_mask, "pct_chg"].iloc[0]) == pytest.approx(10.0)
    assert float(short.iloc[0]["pct_chg"]) == 0.0

    older_history = _daily_window(
        *[
            (AS_OF - timedelta(days=days_ago), 50.0 + days_ago)
            for days_ago in range(4, 203)
        ]
    )
    db.save_daily_data(
        older_history,
        code=code,
        data_source=(
            f"{provider_name}|adjustment="
            f"{'adjusted' if provider_name == 'YfinanceFetcher' else 'qfq'}"
        ),
    )
    db.save_daily_data(
        wide.loc[legacy_mask],
        code=code,
        data_source=provider_name,
    )

    class WindowAwareManager:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def get_daily_data(self, stock_code: str, *, days: int):
            assert stock_code == code
            self.calls.append(days)
            return (wide if days == 261 else short), provider_name

    manager = WindowAwareManager()
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code=code,
        storage_code=code,
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert manager.calls == [261]
    assert result["status"] == "ready"
    batch = service.market_evidence_repo.get_batch(result["evidence_batch_hash"])
    stored_new = next((row for row in batch.rows if row.date == new_date), None)
    assert stored_new is not None
    assert stored_new.data_source == provider_name
    assert stored_new.adjustment_identity == (
        "adjusted" if provider_name == "YfinanceFetcher" else "qfq"
    )
    assert all(row.date != warmup_date for row in batch.rows)


@pytest.mark.parametrize(
    ("provider_name", "code"),
    [("YfinanceFetcher", "AAPL"), ("TencentFetcher", "600519")],
)
def test_prepare_fails_closed_when_pct_change_warmup_is_unavailable(
    db: DatabaseManager,
    provider_name: str,
    code: str,
) -> None:
    class SingleBarManager:
        def get_daily_data(self, stock_code: str, *, days: int):
            assert stock_code == code
            return _daily_frame(210.0), provider_name

    manager = SingleBarManager()
    repo = StockRepository(db)
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=repo,
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code=code,
        storage_code=code,
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert result == {
        "status": "insufficient",
        "code": code,
        "blockers": ["position_market_data_unavailable"],
    }
    assert repo.get_latest(code, days=10) == []


def test_prepare_trims_provider_warmup_to_logical_initial_window(
    db: DatabaseManager,
) -> None:
    frame = _daily_window(
        *[
            (AS_OF - timedelta(days=days_ago), 100.0 + days_ago)
            for days_ago in range(261, 0, -1)
        ]
    )
    manager = StubFetcherManager({"AAPL": (frame, "YfinanceFetcher")})
    repo = StockRepository(db)
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=repo,
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code="AAPL",
        storage_code="AAPL",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert result["status"] == "ready"
    assert manager.days_by_code["AAPL"] == [261]
    batch = service.market_evidence_repo.get_batch(result["evidence_batch_hash"])
    rows = list(batch.rows)
    assert len(rows) == 260
    assert rows[0].date == AS_OF - timedelta(days=260)


def test_prepare_rejects_duplicate_fetched_dates_without_writing_batch(
    db: DatabaseManager,
) -> None:
    warmup_date = AS_OF - timedelta(days=2)
    duplicate_date = AS_OF - timedelta(days=1)
    frame = _daily_window(
        (warmup_date, 190.0),
        (duplicate_date, 200.0),
        (duplicate_date, 200.0),
    )
    manager = StubFetcherManager({"AAPL": (frame, "YfinanceFetcher")})
    repo = StockRepository(db)
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=repo,
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code="AAPL",
        storage_code="AAPL",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert result["status"] == "insufficient"
    assert result["blockers"] == ["position_market_data_unavailable"]
    assert repo.get_daily_on_date(code="AAPL", target_date=duplicate_date) is None
    with db.get_session() as session:
        assert session.execute(
            select(func.count()).select_from(PortfolioMarketEvidenceBar)
        ).scalar_one() == 0


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        (field, invalid_value)
        for field in ("open", "high", "low", "close", "volume", "amount", "pct_chg")
        for invalid_value in (None, float("nan"), float("inf"))
    ],
)
def test_prepare_rejects_invalid_new_bar_field_without_writing_batch(
    db: DatabaseManager,
    field: str,
    invalid_value: Any,
) -> None:
    warmup_date = AS_OF - timedelta(days=2)
    target_date = AS_OF - timedelta(days=1)
    frame = _daily_window((warmup_date, 190.0), (target_date, 200.0))
    frame.loc[1, field] = invalid_value
    manager = StubFetcherManager({"AAPL": (frame, "YfinanceFetcher")})
    repo = StockRepository(db)
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=repo,
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code="AAPL",
        storage_code="AAPL",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert result["status"] == "insufficient"
    assert result["blockers"] == ["position_market_data_unavailable"]
    assert repo.get_daily_on_date(code="AAPL", target_date=target_date) is None
    with db.get_session() as session:
        assert session.execute(
            select(func.count()).select_from(PortfolioMarketEvidenceBar)
        ).scalar_one() == 0


def test_prepare_rechecks_changed_overlap_inside_atomic_batch(
    db: DatabaseManager,
) -> None:
    warmup_date = AS_OF - timedelta(days=3)
    missing_date = AS_OF - timedelta(days=2)
    overlap_date = AS_OF - timedelta(days=1)
    expected_source = "YfinanceFetcher|adjustment=adjusted"
    db.save_daily_data(
        _daily_window((overlap_date, 210.0)),
        code="AAPL",
        data_source=expected_source,
    )

    class RacingStockRepository(StockRepository):
        def __init__(self, manager: DatabaseManager) -> None:
            super().__init__(manager)
            self.changed = False

        def insert_missing_dataframe_verified(self, df, code, data_source, **kwargs):
            if not self.changed:
                self.changed = True
                self.db.save_daily_data(
                    _daily_window((overlap_date, 999.0)),
                    code=code,
                    data_source=expected_source,
                )
            return super().insert_missing_dataframe_verified(
                df,
                code,
                data_source,
                **kwargs,
            )

    repo = RacingStockRepository(db)
    manager = StubFetcherManager(
        {
            "AAPL": (
                _daily_window(
                    (warmup_date, 190.0),
                    (missing_date, 200.0),
                    (overlap_date, 210.0),
                ),
                "YfinanceFetcher",
            )
        }
    )
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=repo,
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=manager,
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code="AAPL",
        storage_code="AAPL",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert result["status"] == "ready"
    assert repo.changed is False
    assert repo.get_daily_on_date(code="AAPL", target_date=missing_date) is None
    assert repo.get_daily_on_date(code="AAPL", target_date=overlap_date).close == 210.0
    batch = service.market_evidence_repo.get_batch(result["evidence_batch_hash"])
    assert [(row.date, row.close) for row in batch.rows] == [
        (missing_date, 200.0),
        (overlap_date, 210.0),
    ]


def test_prepare_uses_transaction_verified_snapshot_after_commit(
    db: DatabaseManager,
) -> None:
    warmup_date = AS_OF - timedelta(days=2)
    target_date = AS_OF - timedelta(days=1)
    expected_source = "YfinanceFetcher|adjustment=adjusted"

    class PostCommitMutationRepository(StockRepository):
        def _mutate_target(self, code: str) -> None:
            self.db.save_daily_data(
                _daily_window((target_date, 999.0)),
                code=code,
                data_source="post-commit-mutation",
            )

        def insert_missing_dataframe(self, df, code, data_source, **kwargs):
            result = super().insert_missing_dataframe(
                df,
                code,
                data_source,
                **kwargs,
            )
            self._mutate_target(code)
            return result

        def insert_missing_dataframe_verified(self, df, code, data_source, **kwargs):
            result = super().insert_missing_dataframe_verified(
                df,
                code,
                data_source,
                **kwargs,
            )
            self._mutate_target(code)
            return result

    repo = PostCommitMutationRepository(db)
    service = PortfolioResearchEvidenceService(
        portfolio_service=StubPortfolioService([]),
        stock_repo=repo,
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=StubFetcherManager(
            {
                "AAPL": (
                    _daily_window((warmup_date, 190.0), (target_date, 210.0)),
                    "YfinanceFetcher",
                )
            }
        ),
        as_of_provider=lambda: AS_OF,
    )

    result = service._prepare_bar(
        fetch_code="AAPL",
        storage_code="AAPL",
        as_of=AS_OF,
        blocker_prefix="position",
    )

    assert {
        key: result[key]
        for key in (
            "status",
            "code",
            "date",
            "close",
            "data_source",
            "source",
            "adjustment",
            "blockers",
        )
    } == {
        "status": "ready",
        "code": "AAPL",
        "date": target_date.isoformat(),
        "close": 210.0,
        "data_source": expected_source,
        "source": "YfinanceFetcher",
        "adjustment": "adjusted",
        "blockers": [],
    }
    assert repo.get_daily_on_date(code="AAPL", target_date=target_date) is None
