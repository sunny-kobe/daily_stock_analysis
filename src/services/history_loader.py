"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)
_cache_read_only: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_cache_read_only", default=False,
)


@dataclass(frozen=True)
class FrozenMarketEvidence:
    code: str
    batch_hash: str


_frozen_market_evidence: contextvars.ContextVar[Optional[FrozenMarketEvidence]] = (
    contextvars.ContextVar("_frozen_market_evidence", default=None)
)


def set_frozen_target_date(d: date) -> contextvars.Token:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token) -> None:
    _frozen_target_date.reset(token)


def set_cache_read_only(value: bool = True) -> contextvars.Token:
    return _cache_read_only.set(bool(value))


def is_cache_read_only() -> bool:
    return _cache_read_only.get()


def reset_cache_read_only(token: contextvars.Token) -> None:
    _cache_read_only.reset(token)


def set_frozen_market_evidence(
    *,
    code: str,
    batch_hash: str,
) -> contextvars.Token:
    normalized_hash = str(batch_hash or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized_hash) is None:
        raise ValueError("invalid frozen market evidence batch hash")
    return _frozen_market_evidence.set(
        FrozenMarketEvidence(code=str(code or "").strip(), batch_hash=normalized_hash)
    )


def get_frozen_market_evidence() -> Optional[FrozenMarketEvidence]:
    return _frozen_market_evidence.get()


def reset_frozen_market_evidence(token: contextvars.Token) -> None:
    _frozen_market_evidence.reset(token)


def get_frozen_research_snapshot_cutoff(
    portfolio_context: Any,
) -> Optional[datetime]:
    """Return the aware cutoff from a valid frozen research snapshot envelope."""
    if not isinstance(portfolio_context, Mapping):
        return None
    envelope = portfolio_context.get("_frozen_research_snapshot")
    if not isinstance(envelope, Mapping):
        return None

    from src.services.portfolio_research_snapshot_service import (
        RESEARCH_SNAPSHOT_SCHEMA_VERSION,
    )

    if envelope.get("schema_version") != RESEARCH_SNAPSHOT_SCHEMA_VERSION:
        return None
    snapshot_hash = str(envelope.get("snapshot_hash") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", snapshot_hash) is None:
        return None
    cutoff = str(envelope.get("cutoff") or "").strip()
    try:
        parsed_cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_cutoff.tzinfo is None:
        return None
    return parsed_cutoff


def is_frozen_research_snapshot_context(portfolio_context: Any) -> bool:
    """Return whether context carries a valid frozen research snapshot envelope."""
    return get_frozen_research_snapshot_cutoff(portfolio_context) is not None


def get_bound_market_evidence_identity(
    portfolio_context: Any,
    stock_code: str,
) -> Optional[FrozenMarketEvidence]:
    """Resolve one unambiguous position batch from a valid frozen snapshot."""
    if get_frozen_research_snapshot_cutoff(portfolio_context) is None:
        return None
    envelope = portfolio_context.get("_frozen_research_snapshot")
    positions = envelope.get("positions") if isinstance(envelope, Mapping) else None
    if not isinstance(positions, list):
        return None
    candidates, normalized_code = _history_code_candidates(stock_code)
    accepted_codes = {str(item).strip().upper() for item in candidates}
    accepted_codes.add(str(normalized_code or "").strip().upper())
    matches: list[FrozenMarketEvidence] = []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        code = str(position.get("symbol") or "").strip()
        if code.upper() not in accepted_codes:
            continue
        batch_hash = str(
            position.get("history_evidence_batch_hash")
            or position.get("price_evidence_batch_hash")
            or ""
        ).strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", batch_hash) is None:
            return None
        matches.append(FrozenMarketEvidence(code=code, batch_hash=batch_hash))
    identities = {(item.code.upper(), item.batch_hash) for item in matches}
    if len(identities) != 1:
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def _filter_history_frame_to_end(
    df: pd.DataFrame,
    end: date,
) -> pd.DataFrame:
    frame = df
    date_column = next(
        (column for column in frame.columns if str(column).lower() == "date"),
        None,
    )
    if date_column is not None:
        raw_dates = frame[date_column].tolist()
    elif isinstance(frame.index, pd.DatetimeIndex):
        raw_dates = list(frame.index)
    else:
        logger.warning("Provider history frame has no usable date axis")
        return frame.iloc[0:0].copy()

    in_range = []
    for value in raw_dates:
        bar_date = _coerce_bar_date(value)
        in_range.append(bar_date != date.min and bar_date <= end)
    if all(in_range):
        return frame
    return frame.loc[in_range].copy()


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback.  Returns ``(None, "none")`` when
    both paths fail.
    """
    # Resolve effective end date
    if target_date is not None:
        end = target_date
    else:
        frozen = get_frozen_target_date()
        end = frozen if frozen else date.today()

    # Calendar-day buffer: ~1.8x trading days + margin for long holidays
    start = end - timedelta(days=int(days * 1.8) + 10)

    frozen_evidence = get_frozen_market_evidence()
    if frozen_evidence is not None:
        try:
            candidates, normalized_code = _history_code_candidates(stock_code)
            accepted_codes = {str(item).strip().upper() for item in candidates}
            accepted_codes.add(str(normalized_code or "").strip().upper())
            if frozen_evidence.code.strip().upper() not in accepted_codes:
                return None, "none"
            from src.repositories.portfolio_market_evidence_repo import (
                PortfolioMarketEvidenceRepository,
            )

            batch = PortfolioMarketEvidenceRepository().get_batch(
                frozen_evidence.batch_hash
            )
            if (
                batch is None
                or batch.batch_hash != frozen_evidence.batch_hash
                or str(batch.code).strip().upper() != frozen_evidence.code.strip().upper()
            ):
                return None, "none"
            rows = [
                row
                for row in batch.rows
                if start <= _bar_date(row) <= end
            ]
            if not rows:
                return None, "none"
            frame = pd.DataFrame([row.to_dict() for row in rows]).tail(days)
            return frame, "portfolio_market_evidence"
        except Exception as exc:
            logger.warning(
                "load_history_df(%s): frozen market evidence read failed: %s",
                stock_code,
                exc,
            )
            return None, "none"

    from src.storage import get_db

    # --- 1. DB lookup (canonical code, then prefix-stripped fallback) ------
    try:
        db = get_db()
        _code, bars = _select_best_bars(db, stock_code, start, end)
        required_records = max(min(days, _CACHE_MIN_RECORDS), 1)
        latest_date = max((_bar_date(bar) for bar in bars), default=date.min)
        if bars and latest_date >= end and len(bars) >= required_records:
            df = pd.DataFrame([b.to_dict() for b in bars])
            logger.debug(
                "load_history_df(%s): %d bars from DB (requested %d)",
                stock_code, len(df), days,
            )
            return df, "db_cache"
    except Exception as e:
        logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    # --- 2. Network fallback via singleton DataFetcherManager -------------
    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(stock_code, days=days)
        if df is not None and not df.empty:
            bounded = _filter_history_frame_to_end(df, end)
            if not bounded.empty:
                return bounded, source
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    return None, "none"
