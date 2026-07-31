from __future__ import annotations

import pytest

from src.schemas.portfolio_instruction import project_holding_instruction


@pytest.mark.parametrize(
    ("position_action", "incremental_action", "blocked", "expected"),
    [
        ("hold", "add_in_batches", False, "add"),
        ("hold", "wait", False, "hold"),
        ("hold", "no_add", False, "hold"),
        ("reduce", "wait", False, "reduce"),
        ("reduce", "no_add", False, "reduce"),
        ("exit", "no_add", False, "exit"),
        ("hold", "add_in_batches", True, "insufficient"),
    ],
)
def test_project_holding_instruction(
    position_action: str,
    incremental_action: str,
    blocked: bool,
    expected: str,
) -> None:
    assert (
        project_holding_instruction(
            position_action=position_action,
            incremental_action=incremental_action,
            blocked=blocked,
        )
        == expected
    )


@pytest.mark.parametrize("position_action", ["reduce", "exit"])
def test_rejects_contradictory_position_reduction_and_add(
    position_action: str,
) -> None:
    with pytest.raises(ValueError, match="contradictory_portfolio_actions"):
        project_holding_instruction(
            position_action=position_action,
            incremental_action="add_in_batches",
            blocked=False,
        )


@pytest.mark.parametrize(
    ("position_action", "incremental_action"),
    [
        ("buy", "wait"),
        ("hold", "buy"),
    ],
)
def test_rejects_unknown_internal_actions(
    position_action: str,
    incremental_action: str,
) -> None:
    with pytest.raises(ValueError, match="invalid_portfolio_action"):
        project_holding_instruction(
            position_action=position_action,
            incremental_action=incremental_action,
            blocked=False,
        )
