# -*- coding: utf-8 -*-
"""Persistence boundary for immutable portfolio market-evidence batches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from typing import Any

import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from src.storage import (
    DatabaseManager,
    PortfolioMarketEvidenceBar,
    to_utc_naive_datetime,
)


_REQUIRED_FIELDS = ("open", "high", "low", "close", "volume", "amount", "pct_chg")
_OPTIONAL_FIELDS = ("ma5", "ma10", "ma20", "volume_ratio")


@dataclass(frozen=True)
class PortfolioMarketEvidenceBatch:
    batch_hash: str
    code: str
    data_source: str
    source_version: str
    adjustment_identity: str
    captured_at: datetime
    rows: tuple[PortfolioMarketEvidenceBar, ...]
    inserted_count: int = 0


class PortfolioMarketEvidenceRepository:
    """Append and resolve content-addressed evidence batches."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def append_batch(
        self,
        frame: pd.DataFrame,
        *,
        code: str,
        data_source: str,
        source_version: str,
        adjustment_identity: str,
        captured_at: datetime,
    ) -> PortfolioMarketEvidenceBatch:
        metadata = self._validate_metadata(
            code=code,
            data_source=data_source,
            source_version=source_version,
            adjustment_identity=adjustment_identity,
            captured_at=captured_at,
        )
        records = self._normalize_records(frame)
        batch_payload = {**metadata, "rows": records}
        batch_hash = self._sha256(batch_payload)

        values = []
        for record in records:
            bar_payload = {**metadata, **record}
            values.append(
                {
                    "batch_hash": batch_hash,
                    "bar_hash": self._sha256(bar_payload),
                    **bar_payload,
                }
            )

        with self.db.get_session() as session:
            existing = self._rows_for_batch(session, batch_hash)
            if existing:
                self._assert_complete(existing, values)
                return self._batch(existing, inserted_count=0)

            session.add_all(PortfolioMarketEvidenceBar(**value) for value in values)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._rows_for_batch(session, batch_hash)
                if not existing:
                    raise
                self._assert_complete(existing, values)
                return self._batch(existing, inserted_count=0)

            inserted = self._rows_for_batch(session, batch_hash)
            self._assert_complete(inserted, values)
            return self._batch(inserted, inserted_count=len(inserted))

    def get_batch(self, batch_hash: str) -> PortfolioMarketEvidenceBatch | None:
        normalized_hash = str(batch_hash or "").strip().lower()
        if len(normalized_hash) != 64:
            return None
        with self.db.get_session() as session:
            rows = self._rows_for_batch(session, normalized_hash)
            return self._batch(rows) if rows else None

    def get_latest_batch(
        self,
        *,
        code: str,
        cutoff: datetime,
        target_date: date | None = None,
        source_version: str | None = None,
        data_source: str | None = None,
    ) -> PortfolioMarketEvidenceBatch | None:
        cutoff_value = to_utc_naive_datetime(cutoff)
        filters = [
            PortfolioMarketEvidenceBar.code == str(code).strip(),
            PortfolioMarketEvidenceBar.captured_at <= cutoff_value,
        ]
        if target_date is not None:
            filters.append(PortfolioMarketEvidenceBar.date == target_date)
        if source_version is not None:
            filters.append(
                PortfolioMarketEvidenceBar.source_version == str(source_version).strip()
            )
        if data_source is not None:
            filters.append(
                PortfolioMarketEvidenceBar.data_source == str(data_source).strip()
            )
        with self.db.get_session() as session:
            batch_hash = session.execute(
                select(PortfolioMarketEvidenceBar.batch_hash)
                .where(*filters)
                .group_by(PortfolioMarketEvidenceBar.batch_hash)
                .order_by(
                    desc(func.max(PortfolioMarketEvidenceBar.captured_at)),
                    desc(PortfolioMarketEvidenceBar.batch_hash),
                )
                .limit(1)
            ).scalar_one_or_none()
            if batch_hash is None:
                return None
            rows = self._rows_for_batch(session, batch_hash)
            return self._batch(rows)

    @staticmethod
    def _validate_metadata(**values: Any) -> dict[str, Any]:
        normalized = {
            "code": str(values["code"] or "").strip(),
            "data_source": str(values["data_source"] or "").strip(),
            "source_version": str(values["source_version"] or "").strip(),
            "adjustment_identity": str(values["adjustment_identity"] or "").strip(),
            "captured_at": to_utc_naive_datetime(values["captured_at"]),
        }
        if not all(normalized.values()) or not isinstance(normalized["captured_at"], datetime):
            raise ValueError("invalid market evidence batch")
        return normalized

    @staticmethod
    def _normalize_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
        if frame is None or frame.empty:
            raise ValueError("invalid market evidence batch")

        normalized: list[dict[str, Any]] = []
        seen_dates: set[date] = set()
        for raw in frame.to_dict(orient="records"):
            raw_date = raw.get("date")
            if isinstance(raw_date, pd.Timestamp):
                row_date = raw_date.date()
            elif isinstance(raw_date, datetime):
                row_date = raw_date.date()
            elif isinstance(raw_date, date):
                row_date = raw_date
            elif isinstance(raw_date, str):
                try:
                    row_date = date.fromisoformat(raw_date)
                except ValueError as exc:
                    raise ValueError("invalid market evidence batch") from exc
            else:
                raise ValueError("invalid market evidence batch")
            if row_date in seen_dates:
                raise ValueError("invalid market evidence batch")
            seen_dates.add(row_date)

            row: dict[str, Any] = {"date": row_date}
            for field in _REQUIRED_FIELDS:
                row[field] = PortfolioMarketEvidenceRepository._finite_float(raw.get(field))
            for field in _OPTIONAL_FIELDS:
                value = raw.get(field)
                row[field] = None if pd.isna(value) else PortfolioMarketEvidenceRepository._finite_float(value)
            normalized.append(row)

        normalized.sort(key=lambda item: item["date"])
        return normalized

    @staticmethod
    def _finite_float(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid market evidence batch") from exc
        if not math.isfinite(number):
            raise ValueError("invalid market evidence batch")
        return number

    @staticmethod
    def _sha256(payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=lambda value: value.isoformat(),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _rows_for_batch(session, batch_hash: str) -> list[PortfolioMarketEvidenceBar]:
        return list(
            session.execute(
                select(PortfolioMarketEvidenceBar)
                .where(PortfolioMarketEvidenceBar.batch_hash == batch_hash)
                .order_by(PortfolioMarketEvidenceBar.date)
            ).scalars().all()
        )

    @staticmethod
    def _assert_complete(
        rows: list[PortfolioMarketEvidenceBar],
        expected: list[dict[str, Any]],
    ) -> None:
        expected_by_date = {value["date"]: value for value in expected}
        if len(rows) != len(expected_by_date):
            raise ValueError("invalid market evidence batch")
        for row in rows:
            values = expected_by_date.get(row.date)
            if values is None or any(getattr(row, key) != value for key, value in values.items()):
                raise ValueError("invalid market evidence batch")

    @staticmethod
    def _batch(
        rows: list[PortfolioMarketEvidenceBar],
        inserted_count: int = 0,
    ) -> PortfolioMarketEvidenceBatch:
        first = rows[0]
        return PortfolioMarketEvidenceBatch(
            batch_hash=first.batch_hash,
            code=first.code,
            data_source=first.data_source,
            source_version=first.source_version,
            adjustment_identity=first.adjustment_identity,
            captured_at=first.captured_at,
            rows=tuple(rows),
            inserted_count=inserted_count,
        )
