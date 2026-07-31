# -*- coding: utf-8 -*-
"""Registry-driven portfolio analysis policy compatibility adapter."""

from __future__ import annotations

from typing import Any, Dict, Optional

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.core.trading_calendar import get_market_for_stock
from src.repositories.portfolio_repo import PortfolioRepository


_PROFILE_BY_TYPE = {
    "equity": "equity_standard",
    "etf": "etf_structure",
    "qdii": "qdii_premium",
    "adr_ads": "adr_parity",
    "daily_leveraged_product": "leveraged_product_precision",
}
_REQUIRED_CHECKS_BY_TYPE = {
    "equity": [],
    "etf": [],
    "qdii": ["nav_premium"],
    "adr_ads": ["adr_parity"],
    "daily_leveraged_product": ["underlying", "daily_reset", "lot_size"],
}
_LEVERAGED_PRODUCT_SKILLS = [
    "leveraged_product_risk",
    "event_driven",
    "expectation_repricing",
    "bull_trend",
]


def resolve_portfolio_analysis_policy(
    stock_code: str,
    *,
    market: Optional[str] = None,
    repo: Optional[PortfolioRepository] = None,
) -> Dict[str, Any]:
    """Return policy fields only when backed by the DSA instrument registry."""

    symbol = canonical_stock_code(normalize_stock_code(str(stock_code or "").strip()))
    market_norm = str(market or get_market_for_stock(symbol) or "").strip().lower()
    if not symbol or not market_norm:
        return {}
    instrument = (repo or PortfolioRepository()).get_instrument(
        symbol=symbol,
        market=market_norm,
    )
    if instrument is None:
        return {}

    instrument_type = str(instrument.instrument_type or "unknown")
    verification_status = str(instrument.verification_status or "missing")
    if verification_status != "verified" or instrument_type not in _PROFILE_BY_TYPE:
        return {
            "profile": "identity_blocked",
            "precision_mode": True,
            "instrument_type": instrument_type,
            "verification_status": verification_status,
            "actionable_identity": False,
            "required_checks": [],
            "skills": [],
            "blockers": ["instrument_identity_unverified"],
        }

    policy = {
        "profile": _PROFILE_BY_TYPE[instrument_type],
        "precision_mode": instrument_type in {
            "qdii",
            "adr_ads",
            "daily_leveraged_product",
        },
        "instrument_type": instrument_type,
        "verification_status": verification_status,
        "actionable_identity": True,
        "quote_currency": instrument.quote_currency,
        "underlying_code": instrument.underlying_symbol,
        "underlying_market": instrument.underlying_market,
        "underlying_currency": instrument.underlying_currency,
        "leverage_factor": instrument.leverage_factor,
        "daily_reset": bool(instrument.daily_reset),
        "conversion_ratio": instrument.conversion_ratio,
        "trade_lot_size": float(instrument.trade_lot_size),
        "requires_premium_check": bool(instrument.requires_premium_check),
        "required_checks": list(_REQUIRED_CHECKS_BY_TYPE[instrument_type]),
        "skills": (
            list(_LEVERAGED_PRODUCT_SKILLS)
            if instrument_type == "daily_leveraged_product"
            else []
        ),
        "blockers": [],
    }
    return policy
