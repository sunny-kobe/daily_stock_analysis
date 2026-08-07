"""Bounded deterministic baseline for daily portfolio review."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import pandas as pd

from data_provider.base import canonical_stock_code
from src.schemas.portfolio_instruction import project_holding_instruction
from src.services.portfolio_research_product_evidence import (
    product_evidence_for_account,
)


logger = logging.getLogger(__name__)

PositionKey = Tuple[str, str]
NameLoader = Callable[[list[PositionKey]], Mapping[PositionKey, str]]
QuoteLoader = Callable[[list[PositionKey]], Mapping[PositionKey, Mapping[str, Any]]]
HistoryLoader = Callable[[list[PositionKey], str], Mapping[PositionKey, Mapping[str, Any]]]
SignalLoader = Callable[[list[PositionKey]], Sequence[Mapping[str, Any]]]
TrendLoader = Callable[[str, Mapping[str, Any]], Optional[Mapping[str, Any]]]

_POSITION_ACTIONS = frozenset({"hold", "reduce", "exit"})
_INCREMENTAL_ACTIONS = frozenset({"add_in_batches", "wait", "no_add"})
_ACTION_CHANGING_POSITION = frozenset({"reduce", "exit"})
_ACTION_CHANGING_INCREMENTAL = frozenset({"add_in_batches"})


class PortfolioResearchBaselineService:
    """Build one cheap baseline row per frozen positive account-position."""

    def __init__(
        self,
        *,
        name_loader: Optional[NameLoader] = None,
        quote_loader: Optional[QuoteLoader] = None,
        history_loader: Optional[HistoryLoader] = None,
        signal_loader: Optional[SignalLoader] = None,
        trend_loader: Optional[TrendLoader] = None,
        deep_analysis: Optional[Callable[..., Any]] = None,
        max_recommended: int = 5,
    ) -> None:
        self.name_loader = name_loader or self._load_names
        self.quote_loader = quote_loader
        self.history_loader = history_loader or self._load_histories
        self.signal_loader = signal_loader
        self.trend_loader = trend_loader or self._load_trend
        # Kept only as an injectable tripwire in tests. Baseline never invokes it.
        self._deep_analysis = deep_analysis
        self.max_recommended = max(0, int(max_recommended))

    def build(self, research_snapshot: Mapping[str, Any]) -> Dict[str, Any]:
        snapshot = dict(research_snapshot)
        cutoff = str(snapshot.get("cutoff") or "").strip()
        snapshot_hash = str(snapshot.get("snapshot_hash") or "").strip().lower()
        positions = self._positive_positions(snapshot.get("positions"))
        keys = sorted({self._position_key(item) for item in positions})

        names = dict(self.name_loader(keys) or {})
        quotes = dict(
            self.quote_loader(keys)
            if self.quote_loader is not None
            else self._quotes_from_positions(positions)
        )
        histories = dict(self.history_loader(keys, cutoff) or {})
        signal_rows = (
            self.signal_loader(keys)
            if self.signal_loader is not None
            else snapshot.get("decision_signals")
        )
        signals = self._signals_by_key(signal_rows or [])
        instruments = {
            self._position_key(item): dict(item)
            for item in snapshot.get("instruments") or []
            if isinstance(item, Mapping)
        }

        shared: Dict[PositionKey, Dict[str, Any]] = {}
        for market, symbol in keys:
            key = (market, symbol)
            instrument = instruments.get(key)
            observed_name = self._clean_name(names.get(key))
            canonical_name = self._clean_name(
                instrument.get("name") if isinstance(instrument, Mapping) else None
            )
            history = dict(histories.get(key) or {})
            trend = None
            if history.get("available") is True:
                try:
                    trend = self.trend_loader(symbol, history)
                except Exception as exc:  # pragma: no cover - defensive adapter guard
                    logger.warning("Fast portfolio trend failed for %s:%s: %s", market, symbol, exc)
            shared[key] = {
                "name": canonical_name or observed_name,
                "canonical_name": canonical_name,
                "observed_name": observed_name,
                "quote": self._public_evidence(quotes.get(key)),
                "history": self._public_evidence(history),
                "trend": dict(trend) if isinstance(trend, Mapping) else None,
                "signals": signals.get(key, []),
                "instrument": instrument,
            }

        items = [
            self._build_row(
                position=position,
                shared=shared[self._position_key(position)],
                snapshot=snapshot,
            )
            for position in positions
        ]
        candidates = self._build_candidates(items)
        recommended_keys = {
            item["selection_key"]
            for item in candidates[: self.max_recommended]
        }
        # The limit is a readability cap for ordinary exceptions, not a license
        # to hide evidence failures that would later block consolidation.
        recommended_keys.update(
            item["selection_key"]
            for item in items
            if item.get("evidence_status") == "INSUFFICIENT_EVIDENCE"
        )
        for item in items:
            item["detail_recommended"] = item["selection_key"] in recommended_keys
        for item in candidates:
            item["recommended"] = item["selection_key"] in recommended_keys

        return {
            "schema_version": "portfolio-research-baseline-v1",
            "snapshot_hash": snapshot_hash,
            "cutoff": cutoff,
            "market_data_cutoff": cutoff,
            "ledger_position_count": len(positions),
            "baseline_row_count": len(items),
            "coverage_reconciled": len(items) == len(positions),
            "portfolio_risk_flags": self._portfolio_risk_flags(snapshot),
            "items": items,
            "suggested_deep_analysis": candidates,
            "deep_analysis_started": False,
        }

    def _build_row(
        self,
        *,
        position: Mapping[str, Any],
        shared: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> Dict[str, Any]:
        market, symbol = self._position_key(position)
        signal = self._select_signal(
            shared.get("signals"),
            account_id=position.get("account_id"),
        )
        instrument = (
            dict(shared["instrument"])
            if isinstance(shared.get("instrument"), Mapping)
            else None
        )
        if instrument is not None:
            instrument["product_evidence"] = product_evidence_for_account(
                instrument,
                account_id=position.get("account_id"),
            )
        quote = dict(shared.get("quote") or {})
        history = dict(shared.get("history") or {})
        name = self._clean_name(shared.get("name"))
        if name is None and signal is not None:
            name = self._clean_name(signal.get("stock_name"))

        reasons: list[str] = []
        blockers = self._snapshot_blockers(snapshot, position=position)
        canonical_name = self._clean_name(shared.get("canonical_name"))
        observed_name = self._clean_name(shared.get("observed_name"))
        if (
            canonical_name is not None
            and observed_name is not None
            and not self._names_match(
                symbol=symbol,
                canonical_name=canonical_name,
                observed_name=observed_name,
            )
        ):
            blockers.append("instrument_name_mismatch")
        if name is None:
            reasons.append("instrument_name_missing")
        if quote.get("available") is not True:
            reasons.append("baseline_quote_missing")
        elif quote.get("stale") is True:
            reasons.append("baseline_quote_stale")
        if history.get("available") is not True:
            reasons.append("baseline_history_missing")
        elif int(history.get("bar_count") or 0) < 20:
            reasons.append("baseline_history_insufficient")

        decision, signal_reasons, signal_blockers = self._decision_from_signal(signal)
        reasons.extend(signal_reasons)
        blockers.extend(signal_blockers)
        if instrument is None:
            blockers.append("instrument_identity_missing")
        else:
            if instrument.get("verification_status") != "verified":
                blockers.append("instrument_identity_unverified")
            instrument_type = str(instrument.get("instrument_type") or "unknown")
            blockers.extend(self._product_evidence_blockers(instrument))
            if instrument_type == "adr_ads":
                blockers.append("adr_parity_required")

        risk_flags = self._row_risk_flags(
            snapshot,
            position=position,
            instrument=instrument,
        )
        blockers = self._unique([*blockers, *reasons])
        contradictory_actions = (
            decision["position_action"] in {"reduce", "exit"}
            and decision["incremental_action"] == "add_in_batches"
        )
        if contradictory_actions:
            blockers = self._unique([*blockers, "contradictory_portfolio_actions"])
        exception_reasons = self._unique(
            [
                *reasons,
                *signal_blockers,
                *self._product_reasons(instrument),
                *(item["code"] for item in risk_flags),
            ]
        )
        if decision["position_action"] in _ACTION_CHANGING_POSITION:
            exception_reasons.append("action_changing_position_signal")
            exception_reasons.append(
                f"position_action_{decision['position_action']}"
            )
        if decision["incremental_action"] in _ACTION_CHANGING_INCREMENTAL:
            exception_reasons.append("action_changing_incremental_signal")
            exception_reasons.append("incremental_action_add_in_batches")
        exception_reasons = self._unique(exception_reasons)

        display_name = name or "名称待核验"
        evidence_status = "INSUFFICIENT_EVIDENCE" if blockers else "baseline"
        user_instruction = (
            "insufficient"
            if contradictory_actions
            else project_holding_instruction(
                position_action=decision["position_action"],
                incremental_action=decision["incremental_action"],
                blocked=bool(blockers),
            )
        )
        return {
            "account_id": position.get("account_id"),
            "account_name": position.get("account_name"),
            "market": market,
            "symbol": symbol,
            "name": name,
            "display_label": f"{display_name}（{symbol}）",
            "selection_key": f"{market}:{symbol}",
            "currency": position.get("currency"),
            "quantity": position.get("quantity"),
            "instrument_type": (instrument or {}).get("instrument_type") or "unknown",
            "quote": quote,
            "history": history,
            "trend": shared.get("trend"),
            "current_signal_id": signal.get("id") if signal is not None else None,
            "position_action": decision["position_action"],
            "incremental_action": decision["incremental_action"],
            "user_instruction": user_instruction,
            "core_reason": signal.get("reason") if signal is not None else "No active DecisionSignal; conservative baseline",
            "hard_blockers": blockers,
            "risk_flags": risk_flags,
            "exception_reasons": exception_reasons,
            "evidence_status": evidence_status,
            "research_level": "baseline",
            "detail_recommended": False,
            "sizing_allowed": False,
        }

    @staticmethod
    def _decision_from_signal(
        signal: Optional[Mapping[str, Any]],
    ) -> tuple[Dict[str, str], list[str], list[str]]:
        fallback = {"position_action": "hold", "incremental_action": "wait"}
        if signal is None:
            return fallback, ["active_decision_signal_missing"], ["active_decision_signal_missing"]
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), Mapping) else {}
        decision = (
            dict(metadata.get("portfolio_decision"))
            if isinstance(metadata.get("portfolio_decision"), Mapping)
            else {}
        )
        position_action = str(decision.get("position_action") or "")
        incremental_action = str(decision.get("incremental_action") or "")
        blockers: list[str] = []
        reasons: list[str] = []
        if position_action not in _POSITION_ACTIONS:
            position_action = "hold"
            blockers.append("position_action_missing_or_invalid")
        if incremental_action not in _INCREMENTAL_ACTIONS:
            incremental_action = "wait"
            blockers.append("incremental_action_missing_or_invalid")
        blockers.extend(list(decision.get("position_action_blockers") or []))
        blockers.extend(list(decision.get("incremental_action_blockers") or []))
        if metadata.get("quality_context_status") != "complete":
            blockers.append("quality_context_not_complete")
        evidence = metadata.get("decision_evidence")
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("status") != "complete"
            or evidence.get("reference_status") != "matched"
        ):
            blockers.append("decision_evidence_not_complete")
        reasons.extend(blockers)
        return {
            "position_action": position_action,
            "incremental_action": incremental_action,
        }, reasons, PortfolioResearchBaselineService._unique(blockers)

    def _build_candidates(self, items: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in items:
            reasons = list(row.get("exception_reasons") or [])
            if not reasons:
                continue
            selection_key = str(row["selection_key"])
            candidate = grouped.setdefault(
                selection_key,
                {
                    "selection_key": selection_key,
                    "display_label": row["display_label"],
                    "market": row["market"],
                    "symbol": row["symbol"],
                    "account_ids": [],
                    "reasons": [],
                    "priority": 99,
                },
            )
            candidate["account_ids"].append(row.get("account_id"))
            candidate["reasons"].extend(reasons)
            candidate["priority"] = min(candidate["priority"], self._candidate_priority(reasons))
        result = []
        for candidate in grouped.values():
            candidate["account_ids"] = sorted(set(candidate["account_ids"]))
            candidate["reasons"] = self._unique(candidate["reasons"])
            result.append(candidate)
        result.sort(key=lambda item: (item["priority"], item["selection_key"]))
        return result

    @staticmethod
    def _candidate_priority(reasons: Iterable[str]) -> int:
        values = set(reasons)
        if values & {"position_action_exit", "daily_reset_product_requires_deep_review"}:
            return 0
        if "position_action_reduce" in values:
            return 1
        if values & {
            "incremental_action_add_in_batches",
            "nav_premium_missing",
            "adr_parity_required",
        }:
            return 2
        if any("risk" in value or "price" in value for value in values):
            return 3
        return 4

    @staticmethod
    def _product_reasons(instrument: Optional[Mapping[str, Any]]) -> list[str]:
        if not isinstance(instrument, Mapping):
            return ["instrument_identity_missing"]
        reasons = PortfolioResearchBaselineService._product_evidence_blockers(instrument)
        instrument_type = str(instrument.get("instrument_type") or "unknown")
        if instrument_type == "adr_ads":
            reasons.append("adr_parity_required")
        if instrument_type == "daily_leveraged_product" or instrument.get("daily_reset") is True:
            reasons.append("daily_reset_product_requires_deep_review")
        return PortfolioResearchBaselineService._unique(reasons)

    @staticmethod
    def _product_evidence_blockers(instrument: Mapping[str, Any]) -> list[str]:
        instrument_type = str(instrument.get("instrument_type") or "unknown")
        requires_qdii_evidence = bool(
            instrument_type == "qdii"
            or instrument.get("requires_premium_check") is True
        )
        requires_daily_reset_evidence = bool(
            instrument_type == "daily_leveraged_product"
            or instrument.get("daily_reset") is True
        )
        if not requires_qdii_evidence and not requires_daily_reset_evidence:
            return []
        evidence = instrument.get("product_evidence")
        if (
            isinstance(evidence, Mapping)
            and evidence.get("status") == "ready"
            and not evidence.get("blockers")
        ):
            return []
        if isinstance(evidence, Mapping):
            blockers = [
                str(item).strip()
                for item in evidence.get("blockers") or []
                if str(item).strip()
            ]
            if blockers:
                return PortfolioResearchBaselineService._unique(blockers)
        if requires_daily_reset_evidence:
            return ["daily_reset_product_evidence_incomplete"]
        return ["nav_premium_missing"]

    @staticmethod
    def _snapshot_blockers(
        snapshot: Mapping[str, Any],
        *,
        position: Mapping[str, Any],
    ) -> list[str]:
        market, symbol = PortfolioResearchBaselineService._position_key(position)
        account_id = position.get("account_id")
        blockers: list[str] = []
        for item in snapshot.get("hard_blockers") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("account_id") not in (None, account_id):
                continue
            if item.get("market") not in (None, "", market):
                continue
            item_symbol = str(item.get("symbol") or "").strip().upper()
            if item_symbol and canonical_stock_code(item_symbol) != symbol:
                continue
            code = str(item.get("code") or "").strip()
            if code:
                blockers.append(code)
        return PortfolioResearchBaselineService._unique(blockers)

    @staticmethod
    def _portfolio_risk_flags(snapshot: Mapping[str, Any]) -> list[Dict[str, Any]]:
        risk_budget = snapshot.get("risk_budget")
        if not isinstance(risk_budget, Mapping):
            return []
        return [
            dict(item)
            for item in risk_budget.get("breaches") or []
            if isinstance(item, Mapping) and str(item.get("code") or "").strip()
        ]

    @staticmethod
    def _row_risk_flags(
        snapshot: Mapping[str, Any],
        *,
        position: Mapping[str, Any],
        instrument: Optional[Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        risk_budget = snapshot.get("risk_budget")
        if not isinstance(risk_budget, Mapping):
            return []
        account_id = position.get("account_id")
        account = next(
            (
                item
                for item in snapshot.get("accounts") or []
                if isinstance(item, Mapping) and item.get("account_id") == account_id
            ),
            None,
        )
        currency = str((account or {}).get("base_currency") or "").strip().upper()
        market, symbol = PortfolioResearchBaselineService._position_key(position)
        scope = next(
            (
                item
                for item in risk_budget.get("scopes") or []
                if isinstance(item, Mapping)
                and str(item.get("currency") or "").strip().upper() == currency
            ),
            None,
        )
        flags = []
        for breach in risk_budget.get("breaches") or []:
            if not isinstance(breach, Mapping):
                continue
            if str(breach.get("currency") or "").strip().upper() != currency:
                continue
            code = str(breach.get("code") or "").strip()
            applies = False
            if code == "single_position_above_maximum" and isinstance(scope, Mapping):
                maximum = scope.get("max_single_position")
                applies = bool(
                    isinstance(maximum, Mapping)
                    and maximum.get("account_id") == account_id
                    and str(maximum.get("market") or "").strip().lower() == market
                    and canonical_stock_code(str(maximum.get("symbol") or "")).upper() == symbol
                )
            elif code == "high_risk_product_above_maximum":
                applies = bool(
                    isinstance(instrument, Mapping)
                    and (
                        instrument.get("instrument_type") == "daily_leveraged_product"
                        or instrument.get("daily_reset") is True
                    )
                )
            if applies:
                flags.append(dict(breach))
        return flags

    @staticmethod
    def _positive_positions(raw: Any) -> list[Dict[str, Any]]:
        result = []
        for item in raw or []:
            if not isinstance(item, Mapping):
                continue
            try:
                quantity = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if quantity > 0:
                result.append(dict(item))
        result.sort(
            key=lambda item: (
                int(item.get("account_id") or 0),
                *PortfolioResearchBaselineService._position_key(item),
            )
        )
        return result

    @staticmethod
    def _position_key(item: Mapping[str, Any]) -> PositionKey:
        market = str(item.get("market") or "").strip().lower()
        symbol = canonical_stock_code(str(item.get("symbol") or "").strip()).upper()
        return market, symbol

    @staticmethod
    def _signals_by_key(
        signals: Sequence[Mapping[str, Any]],
    ) -> Dict[PositionKey, list[Dict[str, Any]]]:
        result: Dict[PositionKey, list[Dict[str, Any]]] = {}
        for signal in signals:
            if not isinstance(signal, Mapping):
                continue
            key = (
                str(signal.get("market") or "").strip().lower(),
                canonical_stock_code(str(signal.get("stock_code") or "").strip()).upper(),
            )
            result.setdefault(key, []).append(dict(signal))
        return result

    @staticmethod
    def _select_signal(
        signals: Any,
        *,
        account_id: Any,
    ) -> Optional[Dict[str, Any]]:
        candidates = [dict(item) for item in signals or [] if isinstance(item, Mapping)]
        for signal in candidates:
            metadata = signal.get("metadata")
            decision = metadata.get("portfolio_decision") if isinstance(metadata, Mapping) else None
            if isinstance(decision, Mapping) and decision.get("account_id") == account_id:
                return signal
        for signal in candidates:
            metadata = signal.get("metadata")
            decision = metadata.get("portfolio_decision") if isinstance(metadata, Mapping) else None
            if not isinstance(decision, Mapping) or decision.get("account_id") is None:
                return signal
        return candidates[0] if candidates else None

    @staticmethod
    def _public_evidence(value: Any) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"available": False, "source": "none"}
        return {key: item for key, item in value.items() if key != "data"}

    @staticmethod
    def _clean_name(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _normalized_name(value: str) -> str:
        return "".join(str(value).split()).casefold()

    @classmethod
    def _names_match(
        cls,
        *,
        symbol: str,
        canonical_name: str,
        observed_name: str,
    ) -> bool:
        canonical = cls._normalized_name(canonical_name)
        observed = cls._normalized_name(observed_name)
        if canonical == observed:
            return True

        company_suffixes = (
            "集团股份有限公司",
            "股份有限公司",
            "集团有限公司",
            "有限公司",
        )

        def without_company_suffix(value: str) -> str:
            for suffix in company_suffixes:
                if value.endswith(suffix):
                    return value[: -len(suffix)]
            return value

        if without_company_suffix(canonical) == without_company_suffix(observed):
            return True

        ignored_legal_tokens = {
            "class",
            "co",
            "company",
            "corp",
            "corporation",
            "inc",
            "incorporated",
            "limited",
            "ltd",
            "plc",
        }

        def english_identity_tokens(value: str) -> tuple[str, ...]:
            return tuple(
                token
                for token in re.findall(r"[a-z0-9]+", value.casefold())
                if token not in ignored_legal_tokens
            )

        canonical_tokens = english_identity_tokens(canonical_name)
        observed_tokens = english_identity_tokens(observed_name)
        if canonical_tokens and canonical_tokens == observed_tokens:
            return True

        from src.data.stock_mapping import foreign_stock_english_aliases

        aliases = foreign_stock_english_aliases(symbol, observed_name)
        return canonical in {cls._normalized_name(alias) for alias in aliases}

    @staticmethod
    def _unique(values: Iterable[Any]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))

    @staticmethod
    def _load_names(keys: list[PositionKey]) -> Mapping[PositionKey, str]:
        from src.data.stock_index_loader import get_index_stock_name
        from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

        result: Dict[PositionKey, str] = {}
        for key in keys:
            symbol = key[1]
            name = STOCK_NAME_MAP.get(symbol) or get_index_stock_name(symbol)
            if is_meaningful_stock_name(name, symbol):
                result[key] = str(name).strip()
        return result

    @staticmethod
    def _quotes_from_positions(
        positions: Sequence[Mapping[str, Any]],
    ) -> Mapping[PositionKey, Mapping[str, Any]]:
        result: Dict[PositionKey, Mapping[str, Any]] = {}
        ordered = sorted(
            positions,
            key=lambda item: str(item.get("cache_updated_at") or ""),
            reverse=True,
        )
        for position in ordered:
            key = PortfolioResearchBaselineService._position_key(position)
            if key in result:
                continue
            available = position.get("price_available") is True
            result[key] = {
                "available": available,
                "price": position.get("last_price") if available else None,
                "source": "portfolio_research_snapshot",
                "as_of": position.get("cache_updated_at"),
                "stale": position.get("price_stale") is True,
            }
        return result

    @staticmethod
    def _load_histories(
        keys: list[PositionKey],
        cutoff: str,
    ) -> Mapping[PositionKey, Mapping[str, Any]]:
        from src.services.history_loader import _select_best_bars
        from src.storage import get_db

        try:
            end = datetime.fromisoformat(cutoff.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            end = date.today()
        start = end - timedelta(days=240)
        db = get_db()
        result: Dict[PositionKey, Mapping[str, Any]] = {}
        for key in keys:
            try:
                _code, bars = _select_best_bars(db, key[1], start, end)
                frame = pd.DataFrame([bar.to_dict() for bar in bars])
                result[key] = {
                    "available": not frame.empty and len(frame) >= 20,
                    "source": "db_cache" if not frame.empty else "none",
                    "bar_count": len(frame),
                    "as_of": str(frame.iloc[-1].get("date")) if not frame.empty else None,
                    "data": frame,
                }
            except Exception as exc:
                logger.warning("Fast portfolio history load failed for %s:%s: %s", key[0], key[1], exc)
                result[key] = {"available": False, "source": "none", "bar_count": 0}
        return result

    @staticmethod
    def _load_signals(keys: list[PositionKey]) -> Sequence[Mapping[str, Any]]:
        from src.services.decision_signal_service import DecisionSignalService

        payload = DecisionSignalService().list_signals(
            stock_identities=keys,
            status="active",
            page=1,
            page_size=100,
        )
        return list(payload.get("items") or [])

    @staticmethod
    def _load_trend(symbol: str, history: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        from src.stock_analyzer import StockTrendAnalyzer

        frame = history.get("data")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        return StockTrendAnalyzer().analyze(frame, symbol).to_dict()
