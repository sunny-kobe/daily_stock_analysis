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


class StubPortfolioService:
    def __init__(self, accounts: list[Dict[str, Any]]) -> None:
        self.accounts = accounts
        self.calls: list[Dict[str, Any]] = []

    def get_portfolio_snapshot(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"accounts": self.accounts}


class StubFetcherManager:
    def __init__(self, responses: Dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_daily_data(self, code: str, *, days: int) -> Any:
        self.calls.append(code)
        result = self.responses[code]
        if isinstance(result, Exception):
            raise result
        return result


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
    service = PortfolioResearchEvidenceService(
        portfolio_service=portfolio_service,
        stock_repo=StockRepository(db),
        portfolio_repo=PortfolioRepository(db),
        fetcher_manager=fetcher_manager,
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
    assert fetcher.calls == ["600519", "000300"]
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
    assert rows["000300"].data_source == "AkshareFetcher|adjustment=qfq"


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
    assert fetcher.calls == ["600519", "000300", "AAPL", "SPY"]
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
    assert fetcher.calls == ["HK00700", "^HSI", "HK09988"]
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
                _daily_window((older, 201.0), (latest, 210.0)),
                "YfinanceFetcher",
            ),
            "MSFT": (
                _daily_window((older, 500.0), (latest, 510.0)),
                "YfinanceFetcher",
            ),
            "SPY": (
                _daily_window((older, 610.0), (latest, 620.0)),
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
