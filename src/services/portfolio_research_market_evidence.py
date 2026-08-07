# -*- coding: utf-8 -*-
"""Market-session validation shared by frozen portfolio research evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.core.trading_calendar import (
    MARKET_TIMEZONE,
    get_effective_trading_date,
    resolve_market_daily_bar_as_of,
)


def daily_bar_not_final(*, market: str, as_of: datetime, cutoff: datetime) -> bool:
    """Fail closed unless ``as_of`` is a completed exchange daily session."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    timezone_name = MARKET_TIMEZONE.get(str(market or "").strip().lower())
    if not timezone_name:
        return True
    as_of_utc = as_of.astimezone(timezone.utc)
    cutoff_utc = cutoff.astimezone(timezone.utc)
    session_date = as_of.astimezone(ZoneInfo(timezone_name)).date()
    expected_close = resolve_market_daily_bar_as_of(market, session_date)
    if expected_close is None:
        return True
    expected_close_utc = expected_close.astimezone(timezone.utc)
    return as_of_utc != expected_close_utc or expected_close_utc > cutoff_utc


def daily_bar_stale(*, market: str, as_of: datetime, cutoff: datetime) -> bool:
    """Return whether a completed bar predates the session required at ``cutoff``."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    normalized_market = str(market or "").strip().lower()
    timezone_name = MARKET_TIMEZONE.get(normalized_market)
    if not timezone_name:
        return True
    bar_session = as_of.astimezone(ZoneInfo(timezone_name)).date()
    expected_session = get_effective_trading_date(
        normalized_market,
        current_time=cutoff,
    )
    return bar_session != expected_session
