# -*- coding: utf-8 -*-
"""Validate attribution links without mutating the DSA portfolio ledger."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.utils.sanitize import sanitize_decision_signal_text


LINK_STATUSES = frozenset({"proposed", "confirmed", "rejected"})
TEMPORAL_RELATIONS = frozenset(
    {"after_signal_confirmed", "before_signal", "same_day_unknown"}
)
LINKED_BY_VALUES = frozenset({"human", "import"})


class DecisionExecutionLinkService:
    """Create audit sidecars while treating referenced trades as read-only truth."""

    def __init__(
        self,
        repo: DecisionQualityRepository | None = None,
        db_manager: Any = None,
    ):
        self.repo = repo or DecisionQualityRepository(db_manager)

    def put_link(
        self,
        *,
        signal_id: int,
        trade_id: int,
        link_status: str,
        linked_by: str,
        temporal_relation: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if link_status not in LINK_STATUSES:
            raise ValueError("invalid_link_status")
        if linked_by not in LINKED_BY_VALUES:
            raise ValueError("invalid_linked_by")
        if temporal_relation is not None and temporal_relation not in TEMPORAL_RELATIONS:
            raise ValueError("invalid_temporal_relation")

        context = self.repo.get_context_by_signal(signal_id=signal_id)
        if context is None:
            raise ValueError("decision_quality_context_not_found")
        trade = self.repo.get_trade(trade_id=trade_id)
        if trade is None:
            raise ValueError("portfolio_trade_not_found")
        self._validate_trade(context, trade)
        relation = self._temporal_relation(
            context,
            trade,
            linked_by=linked_by,
            requested=temporal_relation,
        )
        if link_status == "confirmed":
            other = self.repo.get_confirmed_execution_link_by_trade(
                trade_id=trade_id,
                exclude_signal_id=signal_id,
            )
            if other is not None:
                raise ValueError("trade_already_attributed")

        row, _created = self.repo.upsert_execution_link(
            {
                "signal_id": signal_id,
                "trade_id": trade_id,
                "link_status": link_status,
                "temporal_relation": relation,
                "linked_by": linked_by,
                "note": sanitize_decision_signal_text(note) if note else None,
            }
        )
        return self._response(context, current=row)

    def get_links(self, *, signal_id: int) -> dict[str, Any]:
        context = self.repo.get_context_by_signal(signal_id=signal_id)
        if context is None:
            raise ValueError("decision_quality_context_not_found")
        return self._response(context)

    def _response(self, context: Any, *, current: Any | None = None) -> dict[str, Any]:
        links = self.repo.list_execution_links(signal_id=context.signal_id)
        aggregate = self._derive_actual_actions(context, links)
        payload = {
            "signal_id": context.signal_id,
            "links": [self._serialize_link(row) for row in links],
            **aggregate,
        }
        if current is not None:
            payload["link"] = self._serialize_link(current)
        return payload

    def _derive_actual_actions(self, context: Any, links: list[Any]) -> dict[str, Any]:
        eligible = [
            row
            for row in links
            if row.link_status == "confirmed"
            and row.temporal_relation == "after_signal_confirmed"
        ]
        trades = [self.repo.get_trade(trade_id=row.trade_id) for row in eligible]
        trades = [trade for trade in trades if trade is not None]
        sell_quantity = sum(float(trade.quantity) for trade in trades if trade.side == "sell")
        buy_quantity = sum(float(trade.quantity) for trade in trades if trade.side == "buy")
        actual_position_action = None
        unable_reasons = []
        frozen_quantity = self._positive_or_zero(context.frozen_position_quantity)
        if sell_quantity > 0:
            if frozen_quantity is None:
                unable_reasons.append("frozen_position_quantity_missing")
            elif sell_quantity > frozen_quantity + 1e-9:
                unable_reasons.append("linked_sell_quantity_exceeds_frozen_position")
            elif math.isclose(sell_quantity, frozen_quantity, rel_tol=0, abs_tol=1e-9):
                actual_position_action = "exit"
            else:
                actual_position_action = "reduce"
        return {
            "actual_position_action": actual_position_action,
            "actual_incremental_action": "add_in_batches" if buy_quantity > 0 else None,
            "confirmed_sell_quantity": sell_quantity,
            "confirmed_buy_quantity": buy_quantity,
            "unable_reasons": unable_reasons,
        }

    @staticmethod
    def _validate_trade(context: Any, trade: Any) -> None:
        if trade.account_id != context.account_id:
            raise ValueError("trade_account_mismatch")
        trade_market = str(trade.market or "").strip().lower()
        context_market = str(context.market or "").strip().lower()
        trade_symbol = canonical_stock_code(normalize_stock_code(str(trade.symbol or "")))
        context_symbol = canonical_stock_code(
            normalize_stock_code(str(context.stock_code or ""))
        )
        if trade_market != context_market or trade_symbol != context_symbol:
            raise ValueError("trade_instrument_mismatch")
        if trade.side not in {"buy", "sell"}:
            raise ValueError("trade_side_invalid")
        for field in ("quantity", "price"):
            value = getattr(trade, field)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"trade_{field}_invalid")
        for field in ("fee", "tax"):
            value = getattr(trade, field)
            if value is None or not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"trade_{field}_invalid")

    @staticmethod
    def _temporal_relation(
        context: Any,
        trade: Any,
        *,
        linked_by: str,
        requested: str | None,
    ) -> str:
        signal_date = context.decision_cutoff.date()
        if trade.trade_date > signal_date:
            return "after_signal_confirmed"
        if trade.trade_date < signal_date:
            return "before_signal"
        if linked_by == "human" and requested == "after_signal_confirmed":
            return "after_signal_confirmed"
        return "same_day_unknown"

    @staticmethod
    def _positive_or_zero(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _serialize_link(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "signal_id": row.signal_id,
            "trade_id": row.trade_id,
            "link_status": row.link_status,
            "temporal_relation": row.temporal_relation,
            "linked_by": row.linked_by,
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
