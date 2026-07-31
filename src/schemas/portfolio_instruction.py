"""User-facing projection for internal portfolio action axes."""

from __future__ import annotations

from typing import Literal


HoldingInstruction = Literal["add", "hold", "reduce", "exit", "insufficient"]

_POSITION_ACTIONS = frozenset({"hold", "reduce", "exit"})
_INCREMENTAL_ACTIONS = frozenset({"add_in_batches", "wait", "no_add"})


def project_holding_instruction(
    *,
    position_action: str,
    incremental_action: str,
    blocked: bool,
) -> HoldingInstruction:
    """Return one plain instruction without changing either stored action axis."""

    if (
        position_action not in _POSITION_ACTIONS
        or incremental_action not in _INCREMENTAL_ACTIONS
    ):
        raise ValueError("invalid_portfolio_action")
    if position_action in {"reduce", "exit"} and incremental_action == "add_in_batches":
        raise ValueError("contradictory_portfolio_actions")
    if blocked:
        return "insufficient"
    if position_action == "reduce":
        return "reduce"
    if position_action == "exit":
        return "exit"
    if incremental_action == "add_in_batches":
        return "add"
    return "hold"
