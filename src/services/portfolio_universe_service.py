# -*- coding: utf-8 -*-
"""Pure analysis-universe resolution from DSA cached holdings and watchlist."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from data_provider.base import canonical_stock_code
from src.core.trading_calendar import get_market_for_stock
from src.repositories.portfolio_repo import PortfolioRepository


ANALYSIS_UNIVERSE_SOURCES = frozenset({"watchlist", "portfolio_holdings", "union"})
_MARKET_ORDER = {market: index for index, market in enumerate(("cn", "hk", "us", "jp", "kr", "tw"))}


class PortfolioUniverseUnavailableError(ValueError):
    """Raised when a holdings-backed universe cannot be resolved safely."""


class PortfolioUniverseService:
    """Resolve a deterministic batch universe without replaying or writing the ledger."""

    def __init__(self, repo: Optional[PortfolioRepository] = None):
        self.repo = repo or PortfolioRepository()

    def resolve(
        self,
        *,
        source: str,
        watchlist: Iterable[str],
        account_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        source_norm = str(source or "").strip().lower()
        if source_norm not in ANALYSIS_UNIVERSE_SOURCES:
            raise ValueError(
                "analysis universe source must be one of: "
                + ", ".join(sorted(ANALYSIS_UNIVERSE_SOURCES))
            )

        watchlist_entries = self._watchlist_entries(watchlist)
        holdings_entries: List[Tuple[str, str]] = []
        ledger_as_of: Optional[datetime] = None
        if source_norm in {"portfolio_holdings", "union"}:
            try:
                raw_identities = self.repo.list_cached_position_identities(
                    account_id=account_id
                )
                ledger_as_of = self.repo.get_cached_positions_updated_at(
                    account_id=account_id
                )
            except Exception as exc:
                raise PortfolioUniverseUnavailableError(
                    f"portfolio ledger read failed: {exc}"
                ) from exc
            holdings_entries = self._holding_entries(raw_identities)
            if not holdings_entries:
                raise PortfolioUniverseUnavailableError(
                    "portfolio ledger has no non-zero positions; watchlist fallback is disabled"
                )

        if source_norm == "watchlist":
            selected_entries = watchlist_entries
        elif source_norm == "portfolio_holdings":
            selected_entries = holdings_entries
        else:
            selected_entries = self._deduplicate_entries(
                [*holdings_entries, *watchlist_entries]
            )

        return {
            "source": source_norm,
            "symbols": [symbol for _market, symbol in selected_entries],
            "blocked_symbols": [],
            "ledger_as_of": ledger_as_of.isoformat() if ledger_as_of else None,
            "coverage": {
                "holdings_count": len(holdings_entries),
                "watchlist_count": len(watchlist_entries),
                "selected_count": len(selected_entries),
                "deduplicated_count": (
                    len(holdings_entries) + len(watchlist_entries) - len(selected_entries)
                    if source_norm == "union"
                    else 0
                ),
            },
        }

    @classmethod
    def resolve_for_config(
        cls,
        config: Any,
        *,
        repo: Optional[PortfolioRepository] = None,
    ) -> Dict[str, Any]:
        source = str(
            getattr(config, "analysis_universe_source", "watchlist") or "watchlist"
        ).strip().lower()
        if source in {"watchlist", "union"}:
            config.refresh_stock_list()
        return cls(repo=repo).resolve(
            source=source,
            watchlist=getattr(config, "stock_list", []) or [],
        )

    @classmethod
    def _holding_entries(
        cls,
        identities: Iterable[Tuple[str, str]],
    ) -> List[Tuple[str, str]]:
        entries = []
        for market, symbol in identities:
            market_norm = str(market or "").strip().lower()
            symbol_norm = canonical_stock_code(str(symbol or ""))
            if market_norm and symbol_norm:
                entries.append((market_norm, symbol_norm))
        return cls._deduplicate_entries(entries)

    @classmethod
    def _watchlist_entries(cls, watchlist: Iterable[str]) -> List[Tuple[str, str]]:
        entries = []
        for symbol in watchlist:
            symbol_norm = canonical_stock_code(str(symbol or ""))
            if not symbol_norm:
                continue
            market = get_market_for_stock(symbol_norm) or "unknown"
            entries.append((market, symbol_norm))
        return cls._deduplicate_entries(entries)

    @staticmethod
    def _deduplicate_entries(entries: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
        unique = {(market, symbol) for market, symbol in entries}
        return sorted(
            unique,
            key=lambda item: (_MARKET_ORDER.get(item[0], len(_MARKET_ORDER)), item[0], item[1]),
        )
