# -*- coding: utf-8 -*-
"""Tests for immutable, versioned portfolio market evidence."""

from __future__ import annotations

from datetime import date, datetime, timezone
import importlib
import sqlite3

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.storage import DatabaseManager, PortfolioMarketEvidenceBar


@pytest.fixture(autouse=True)
def _reset_database_manager() -> None:
    DatabaseManager.reset_instance()
    yield
    DatabaseManager.reset_instance()


def _frame(*rows: tuple[date, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": bar_date,
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1000.0,
                "amount": close * 1000,
                "pct_chg": 1.0,
                "ma5": close - 0.5,
                "ma10": close - 1,
                "ma20": close - 2,
                "volume_ratio": 1.1,
            }
            for bar_date, close in rows
        ]
    )


def _repo(db: DatabaseManager):
    module = importlib.import_module(
        "src.repositories.portfolio_market_evidence_repo"
    )
    return module.PortfolioMarketEvidenceRepository(db)


def test_database_initialization_creates_portfolio_market_evidence_table(tmp_path) -> None:
    db_path = tmp_path / "market_evidence.db"

    DatabaseManager.reset_instance()
    try:
        DatabaseManager(db_url=f"sqlite:///{db_path}")
        with sqlite3.connect(db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    finally:
        DatabaseManager.reset_instance()

    assert "portfolio_market_evidence_bars" in tables


def test_append_batch_is_idempotent_and_revised_same_date_coexists(tmp_path) -> None:
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'market_evidence.db'}")
    repo = _repo(db)
    captured_at = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    original = _frame(
        (date(2026, 7, 30), 100.0),
        (date(2026, 7, 31), 101.0),
    )

    first = repo.append_batch(
        original,
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=captured_at,
    )
    repeated = repo.append_batch(
        original,
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=captured_at,
    )
    revised = repo.append_batch(
        _frame(
            (date(2026, 7, 30), 100.0),
            (date(2026, 7, 31), 102.0),
        ),
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
    )

    assert first.batch_hash == repeated.batch_hash
    assert revised.batch_hash != first.batch_hash
    assert repeated.inserted_count == 0
    with db.get_session() as session:
        assert session.scalar(
            select(func.count()).select_from(PortfolioMarketEvidenceBar)
        ) == 4


def test_get_latest_batch_respects_capture_cutoff(tmp_path) -> None:
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'market_evidence.db'}")
    repo = _repo(db)
    first = repo.append_batch(
        _frame((date(2026, 7, 31), 101.0)),
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
    )
    repo.append_batch(
        _frame((date(2026, 7, 31), 102.0)),
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
    )

    selected = repo.get_latest_batch(
        code="AAPL",
        cutoff=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
    )

    assert selected is not None
    assert selected.batch_hash == first.batch_hash
    assert selected.rows[-1].close == 101.0


def test_get_latest_batch_filters_before_choosing_latest_capture(tmp_path) -> None:
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'market_evidence.db'}")
    repo = _repo(db)
    eligible = repo.append_batch(
        _frame((date(2026, 7, 31), 101.0)),
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
    )
    repo.append_batch(
        _frame((date(2026, 7, 30), 99.0)),
        code="AAPL",
        data_source="OtherFetcher",
        source_version="portfolio-research-evidence-prepare-v1",
        adjustment_identity="unknown",
        captured_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
    )

    selected = repo.get_latest_batch(
        code="AAPL",
        cutoff=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
        target_date=date(2026, 7, 31),
        source_version="portfolio-research-evidence-prepare-v2",
        data_source="YfinanceFetcher",
    )

    assert selected is not None
    assert selected.batch_hash == eligible.batch_hash


def test_append_batch_rejects_duplicate_dates_and_non_finite_values(tmp_path) -> None:
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'market_evidence.db'}")
    repo = _repo(db)
    duplicated = _frame(
        (date(2026, 7, 31), 101.0),
        (date(2026, 7, 31), 101.0),
    )
    invalid = _frame((date(2026, 7, 31), 101.0))
    invalid.loc[0, "close"] = float("nan")

    for frame in (duplicated, invalid):
        with pytest.raises(ValueError, match="invalid market evidence batch"):
            repo.append_batch(
                frame,
                code="AAPL",
                data_source="YfinanceFetcher",
                source_version="portfolio-research-evidence-prepare-v2",
                adjustment_identity="adjusted",
                captured_at=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
            )

    with db.get_session() as session:
        assert session.scalar(
            select(func.count()).select_from(PortfolioMarketEvidenceBar)
        ) == 0


def test_market_evidence_rows_are_immutable_in_sqlite(tmp_path) -> None:
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'market_evidence.db'}")
    batch = _repo(db).append_batch(
        _frame((date(2026, 7, 31), 101.0)),
        code="AAPL",
        data_source="YfinanceFetcher",
        source_version="portfolio-research-evidence-prepare-v2",
        adjustment_identity="adjusted",
        captured_at=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IntegrityError, match="portfolio_market_evidence_immutable"):
        with db.get_session() as session:
            row = session.get(PortfolioMarketEvidenceBar, batch.rows[0].id)
            row.close = 999.0
            session.commit()

    with pytest.raises(IntegrityError, match="portfolio_market_evidence_immutable"):
        with db.get_session() as session:
            row = session.get(PortfolioMarketEvidenceBar, batch.rows[0].id)
            session.delete(row)
            session.commit()
