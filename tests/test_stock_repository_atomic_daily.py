# -*- coding: utf-8 -*-
"""Focused tests for atomic insert-only daily market evidence."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import update

from src.repositories.stock_repo import DailyBarInsertConflict, StockRepository
from src.storage import DatabaseManager, StockDaily


AS_OF = date(2026, 7, 31)


def _daily_frame(*rows: tuple[date, float]) -> pd.DataFrame:
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


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'atomic_daily.db'}")
    yield manager
    DatabaseManager.reset_instance()


def test_insert_missing_dataframe_chunks_260_rows_under_999_bind_limit(
    db: DatabaseManager,
) -> None:
    raw_connection = db._engine.raw_connection()
    try:
        raw_connection.driver_connection.setlimit(
            sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
            999,
        )
    finally:
        raw_connection.close()
    frame = _daily_frame(
        *[
            (AS_OF - timedelta(days=days_ago), 100.0 + days_ago)
            for days_ago in range(260, 0, -1)
        ]
    )
    repo = StockRepository(db)

    inserted = repo.insert_missing_dataframe(
        frame,
        code="AAPL",
        data_source="YfinanceFetcher|adjustment=adjusted",
    )

    assert inserted == 260
    assert len(repo.get_range("AAPL", AS_OF - timedelta(days=300), AS_OF)) == 260


def test_insert_missing_dataframe_validates_existing_and_inserts_missing_atomically(
    db: DatabaseManager,
) -> None:
    existing_date = AS_OF - timedelta(days=2)
    missing_date = AS_OF - timedelta(days=1)
    db.save_daily_data(
        _daily_frame((existing_date, 200.0)),
        code="AAPL",
        data_source="YfinanceFetcher",
    )
    repo = StockRepository(db)

    inserted = repo.insert_missing_dataframe(
        _daily_frame((existing_date, 200.0), (missing_date, 210.0)),
        code="AAPL",
        data_source="YfinanceFetcher|adjustment=adjusted",
        existing_row_matches=lambda row, source_row: (
            row.data_source == "YfinanceFetcher"
            and row.close == float(source_row["close"])
        ),
    )

    assert inserted == 1
    assert repo.get_daily_on_date(
        code="AAPL",
        target_date=existing_date,
    ).data_source == "YfinanceFetcher"
    assert repo.get_daily_on_date(
        code="AAPL",
        target_date=missing_date,
    ).data_source == "YfinanceFetcher|adjustment=adjusted"


def test_insert_missing_dataframe_conflict_rolls_back_entire_batch(
    db: DatabaseManager,
) -> None:
    absent_date = AS_OF - timedelta(days=2)
    existing_date = AS_OF - timedelta(days=1)
    db.save_daily_data(
        _daily_frame((existing_date, 210.0)),
        code="AAPL",
        data_source="original-source",
    )
    repo = StockRepository(db)
    before = repo.get_daily_on_date(code="AAPL", target_date=existing_date)
    before_values = (
        before.close,
        before.data_source,
        before.created_at,
        before.updated_at,
    )

    with pytest.raises(DailyBarInsertConflict, match="daily bar insert conflict"):
        repo.insert_missing_dataframe(
            _daily_frame((absent_date, 200.0), (existing_date, 999.0)),
            code="AAPL",
            data_source="replacement-source",
            existing_row_matches=lambda row, source_row: False,
        )

    existing = repo.get_daily_on_date(code="AAPL", target_date=existing_date)
    assert (
        existing.close,
        existing.data_source,
        existing.created_at,
        existing.updated_at,
    ) == before_values
    assert repo.get_daily_on_date(code="AAPL", target_date=absent_date) is None


def test_insert_missing_dataframe_rolls_back_query_back_mismatch(
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_date = AS_OF - timedelta(days=2)
    second_date = AS_OF - timedelta(days=1)
    original_run_transaction = db._run_write_transaction

    def run_with_persistence_corruption(operation_name, operation):
        def wrapped(real_session):
            class CorruptingSession:
                def __getattr__(self, name):
                    return getattr(real_session, name)

                def execute(self, statement, *args, **kwargs):
                    result = real_session.execute(statement, *args, **kwargs)
                    if getattr(statement, "is_insert", False):
                        real_session.execute(
                            update(StockDaily)
                            .where(StockDaily.code == "AAPL")
                            .values(close=999.0, data_source="corrupted-source")
                        )
                    return result

            return operation(CorruptingSession())

        return original_run_transaction(operation_name, wrapped)

    monkeypatch.setattr(db, "_run_write_transaction", run_with_persistence_corruption)
    repo = StockRepository(db)

    with pytest.raises(DailyBarInsertConflict, match="daily bar insert conflict"):
        repo.insert_missing_dataframe(
            _daily_frame((first_date, 200.0), (second_date, 210.0)),
            code="AAPL",
            data_source="YfinanceFetcher|adjustment=adjusted",
        )

    assert repo.get_daily_on_date(code="AAPL", target_date=first_date) is None
    assert repo.get_daily_on_date(code="AAPL", target_date=second_date) is None


def test_insert_missing_dataframe_rolls_back_prior_chunks_on_late_conflict(
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = [AS_OF - timedelta(days=days_ago) for days_ago in range(51, 0, -1)]
    frame = _daily_frame(*[(bar_date, 200.0 + index) for index, bar_date in enumerate(dates)])
    conflict_date = dates[-1]
    original_run_transaction = db._run_write_transaction

    def run_with_second_chunk_conflict(operation_name, operation):
        def wrapped(real_session):
            class ConflictingSession:
                insert_count = 0

                def __getattr__(self, name):
                    return getattr(real_session, name)

                def execute(self, statement, *args, **kwargs):
                    if getattr(statement, "is_insert", False):
                        self.insert_count += 1
                        if self.insert_count == 2:
                            real_session.execute(
                                StockDaily.__table__.insert().values(
                                    code="AAPL",
                                    date=conflict_date,
                                    open=999.0,
                                    high=999.0,
                                    low=999.0,
                                    close=999.0,
                                    volume=100.0,
                                    amount=99900.0,
                                    pct_chg=0.0,
                                    data_source="conflicting-source",
                                )
                            )
                    return real_session.execute(statement, *args, **kwargs)

            return operation(ConflictingSession())

        return original_run_transaction(operation_name, wrapped)

    monkeypatch.setattr(db, "_run_write_transaction", run_with_second_chunk_conflict)
    repo = StockRepository(db)

    with pytest.raises(DailyBarInsertConflict, match="daily bar insert conflict"):
        repo.insert_missing_dataframe(
            frame,
            code="AAPL",
            data_source="YfinanceFetcher|adjustment=adjusted",
        )

    assert repo.get_range("AAPL", dates[0], dates[-1]) == []
