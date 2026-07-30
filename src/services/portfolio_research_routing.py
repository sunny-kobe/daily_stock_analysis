"""Pure artifact-only routing for optional portfolio research capabilities."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from data_provider.base import canonical_stock_code


CAPABILITY_BY_QUESTION_TYPE = {
    "timing": "deterministic_timing",
    "product_terms": "vibe_product_evidence",
    "thesis": "tradingagents_thesis",
    "high_impact_risk": "tradingagents_risk_review",
}


class PortfolioResearchRoutingService:
    """Return one auditable upgrade offer without invoking any worker."""

    def route(
        self,
        *,
        research_snapshot: Mapping[str, Any],
        symbol: str,
        market: str,
        question_type: str,
        question: str,
    ) -> Dict[str, Any]:
        symbol_norm = canonical_stock_code(str(symbol or ""))
        market_norm = str(market or "").strip().lower()
        question_norm = str(question or "").strip()
        if not symbol_norm or not market_norm or not question_norm:
            raise ValueError("symbol, market, and exact question are required")
        if question_type not in {"routine", *CAPABILITY_BY_QUESTION_TYPE}:
            raise ValueError(f"unsupported question_type: {question_type}")

        blockers = self._relevant_blockers(
            research_snapshot,
            symbol=symbol_norm,
            market=market_norm,
        )
        instrument = next(
            (
                item for item in research_snapshot.get("instruments") or []
                if isinstance(item, Mapping)
                and str(item.get("symbol") or "").upper() == symbol_norm
                and str(item.get("market") or "").lower() == market_norm
            ),
            None,
        )
        if instrument is None and "instrument_identity_missing" not in blockers:
            blockers.append("instrument_identity_missing")
        if blockers:
            return self._result(
                research_snapshot, symbol_norm, market_norm, question_type,
                question_norm, status="blocked", capability=None, blockers=blockers,
            )

        capability = CAPABILITY_BY_QUESTION_TYPE.get(question_type)
        return self._result(
            research_snapshot, symbol_norm, market_norm, question_type,
            question_norm,
            status="offered_pending_confirmation" if capability else "not_required",
            capability=capability,
            blockers=[],
        )

    @staticmethod
    def _relevant_blockers(
        snapshot: Mapping[str, Any],
        *,
        symbol: str,
        market: str,
    ) -> list[str]:
        codes = []
        for item in snapshot.get("hard_blockers") or []:
            if not isinstance(item, Mapping):
                continue
            scope = str(item.get("scope") or "")
            if scope == "instrument" and (
                str(item.get("symbol") or "").upper() != symbol
                or str(item.get("market") or "").lower() != market
            ):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                codes.append(code)
        return list(dict.fromkeys(codes))

    @staticmethod
    def _result(
        snapshot: Mapping[str, Any],
        symbol: str,
        market: str,
        question_type: str,
        question: str,
        *,
        status: str,
        capability: str | None,
        blockers: list[str],
    ) -> Dict[str, Any]:
        return {
            "schema_version": "portfolio-research-route-v1",
            "frozen_input_hash": snapshot.get("snapshot_hash"),
            "cutoff": snapshot.get("cutoff"),
            "symbol": symbol,
            "market": market,
            "question_type": question_type,
            "exact_question": question,
            "status": status,
            "capability": capability,
            "blockers": blockers,
            "worker_started": False,
        }
