# -*- coding: utf-8 -*-
"""Fail-closed portfolio gate applied before actionable signal persistence."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.services.portfolio_research_product_evidence import (
    product_evidence_for_account,
)
from src.schemas.decision_action import localize_action_label


ACTIONABLE_ACTIONS = frozenset({"buy", "add", "reduce", "sell"})


class PortfolioDecisionGate:
    """Pure blocker evaluation plus adapters for DSA frozen snapshot evidence."""

    def evaluate(self, *, action: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
        raw_action = str(action or "").strip().lower()
        blockers: list[str] = []
        if raw_action in ACTIONABLE_ACTIONS:
            instrument = self._mapping(evidence.get("instrument"))
            if not instrument:
                blockers.append("instrument_identity_missing")
            else:
                required_identity = ("symbol", "market", "quote_currency", "instrument_type")
                if any(not instrument.get(field) for field in required_identity):
                    blockers.append("instrument_identity_incomplete")
                if (
                    instrument.get("verification_status") != "verified"
                    or instrument.get("instrument_type") == "unknown"
                ):
                    blockers.append("instrument_identity_unverified")

            price = self._mapping(evidence.get("price"))
            if price.get("available") is not True:
                blockers.append("decision_price_missing")
            elif price.get("stale") is True:
                blockers.append("decision_price_stale")

            fx = self._mapping(evidence.get("fx"))
            if fx.get("required") is True:
                if fx.get("available") is not True:
                    blockers.append("fx_rate_missing")
                elif fx.get("stale") is True:
                    blockers.append("fx_rate_stale")

            if not isinstance(evidence.get("risk_policy"), Mapping):
                blockers.append("portfolio_risk_policy_missing")

            instrument_type = str(instrument.get("instrument_type") or "")
            product = self._mapping(evidence.get("product"))
            if instrument_type == "adr_ads":
                for field_name, blocker in (
                    ("underlying_symbol", "adr_underlying_missing"),
                    ("underlying_market", "adr_underlying_market_missing"),
                    ("underlying_currency", "adr_underlying_currency_missing"),
                ):
                    if not instrument.get(field_name):
                        blockers.append(blocker)
                if self._positive(instrument.get("conversion_ratio")) is None:
                    blockers.append("adr_conversion_ratio_missing")
            if instrument_type == "daily_leveraged_product":
                if not all(
                    instrument.get(field_name)
                    for field_name in ("underlying_symbol", "underlying_market", "underlying_currency")
                ):
                    blockers.append("leveraged_underlying_missing")
                if instrument.get("daily_reset") is not True or self._positive(
                    instrument.get("leverage_factor")
                ) is None:
                    blockers.append("daily_reset_terms_missing")
                for field_name, blocker in (
                    ("official_terms_available", "daily_reset_official_terms_missing"),
                    ("underlying_identity_available", "daily_reset_underlying_identity_missing"),
                    ("underlying_same_cutoff_available", "daily_reset_underlying_same_cutoff_missing"),
                    ("intraday_leverage_available", "daily_reset_intraday_leverage_missing"),
                    ("path_decay_rebalance_available", "daily_reset_path_decay_rebalance_missing"),
                    ("liquidity_available", "daily_reset_liquidity_missing"),
                    ("horizon_fit_evaluated", "daily_reset_horizon_fit_missing"),
                ):
                    if product.get(field_name) is not True:
                        blockers.append(blocker)
                product_components = self._mapping(product.get("components"))
                horizon_fit = self._mapping(product_components.get("horizon_fit"))
                if (
                    raw_action in {"buy", "add"}
                    and horizon_fit.get("evaluated") is True
                    and horizon_fit.get("fits_holding_period") is False
                ):
                    blockers.append("daily_reset_holding_period_incompatible")

            if instrument_type == "qdii":
                for field_name, blocker in (
                    ("nav_iopv_available", "qdii_nav_iopv_missing"),
                    ("premium_discount_available", "qdii_premium_discount_missing"),
                    ("underlying_fx_available", "qdii_underlying_fx_missing"),
                    ("spread_available", "qdii_spread_missing"),
                    ("tracking_available", "qdii_tracking_evidence_missing"),
                ):
                    if product.get(field_name) is not True:
                        blockers.append(blocker)

            premium = self._mapping(evidence.get("premium"))
            premium_required = bool(
                instrument.get("requires_premium_check")
                or premium.get("required") is True
            )
            if premium_required:
                if premium.get("available") is not True:
                    blockers.append("nav_premium_missing")
                elif premium.get("stale") is True:
                    blockers.append("nav_premium_stale")

            trade = self._mapping(evidence.get("trade"))
            quantity = self._positive(trade.get("quantity"))
            lot_size = self._positive(
                trade.get("lot_size") or instrument.get("trade_lot_size")
            )
            if trade.get("quantity") is not None:
                if quantity is None or lot_size is None:
                    blockers.append("trade_lot_size_invalid")
                elif not math.isclose(quantity / lot_size, round(quantity / lot_size), abs_tol=1e-9):
                    blockers.append("trade_quantity_not_lot_aligned")

            news = self._mapping(evidence.get("news"))
            if (
                news.get("event_dependent") is True
                and news.get("primary_evidence_available") is not True
            ):
                blockers.append("event_evidence_missing")

        blockers = list(dict.fromkeys(blockers))
        final_action = "alert" if blockers and raw_action in ACTIONABLE_ACTIONS else raw_action
        risk_budget = self._mapping(evidence.get("risk_budget"))
        return {
            "raw_action": raw_action,
            "final_action": final_action,
            "hard_blockers": blockers,
            "required_capability": self._required_capability(blockers),
            "completeness": "COMPLETE" if not blockers else "INSUFFICIENT_EVIDENCE",
            "risk_budget_evaluated": risk_budget.get("evaluated") is True,
        }

    def apply_to_payload(
        self,
        payload: Mapping[str, Any],
        *,
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        gated = dict(payload)
        metadata = dict(gated.get("metadata")) if isinstance(gated.get("metadata"), Mapping) else {}
        portfolio_decision = self._mapping(metadata.get("portfolio_decision"))
        if portfolio_decision:
            result = self._apply_two_axis_decision(
                gated, metadata, portfolio_decision, evidence
            )
            return self._apply_sizing_contract(result, evidence=evidence)

        result = self.evaluate(action=str(gated.get("action") or ""), evidence=evidence)
        unable_reasons = metadata.get("quality_context_unable_reasons")
        if isinstance(unable_reasons, list) and "portfolio_decision_missing" in unable_reasons:
            result = dict(result)
            result["hard_blockers"] = list(
                dict.fromkeys([*result["hard_blockers"], "portfolio_decision_missing"])
            )
            result["final_action"] = (
                "alert" if result["raw_action"] in ACTIONABLE_ACTIONS else result["raw_action"]
            )
            result["completeness"] = "INSUFFICIENT_EVIDENCE"
        metadata["portfolio_gate"] = result
        metadata.setdefault("raw_action", result["raw_action"])
        metadata["final_action"] = result["final_action"]
        if result["final_action"] != result["raw_action"]:
            metadata["action_adjustment_reason"] = "portfolio_fail_closed_gate"
            metadata["guardrail_reason"] = "组合关键证据不完整，已阻断可执行交易信号"
        gated["metadata"] = metadata
        gated["action"] = result["final_action"]
        if result["final_action"] != result["raw_action"] or not gated.get("action_label"):
            gated["action_label"] = localize_action_label(
                result["final_action"],
                gated.get("report_language"),
            )
        return self._apply_sizing_contract(gated, evidence=evidence)

    def _apply_two_axis_decision(
        self,
        gated: Dict[str, Any],
        metadata: Dict[str, Any],
        decision: Dict[str, Any],
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        position_action = str(decision.get("position_action") or "")
        incremental_action = str(decision.get("incremental_action") or "")
        position_result = self.evaluate(
            action={"hold": "hold", "reduce": "reduce", "exit": "sell"}.get(
                position_action, "alert"
            ),
            evidence=evidence,
        )
        incremental_result = self.evaluate(
            action="add" if incremental_action == "add_in_batches" else "hold",
            evidence=evidence,
        )

        decision["position_action_blockers"] = list(position_result["hard_blockers"])
        decision["incremental_action_blockers"] = list(incremental_result["hard_blockers"])
        decision["position_action_executable"] = not position_result["hard_blockers"]
        decision["incremental_action_executable"] = (
            incremental_action == "add_in_batches" and not incremental_result["hard_blockers"]
        )

        if position_action in {"reduce", "exit"}:
            final_action = position_result["final_action"]
            result = position_result
        elif incremental_action == "add_in_batches":
            if incremental_result["hard_blockers"]:
                decision["incremental_action"] = "wait"
                final_action = "hold"
            else:
                final_action = "add"
            result = incremental_result
        else:
            final_action = "hold"
            result = position_result

        result = dict(result)
        result["raw_action"] = str(gated.get("action") or "").strip().lower()
        result["final_action"] = final_action
        metadata["portfolio_decision"] = decision
        metadata["portfolio_gate"] = result
        metadata.setdefault("raw_action", result["raw_action"])
        metadata["final_action"] = final_action
        if final_action != result["raw_action"]:
            metadata["action_adjustment_reason"] = "portfolio_two_axis_gate"
        gated["metadata"] = metadata
        gated["action"] = final_action
        gated["action_label"] = localize_action_label(
            final_action,
            gated.get("report_language"),
        )
        return gated

    @staticmethod
    def _apply_sizing_contract(
        payload: Dict[str, Any],
        *,
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        risk_budget = (
            dict(evidence.get("risk_budget"))
            if isinstance(evidence.get("risk_budget"), Mapping)
            else {}
        )
        metadata = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), Mapping)
            else {}
        )
        sizing_allowed = risk_budget.get("evaluated") is True
        metadata["sizing_allowed"] = sizing_allowed
        if not sizing_allowed:
            payload = PortfolioDecisionGate._without_sizing_fields(payload)
            metadata = (
                dict(payload.get("metadata"))
                if isinstance(payload.get("metadata"), Mapping)
                else {}
            )
            metadata["sizing_allowed"] = False
        payload["metadata"] = metadata
        return payload

    @classmethod
    def _without_sizing_fields(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: cls._without_sizing_fields(item)
                for key, item in value.items()
                if not cls._is_sizing_field(key)
            }
        if isinstance(value, list):
            return [cls._without_sizing_fields(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._without_sizing_fields(item) for item in value)
        return value

    @staticmethod
    def _is_sizing_field(value: Any) -> bool:
        field = str(value or "").strip().lower()
        return field.endswith(
            ("_quantity", "_ratio", "_pct", "_percentage", "_amount")
        ) or field in {
            "target_position",
            "target_allocation",
            "position_size",
            "suggested_position",
            "allocation",
        }

    def evidence_from_snapshot(
        self,
        *,
        payload: Mapping[str, Any],
        research_snapshot: Mapping[str, Any],
        context_snapshot: Optional[Mapping[str, Any]] = None,
        portfolio_context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        symbol = canonical_stock_code(normalize_stock_code(str(payload.get("stock_code") or "")))
        market = str(payload.get("market") or "").strip().lower()
        instrument = next(
            (
                dict(item)
                for item in research_snapshot.get("instruments") or []
                if isinstance(item, Mapping)
                and str(item.get("symbol") or "").upper() == symbol
                and str(item.get("market") or "").lower() == market
            ),
            None,
        )
        context = self._mapping(portfolio_context)
        account_id = context.get("account_id")
        if instrument is not None:
            instrument["product_evidence"] = product_evidence_for_account(
                instrument,
                account_id=account_id,
            )
        position = next(
            (
                dict(item)
                for item in research_snapshot.get("positions") or []
                if isinstance(item, Mapping)
                and str(item.get("symbol") or "").upper() == symbol
                and str(item.get("market") or "").lower() == market
                and (account_id is None or item.get("account_id") == account_id)
            ),
            None,
        )

        if position is not None:
            price = {
                "available": position.get("price_available") is True,
                "stale": position.get("price_stale") is True,
            }
            fx = self._mapping(position.get("fx"))
        else:
            statuses = self._context_block_statuses(context_snapshot)
            price = {
                "available": statuses.get("quote") == "available",
                "stale": statuses.get("quote") in {"stale", "expired"},
            }
            account_currencies = {
                str(item.get("base_currency") or "").upper()
                for item in research_snapshot.get("accounts") or []
                if isinstance(item, Mapping)
            }
            quote_currency = str((instrument or {}).get("quote_currency") or "").upper()
            fx_required = bool(
                quote_currency
                and account_currencies
                and any(currency != quote_currency for currency in account_currencies)
            )
            fx = {
                "required": fx_required,
                "available": not fx_required,
                "stale": False,
            }

        metadata = self._mapping(payload.get("metadata"))
        snapshot_context = self._mapping(context_snapshot)
        frozen_product = self._mapping((instrument or {}).get("product_evidence"))
        product = self._mapping(
            frozen_product
            or context.get("product_evidence")
            or snapshot_context.get("product_evidence")
        )
        if frozen_product and (instrument or {}).get("requires_premium_check") is True:
            premium = {
                "available": bool(
                    frozen_product.get("status") == "ready"
                    and frozen_product.get("premium_discount_available") is True
                    and not frozen_product.get("blockers")
                ),
                "stale": False,
            }
        else:
            premium = self._mapping(
                context.get("premium_evidence")
                or snapshot_context.get("premium_evidence")
            )
        trade_quantity = metadata.get("suggested_trade_quantity")
        if self._positive(trade_quantity) is None:
            trade_quantity = context.get("proposed_trade_quantity")
        return {
            "instrument": instrument,
            "price": price,
            "fx": fx,
            "risk_policy": research_snapshot.get("risk_policy"),
            "risk_budget": research_snapshot.get("risk_budget"),
            "premium": {
                "required": bool((instrument or {}).get("requires_premium_check")),
                "available": premium.get("available") is True,
                "stale": premium.get("stale") is True,
            },
            "product": product,
            "trade": {
                "quantity": trade_quantity,
                "lot_size": (instrument or {}).get("trade_lot_size"),
            },
            "news": {
                "event_dependent": metadata.get("event_dependent_action") is True,
                "primary_evidence_available": self._news_result_count(context_snapshot) > 0,
            },
        }

    @staticmethod
    def _context_block_statuses(context_snapshot: Optional[Mapping[str, Any]]) -> Dict[str, str]:
        snapshot = PortfolioDecisionGate._mapping(context_snapshot)
        overview = PortfolioDecisionGate._mapping(snapshot.get("analysis_context_pack_overview"))
        statuses = {}
        for item in overview.get("blocks") or []:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or "").strip()
            if key:
                statuses[key] = str(item.get("status") or "").strip()
        return statuses

    @staticmethod
    def _news_result_count(context_snapshot: Optional[Mapping[str, Any]]) -> int:
        snapshot = PortfolioDecisionGate._mapping(context_snapshot)
        overview = PortfolioDecisionGate._mapping(snapshot.get("analysis_context_pack_overview"))
        metadata = PortfolioDecisionGate._mapping(overview.get("metadata"))
        value = metadata.get("news_result_count", snapshot.get("news_result_count", 0))
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _required_capability(blockers: list[str]) -> Optional[str]:
        if "event_evidence_missing" in blockers:
            return "vibe_current_evidence"
        if any(
            blocker.startswith(("nav_premium_", "adr_", "leveraged_", "daily_reset_"))
            for blocker in blockers
        ):
            return "vibe_product_evidence"
        return "dsa_control_plane" if blockers else None

    @staticmethod
    def _mapping(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _positive(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None
