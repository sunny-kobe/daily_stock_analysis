# -*- coding: utf-8 -*-
"""Focused tests for bounded current portfolio evidence preparation."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import pytest
from sqlalchemy import func, select

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
    PortfolioPosition,
    PortfolioRiskPolicy,
    PortfolioStrategyTransitionRecord,
    PortfolioStrategyValidationRunRecord,
    PortfolioStrategyVersionRecord,
    PortfolioTrade,
    StockDaily,
)


AS_OF = date(2026, 7, 31)


def _daily_frame(
    close: float = 100.0,
    *,
    bar_date: date = AS_OF - timedelta(days=1),
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
                "pct_chg": 0.0,
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

    def get_daily_data(self, code: str, *, days: int) -> Any:
        self.calls.append(code)
        self.days_by_code.setdefault(code, []).append(days)
        result = self.responses[code]
        if isinstance(result, Exception):
            raise result
        frame, source = result
        return _with_provider_warmup(frame, source), source


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
) -> tuple[PortfolioResearchEvidenceService, StubPortfolioService, StubFetcherManager]:
    portfolio_service = StubPortfolioService(accounts)
    fetcher_manager = StubFetcherManager(responses)
    tencent_fetcher = StubDirectFetcher("TencentFetcher", responses)
    yfinance_fetcher = StubDirectFetcher("YfinanceFetcher", responses)
    service = PortfolioResearchEvidenceService(
        portfolio_service=portfolio_service,
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=fetcher_manager,
        tencent_benchmark_fetcher=tencent_fetcher,
        yfinance_benchmark_fetcher=yfinance_fetcher,
        as_of_provider=lambda: AS_OF,
        fx_fetcher=fx_fetcher,
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
            for row in session.execute(select(StockDaily)).scalars().all()
        }
    assert rows["600519"].data_source == "EfinanceFetcher|adjustment=qfq"
    assert rows["000300"].data_source == "TencentFetcher|adjustment=qfq"


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
            for row in session.execute(select(StockDaily)).scalars().all()
        }
    assert fx_row.source == "test-fx@1"
    assert fx_row.is_stale is False
    assert sources == {
        "AAPL": "YfinanceFetcher|adjustment=adjusted",
        "SPY": "YfinanceFetcher|adjustment=adjusted",
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
            select(StockDaily).where(StockDaily.code == "600519")
        ).scalar_one()
    assert unknown.data_source == "MysteryFetcher|adjustment=unknown"


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
            select(StockDaily).where(StockDaily.code == "HSI")
        ).scalar_one()
    assert benchmark.data_source == "YfinanceFetcher|adjustment=adjusted"


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
    assert item["status"] == "insufficient"
    assert "position_existing_bar_conflict" in item["blockers"]
    with db.get_session() as session:
        existing = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalar_one()
    assert existing.close == 200.0
    assert existing.data_source == "LegacyFetcher"


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
    assert item["status"] == "insufficient"
    assert "position_existing_bar_conflict" in item["blockers"]
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
    assert items["AAPL"]["status"] == "insufficient"
    assert "position_existing_bar_conflict" in items["AAPL"]["blockers"]
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
    assert [(row.date, row.close) for row in msft_rows] == [
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
        ("cn", _position("600519", market="cn", currency="CNY"), "000300", "sh000300", "TencentFetcher", "qfq"),
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
        tencent_benchmark_fetcher=tencent,
        yfinance_benchmark_fetcher=yahoo,
        as_of_provider=lambda: AS_OF,
    )

    result = service.prepare()

    assert result["items"][0]["benchmark"]["data_source"] == (
        f"{provider_name}|adjustment={adjustment}"
    )
    assert manager.calls == [position["symbol"]]
    routed = tencent if market == "cn" else yahoo
    assert routed.calls == [(fetch_code, 261)]
    with db.get_session() as session:
        row = session.execute(
            select(StockDaily).where(StockDaily.code == storage_code)
        ).scalar_one()
    assert row.data_source == f"{provider_name}|adjustment={adjustment}"


def test_prepare_fixed_benchmark_failure_does_not_fallback_to_manager(
    db: DatabaseManager,
) -> None:
    manager = StubFetcherManager(
        {
            "600519": (_daily_frame(150.0), "TencentFetcher"),
            "000300": (_daily_frame(4000.0), "AkshareFetcher"),
        }
    )
    tencent = StubDirectFetcher(
        "TencentFetcher",
        {"sh000300": RuntimeError("fixed provider unavailable")},
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

    result = service.prepare()

    assert result["items"][0]["benchmark"] == {
        "status": "insufficient",
        "code": "000300",
    }
    assert "benchmark_market_data_unavailable" in result["items"][0]["blockers"]
    assert manager.calls == ["600519"]
    assert tencent.calls == [("sh000300", 261)]


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
            select(StockDaily)
            .where(StockDaily.code == "AAPL")
            .order_by(StockDaily.date)
        ).scalars().all()
    assert len(rows) == 2
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
    assert rows[1].data_source == "YfinanceFetcher|adjustment=adjusted"


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

    assert result["items"][0]["price"]["status"] == "insufficient"
    assert "position_adjustment_identity_unknown" in result["items"][0]["blockers"]


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

    assert "position_existing_bar_conflict" in result["items"][0]["blockers"]


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

    assert "position_existing_bar_conflict" in result["items"][0]["blockers"]


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

    assert "position_existing_bar_conflict" in result["items"][0]["blockers"]
    with db.get_session() as session:
        rows = session.execute(
            select(StockDaily).where(StockDaily.code == "AAPL")
        ).scalars().all()
    assert [(row.date, row.close, row.data_source) for row in rows] == [
        (conflict_date, 200.0, "YfinanceFetcher")
    ]


def test_prepare_uses_initial_then_short_incremental_lookback(db: DatabaseManager) -> None:
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
    assert manager.days_by_code["MSFT"] == [11]


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
            return (wide if days == 11 else short), provider_name

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

    assert manager.calls == [11]
    assert result["status"] == "ready"
    stored_new = StockRepository(db).get_daily_on_date(
        code=code,
        target_date=new_date,
    )
    assert stored_new is not None
    assert stored_new.data_source == (
        f"{provider_name}|adjustment="
        f"{'adjusted' if provider_name == 'YfinanceFetcher' else 'qfq'}"
    )
    assert StockRepository(db).get_daily_on_date(
        code=code,
        target_date=warmup_date,
    ) is None


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
    rows = repo.get_range(
        "AAPL",
        AS_OF - timedelta(days=400),
        AS_OF,
    )
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
    assert result["blockers"] == ["position_existing_bar_conflict"]
    assert repo.get_daily_on_date(code="AAPL", target_date=duplicate_date) is None


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
    assert result["blockers"] == ["position_existing_bar_conflict"]
    assert repo.get_daily_on_date(code="AAPL", target_date=target_date) is None


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

    assert result["status"] == "insufficient"
    assert result["blockers"]
    assert repo.get_daily_on_date(code="AAPL", target_date=missing_date) is None
    assert repo.get_daily_on_date(code="AAPL", target_date=overlap_date).close == 999.0


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

    assert result == {
        "status": "ready",
        "code": "AAPL",
        "date": target_date.isoformat(),
        "close": 210.0,
        "data_source": expected_source,
        "source": "YfinanceFetcher",
        "adjustment": "adjusted",
        "blockers": [],
    }
    assert repo.get_daily_on_date(code="AAPL", target_date=target_date).close == 999.0
