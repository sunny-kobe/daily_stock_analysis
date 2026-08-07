# -*- coding: utf-8 -*-
"""Normalize and validate caller-selected portfolio research scope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from data_provider.base import canonical_stock_code, normalize_stock_code


ResearchScopeKey = tuple[int, str, str]


def normalize_research_scope(scope: Optional[Sequence[Any]]) -> Optional[list[ResearchScopeKey]]:
    if scope is None:
        return None
    if not scope:
        raise ValueError("research scope must not be empty")

    normalized: set[ResearchScopeKey] = set()
    for item in scope:
        if isinstance(item, Mapping):
            account_value = item.get("account_id")
            market_value = item.get("market")
            symbol_value = item.get("symbol")
        else:
            account_value = getattr(item, "account_id", None)
            market_value = getattr(item, "market", None)
            symbol_value = getattr(item, "symbol", None)
        try:
            account_id = int(account_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("research scope account_id must be a positive integer") from exc
        market = str(market_value or "").strip().lower()
        symbol = canonical_stock_code(
            normalize_stock_code(str(symbol_value or "").strip())
        ).upper()
        if account_id <= 0 or not market or not symbol:
            raise ValueError("research scope identity is incomplete")
        normalized.add((account_id, market, symbol))
    return sorted(normalized)


def research_scope_payload(scope: Sequence[ResearchScopeKey]) -> list[dict[str, Any]]:
    return [
        {"account_id": account_id, "market": market, "symbol": symbol}
        for account_id, market, symbol in scope
    ]


def position_scope_key(position: Any) -> ResearchScopeKey:
    if isinstance(position, Mapping):
        account_value = position.get("account_id")
        market_value = position.get("market")
        symbol_value = position.get("symbol")
    else:
        account_value = getattr(position, "account_id", None)
        market_value = getattr(position, "market", None)
        symbol_value = getattr(position, "symbol", None)
    return (
        int(account_value),
        str(market_value or "").strip().lower(),
        canonical_stock_code(
            normalize_stock_code(str(symbol_value or "").strip())
        ).upper(),
    )


def resolve_research_scope(
    requested: Optional[Sequence[Any]],
    *,
    positive_positions: Sequence[Any],
) -> list[ResearchScopeKey]:
    held = {position_scope_key(position) for position in positive_positions}
    normalized = normalize_research_scope(requested)
    if normalized is None:
        return sorted(held)
    missing = [key for key in normalized if key not in held]
    if missing:
        raise ValueError(f"research scope contains non-held positions: {missing}")
    return normalized
