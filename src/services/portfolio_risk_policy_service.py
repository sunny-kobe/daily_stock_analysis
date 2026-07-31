# -*- coding: utf-8 -*-
"""Validated singleton portfolio risk-budget service."""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import PortfolioRiskPolicy


RISK_POLICY_FIELDS = (
    "min_cash_buffer_pct",
    "max_single_position_pct",
    "max_sector_pct",
    "max_high_risk_product_pct",
    "max_portfolio_drawdown_pct",
)


class PortfolioRiskPolicyService:
    """Own validation before the singleton DSA risk policy is persisted."""

    def __init__(self, repo: Optional[PortfolioRepository] = None):
        self.repo = repo or PortfolioRepository()

    def get_policy(self) -> Optional[Dict[str, Any]]:
        row = self.repo.get_risk_policy()
        return self._to_dict(row) if row is not None else None

    def save_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("risk policy payload must be an object")
        unsupported = sorted(set(payload) - set(RISK_POLICY_FIELDS))
        if unsupported:
            raise ValueError(f"unsupported risk policy fields: {', '.join(unsupported)}")
        if not payload:
            raise ValueError("No fields provided for update")

        existing = self.repo.get_risk_policy()
        merged = {
            field_name: getattr(existing, field_name)
            for field_name in RISK_POLICY_FIELDS
        } if existing is not None else {}
        merged.update(payload)

        missing = [field_name for field_name in RISK_POLICY_FIELDS if field_name not in merged]
        if missing:
            raise ValueError(f"{missing[0]} is required")

        fields = {
            field_name: self._percentage(merged[field_name], field_name)
            for field_name in RISK_POLICY_FIELDS
        }
        return self._to_dict(self.repo.upsert_risk_policy(fields))

    @staticmethod
    def _percentage(value: Any, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be in [0, 100]")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be in [0, 100]") from exc
        if number < 0 or number > 100:
            raise ValueError(f"{field_name} must be in [0, 100]")
        return number

    @staticmethod
    def _to_dict(row: PortfolioRiskPolicy) -> Dict[str, Any]:
        return {
            "id": row.id,
            **{
                field_name: float(getattr(row, field_name))
                for field_name in RISK_POLICY_FIELDS
            },
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
