# -*- coding: utf-8 -*-
"""Frozen, hashed, read-only DSA portfolio research snapshot."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, desc, or_, select

from src.config import get_config
from src.core.trading_calendar import resolve_market_daily_bar_as_of
from src.repositories.decision_evidence_snapshot_repo import (
    DecisionEvidenceSnapshotRepository,
)
from src.repositories.portfolio_market_evidence_repo import (
    PortfolioMarketEvidenceRepository,
)
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.portfolio_instrument_service import PortfolioInstrumentService
from src.services.portfolio_research_scope import (
    position_scope_key,
    research_scope_payload,
    resolve_research_scope,
)
from src.services.portfolio_research_product_evidence import (
    product_evidence_from_instrument,
    validate_prepared_product_evidence,
)
from src.services.portfolio_research_market_evidence import (
    daily_bar_not_final,
    daily_bar_stale,
)
from src.services.strategy_registry_service import load_strategy_manifest
from src.storage import (
    DecisionSignalRecord,
    PortfolioInstrument,
    PortfolioPosition,
    PortfolioRiskPolicy,
)
from src.utils.sanitize import sanitize_decision_signal_payload


RESEARCH_SNAPSHOT_SCHEMA_VERSION = "portfolio-research-snapshot-v1"
STRATEGY_BENCHMARK_EVIDENCE_VERSION = "strategy-benchmark-policy-v1"
MARKET_BAR_EVIDENCE_VERSION = "stock-daily-cache-v1"
PORTFOLIO_ACCOUNT_EVIDENCE_VERSION = "portfolio-account-v1"
PORTFOLIO_INSTRUMENT_EVIDENCE_VERSION = "portfolio-instrument-v1"
PORTFOLIO_RISK_POLICY_EVIDENCE_VERSION = "portfolio-risk-policy-v1"
PORTFOLIO_RISK_BUDGET_EVIDENCE_VERSION = "portfolio-risk-budget-v1"
PORTFOLIO_FX_EVIDENCE_VERSION = "portfolio-fx-cache-v1"
FX_MAX_AGE = timedelta(days=7)
SUPPORTED_BENCHMARK_MARKETS = frozenset({"cn", "hk", "us", "jp", "kr"})
FROZEN_RESEARCH_SNAPSHOT_CONTEXT_KEY = "_frozen_research_snapshot"
POINT_IN_TIME_SCOPE = "current_prospective"


class PortfolioResearchSnapshotService:
    """Build the minimum deterministic DSA truth package used by research gates."""

    def __init__(
        self,
        repo: Optional[PortfolioRepository] = None,
        *,
        stock_repo: Optional[StockRepository] = None,
        market_evidence_repo: Optional[PortfolioMarketEvidenceRepository] = None,
        decision_evidence_repo: Optional[DecisionEvidenceSnapshotRepository] = None,
        max_price_age_hours: float = 72.0,
        max_decision_signals: int = 100,
    ):
        self.repo = repo or PortfolioRepository()
        self.stock_repo = stock_repo or StockRepository(self.repo.db)
        self.market_evidence_repo = market_evidence_repo or (
            PortfolioMarketEvidenceRepository(self.repo.db)
        )
        self.decision_evidence_repo = decision_evidence_repo or (
            DecisionEvidenceSnapshotRepository(self.repo.db)
        )
        self.max_price_age = timedelta(hours=max_price_age_hours)
        self.max_decision_signals = max(0, int(max_decision_signals))

    def build(
        self,
        *,
        cutoff: Optional[datetime] = None,
        scope: Optional[Sequence[Any]] = None,
        prepared_product_evidence_items: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        cutoff_value = self._utc_naive(cutoff or datetime.now(timezone.utc))
        all_accounts = self.repo.list_accounts(include_inactive=False)
        all_positions = self.repo.list_cached_positions(cost_method="fifo")
        positive_positions = [
            position
            for position in all_positions
            if float(getattr(position, "quantity", 0) or 0) > 0
        ]
        resolved_scope = resolve_research_scope(
            scope,
            positive_positions=positive_positions,
        )
        scope_keys = set(resolved_scope)
        selected_account_ids = {account_id for account_id, _, _ in resolved_scope}
        accounts = [account for account in all_accounts if account.id in selected_account_ids]
        positions = [
            position for position in positive_positions if position_scope_key(position) in scope_keys
        ]
        account_risk_positions = [
            position
            for position in positive_positions
            if position.account_id in selected_account_ids
        ]
        all_instruments = {
            (str(row.market).lower(), str(row.symbol).upper()): row
            for row in self.repo.list_instruments()
        }
        selected_identities = {(market, symbol) for _, market, symbol in resolved_scope}
        instruments = {
            identity: row
            for identity, row in all_instruments.items()
            if identity in selected_identities
        }
        prepared_product_evidence = self._prepared_product_evidence(
            items=prepared_product_evidence_items,
            resolved_scope=resolved_scope,
            instruments=instruments,
            cutoff=cutoff_value.replace(tzinfo=timezone.utc),
        )
        risk_instruments = {
            identity: row
            for identity, row in all_instruments.items()
            if identity
            in {
                (
                    str(position.market or "").strip().lower(),
                    str(position.symbol or "").strip().upper(),
                )
                for position in account_risk_positions
            }
        }
        risk_policy = self.repo.get_risk_policy()
        daily_rows = [
            row
            for row in self.repo.list_daily_snapshots_for_risk(
                as_of=cutoff_value.date(),
                cost_method="fifo",
                lookback_days=0,
            )
            if row.account_id in selected_account_ids
        ]
        latest_daily = {}
        for row in daily_rows:
            current = latest_daily.get(row.account_id)
            if current is None or row.snapshot_date > current.snapshot_date:
                latest_daily[row.account_id] = row

        account_payload = self._accounts_payload(accounts, latest_daily)
        blockers: List[Dict[str, Any]] = []
        if risk_policy is None:
            blockers.append({
                "code": "portfolio_risk_policy_missing",
                "scope": "portfolio",
            })

        position_payload = []
        bar_by_identity: Dict[Tuple[str, str], Any] = {}
        for position in positions:
            identity = (
                str(position.market or "").strip().lower(),
                str(position.symbol or "").strip().upper(),
            )
            instrument = instruments.get(identity)
            if instrument is None:
                blockers.append({
                    "code": "instrument_identity_missing",
                    "scope": "instrument",
                    "account_id": position.account_id,
                    "market": identity[0],
                    "symbol": identity[1],
                })
            elif instrument.verification_status != "verified":
                blockers.append({
                    "code": "instrument_identity_unverified",
                    "scope": "instrument",
                    "account_id": position.account_id,
                    "market": identity[0],
                    "symbol": identity[1],
                    "verification_status": instrument.verification_status,
                })
            price_batch = self.market_evidence_repo.get_latest_batch(
                code=identity[1],
                cutoff=cutoff_value,
            )
            price_bar = price_batch.rows[-1] if price_batch is not None else None
            bar_by_identity[identity] = price_bar
            position_payload.append(
                self._position_payload(
                    position,
                    price_bar=price_bar,
                    cutoff=cutoff_value,
                    blockers=blockers,
                )
            )

        instrument_payload = [
            self._instrument_payload(
                row,
                cutoff=cutoff_value.replace(tzinfo=timezone.utc),
                adjustment_identity=(
                    getattr(bar_by_identity.get(key), "adjustment_identity", None)
                    or StockRepository._adjustment_marker(
                        getattr(bar_by_identity.get(key), "data_source", None)
                    )
                ),
                prepared_product_evidence=prepared_product_evidence.get(key),
            )
            for key, row in instruments.items()
        ]

        decision_signals, signal_rows, signals_truncated = self._load_decision_signals(
            positions=position_payload,
            cutoff=cutoff_value,
        )
        point_in_time = self._point_in_time_payload(
            cutoff=cutoff_value,
            accounts=accounts,
            positions=positions,
            daily_rows=daily_rows,
            instruments=list(instruments.values()),
            risk_policy=risk_policy,
            signal_rows=signal_rows,
            signals_truncated=signals_truncated,
        )
        blockers.extend(
            {"code": code, "scope": "point_in_time"}
            for code in point_in_time["blockers"]
        )

        if not positions:
            blockers.append({
                "code": "portfolio_positions_missing",
                "scope": "portfolio",
            })

        position_payload.sort(
            key=lambda item: (item["account_id"], item["market"], item["symbol"], item["currency"])
        )
        instrument_payload.sort(key=lambda item: (item["market"], item["symbol"]))
        risk_position_payload = []
        for position in account_risk_positions:
            risk_symbol = str(position.symbol or "").strip().upper()
            risk_batch = self.market_evidence_repo.get_latest_batch(
                code=risk_symbol,
                cutoff=cutoff_value,
            )
            risk_position_payload.append(
                self._position_payload(
                    position,
                    price_bar=(risk_batch.rows[-1] if risk_batch and risk_batch.rows else None),
                    cutoff=cutoff_value,
                    blockers=[],
                )
            )
        risk_budget = self._evaluate_risk_budget(
            accounts=accounts,
            positions=risk_position_payload,
            instruments=risk_instruments,
            latest_daily=latest_daily,
            daily_rows=daily_rows,
            risk_policy=risk_policy,
        )
        risk_budget = self._with_evidence_metadata(
            risk_budget,
            as_of_field="as_of",
            as_of=self._iso_utc(cutoff_value),
            source="portfolio_research_snapshot",
            source_version=PORTFOLIO_RISK_BUDGET_EVIDENCE_VERSION,
        )
        benchmark_payload = self._benchmark_payload(
            position_payload,
            cutoff=cutoff_value,
        )
        for benchmark in benchmark_payload:
            benchmark_blocker = {
                "scope": "benchmark",
                "market": benchmark["market"],
                "symbol": benchmark["code"],
            }
            if benchmark.get("price") is None:
                blockers.append({"code": "benchmark_price_missing", **benchmark_blocker})
            elif benchmark.get("not_final"):
                blockers.append({"code": "benchmark_price_not_final", **benchmark_blocker})
            elif benchmark.get("stale"):
                blockers.append({"code": "benchmark_price_stale", **benchmark_blocker})
        benchmark_by_market = {
            benchmark["market"]: benchmark
            for benchmark in benchmark_payload
        }
        for position in position_payload:
            benchmark = benchmark_by_market.get(position["market"])
            if benchmark is None:
                continue
            position_adjustment = position.get("adjustment_identity")
            benchmark_adjustment = benchmark.get("adjustment_identity")
            adjustment_blocker = {
                "scope": "position",
                "account_id": position["account_id"],
                "market": position["market"],
                "symbol": position["symbol"],
                "benchmark_symbol": benchmark["code"],
            }
            if not position_adjustment:
                blockers.append({
                    "code": "position_adjustment_identity_unknown",
                    **adjustment_blocker,
                })
            if not benchmark_adjustment:
                blockers.append({
                    "code": "benchmark_adjustment_identity_unknown",
                    **adjustment_blocker,
                })
            if (
                position_adjustment
                and benchmark_adjustment
                and position_adjustment != benchmark_adjustment
            ):
                blockers.append({
                    "code": "benchmark_adjustment_identity_mismatch",
                    **adjustment_blocker,
                })
        blockers.sort(
            key=lambda item: (
                item.get("scope", ""),
                item.get("account_id", 0),
                item.get("market", ""),
                item.get("symbol", ""),
                item["code"],
            )
        )
        universe_hash = self._hash({
            "positions": [
                {
                    "account_id": item["account_id"],
                    "market": item["market"],
                    "symbol": item["symbol"],
                    "currency": item["currency"],
                    "quantity": item["quantity"],
                }
                for item in position_payload
            ]
        })
        scope_payload = research_scope_payload(resolved_scope)
        payload = {
            "schema_version": RESEARCH_SNAPSHOT_SCHEMA_VERSION,
            "cutoff": cutoff_value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "timezone": "UTC",
            "cost_method": "fifo",
            "analysis_runtime": self._analysis_runtime_payload(),
            "scope": scope_payload,
            "scope_hash": self._hash(scope_payload),
            "universe_hash": universe_hash,
            "accounts": account_payload,
            "positions": position_payload,
            "instruments": instrument_payload,
            "benchmarks": benchmark_payload,
            "risk_policy": self._risk_policy_payload(risk_policy),
            "risk_budget": risk_budget,
            "point_in_time": point_in_time,
            "decision_signals": decision_signals,
            "hard_blockers": blockers,
            "limitations": ["cached_portfolio_state_only"],
            "completeness": "COMPLETE" if not blockers else "INSUFFICIENT_EVIDENCE",
        }
        if not risk_budget["evaluated"]:
            payload["limitations"].append(
                "portfolio_risk_budget_thresholds_not_evaluated"
            )
        payload["execution_identity_hash"] = self._execution_identity_hash(payload)
        payload["snapshot_hash"] = self._hash(payload)
        return payload

    def _load_decision_signals(
        self,
        *,
        positions: List[Dict[str, Any]],
        cutoff: datetime,
    ) -> Tuple[List[Dict[str, Any]], List[DecisionSignalRecord], bool]:
        reference_keys = sorted(
            {
                (
                    item.get("account_id"),
                    str(item.get("market") or "").lower(),
                    str(item.get("symbol") or "").upper(),
                )
                for item in positions
                if float(item.get("quantity") or 0.0) > 0
            }
        )
        if not reference_keys:
            return [], [], False

        identities = sorted({(market, symbol) for _, market, symbol in reference_keys})
        identity_filters = [
            and_(
                DecisionSignalRecord.market == market,
                DecisionSignalRecord.stock_code == symbol,
            )
            for market, symbol in identities
        ]
        with self.repo.db.get_session() as session:
            rows = list(
                session.execute(
                    select(DecisionSignalRecord)
                    .where(
                        DecisionSignalRecord.status == "active",
                        or_(*identity_filters),
                        or_(
                            DecisionSignalRecord.created_at.is_(None),
                            DecisionSignalRecord.created_at <= cutoff,
                        ),
                        or_(
                            DecisionSignalRecord.expires_at.is_(None),
                            DecisionSignalRecord.expires_at > cutoff,
                        ),
                    )
                    .order_by(
                        desc(DecisionSignalRecord.created_at),
                        desc(DecisionSignalRecord.id),
                    )
                ).scalars()
            )
            for row in rows:
                session.expunge(row)

        rows_by_identity: Dict[Tuple[str, str], List[DecisionSignalRecord]] = {}
        for row in rows:
            identity = (
                str(row.market or "").strip().lower(),
                str(row.stock_code or "").strip().upper(),
            )
            rows_by_identity.setdefault(identity, []).append(row)

        projected_rows: List[DecisionSignalRecord] = []
        selected_ids = set()
        for account_id, market, symbol in reference_keys:
            selected = self._select_reference_signal(
                rows_by_identity.get((market, symbol), []),
                account_id=account_id,
            )
            if selected is None or selected.id in selected_ids:
                continue
            projected_rows.append(selected)
            selected_ids.add(selected.id)

        truncated = len(projected_rows) > self.max_decision_signals
        captured_rows = projected_rows[: self.max_decision_signals]
        payload = [self._decision_signal_payload(row) for row in captured_rows]
        payload.sort(
            key=lambda item: (
                item["market"],
                item["stock_code"],
                item["created_at"] or "",
                item["id"],
            ),
            reverse=True,
        )
        return payload, captured_rows, truncated

    @classmethod
    def _select_reference_signal(
        cls,
        rows: List[DecisionSignalRecord],
        *,
        account_id: Any,
    ) -> Optional[DecisionSignalRecord]:
        for row in rows:
            if cls._decision_signal_account_id(row) == account_id:
                return row
        for row in rows:
            if cls._decision_signal_account_id(row) is None:
                return row
        return rows[0] if rows else None

    @staticmethod
    def _decision_signal_account_id(row: DecisionSignalRecord) -> Any:
        try:
            metadata = json.loads(row.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        decision = metadata.get("portfolio_decision")
        if not isinstance(decision, dict):
            return None
        return decision.get("account_id")

    def _point_in_time_payload(
        self,
        *,
        cutoff: datetime,
        accounts: List[Any],
        positions: List[PortfolioPosition],
        daily_rows: List[Any],
        instruments: List[PortfolioInstrument],
        risk_policy: Optional[PortfolioRiskPolicy],
        signal_rows: List[DecisionSignalRecord],
        signals_truncated: bool,
    ) -> Dict[str, Any]:
        sources = {
            "accounts": (
                accounts,
                ("updated_at",),
                "account_state",
                True,
            ),
            "position_cache": (
                positions,
                ("updated_at",),
                "position_cache",
                True,
            ),
            "daily_snapshots": (
                daily_rows,
                ("updated_at",),
                "daily_snapshot",
                True,
            ),
            "instrument_registry": (
                instruments,
                ("updated_at",),
                "instrument_registry",
                False,
            ),
            "risk_policy": (
                [risk_policy] if risk_policy is not None else [],
                ("updated_at",),
                "risk_policy",
                False,
            ),
            "decision_signals": (
                signal_rows,
                ("created_at", "updated_at"),
                "decision_signal",
                False,
            ),
        }
        source_cutoffs: Dict[str, Optional[str]] = {}
        blockers: List[str] = []
        for source_name, (rows, fields, blocker_prefix, legacy_local) in sources.items():
            timestamps: List[datetime] = []
            missing = False
            for row in rows:
                for field in fields:
                    value = getattr(row, field, None)
                    if value is None:
                        missing = True
                        continue
                    timestamps.append(
                        self._legacy_local_to_utc_naive(value)
                        if legacy_local
                        else self._utc_naive(value)
                    )
            source_cutoff = max(timestamps) if timestamps else None
            source_cutoffs[source_name] = self._iso_utc(source_cutoff)
            if rows and missing:
                blockers.append(f"{blocker_prefix}_cutoff_missing")
            if source_cutoff is not None and source_cutoff > cutoff:
                blockers.append(f"{blocker_prefix}_after_cutoff")

        if signals_truncated:
            blockers.append("decision_signal_snapshot_truncated")
        blockers = sorted(set(blockers))
        return {
            "scope": POINT_IN_TIME_SCOPE,
            "prospective_decision_eligible": not blockers,
            "historical_replay_eligible": False,
            "source_cutoffs": source_cutoffs,
            "blockers": blockers,
        }

    def _decision_signal_payload(self, row: DecisionSignalRecord) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        raw_metadata: Dict[str, Any] = {}
        if row.metadata_json:
            try:
                loaded_metadata = json.loads(row.metadata_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                loaded_metadata = {}
            if isinstance(loaded_metadata, dict):
                raw_metadata = loaded_metadata
                metadata = {
                    key: raw_metadata[key]
                    for key in (
                        "quality_context_status",
                        "quality_context_unable_reasons",
                        "portfolio_decision",
                    )
                    if key in raw_metadata
                }
        metadata["decision_evidence"] = self._decision_evidence_summary(
            signal_id=int(row.id),
            metadata=raw_metadata,
        )
        return {
            "id": row.id,
            "market": str(row.market or "").lower(),
            "stock_code": str(row.stock_code or "").upper(),
            "stock_name": row.stock_name,
            "reason": row.reason,
            "status": row.status,
            "created_at": self._iso_utc(self._utc_naive(row.created_at)) if row.created_at else None,
            "updated_at": self._iso_utc(self._utc_naive(row.updated_at)) if row.updated_at else None,
            "metadata": sanitize_decision_signal_payload(metadata),
        }

    def _decision_evidence_summary(
        self,
        *,
        signal_id: int,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        sidecar = self.decision_evidence_repo.get_by_signal_id(signal_id=signal_id)
        if sidecar is None:
            return {
                "status": "missing",
                "display_status": "资料不足",
                "reference_status": "missing",
                "unable_reasons": ["legacy_evidence_snapshot_missing"],
            }

        try:
            blockers = json.loads(sidecar.blockers_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            blockers = ["decision_evidence_blockers_invalid"]
        if not isinstance(blockers, list):
            blockers = ["decision_evidence_blockers_invalid"]

        references_match = (
            metadata.get("decision_evidence_snapshot_id") == sidecar.id
            and metadata.get("decision_evidence_research_snapshot_hash")
            == sidecar.snapshot_hash
            and metadata.get("decision_evidence_bundle_hash")
            == sidecar.evidence_bundle_hash
            and metadata.get("decision_evidence_input_hash")
            == sidecar.decision_input_hash
        )
        if not references_match:
            blockers = [*blockers, "decision_evidence_reference_mismatch"]
        blockers = list(dict.fromkeys(str(item) for item in blockers if item))
        complete = (
            sidecar.readiness_status == "complete"
            and references_match
            and not blockers
        )
        return {
            "status": "complete" if complete else "insufficient_evidence",
            "display_status": "已保存" if complete else "资料不足",
            "reference_status": "matched" if references_match else "mismatch",
            "unable_reasons": blockers,
        }

    @staticmethod
    def _legacy_local_to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.astimezone().astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _iso_utc(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    def _benchmark_payload(
        self,
        positions: List[Dict[str, Any]],
        *,
        cutoff: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        benchmark_policy = load_strategy_manifest()["benchmark_policy"]["benchmarks"]
        cutoff_value = cutoff or self._utc_naive(datetime.now(timezone.utc))
        markets = sorted(
            {
                str(item.get("market") or "").strip().lower()
                for item in positions
            }
            & SUPPORTED_BENCHMARK_MARKETS
            & set(benchmark_policy)
        )
        payload = []
        for market in markets:
            code = benchmark_policy[market]
            batch = self.market_evidence_repo.get_latest_batch(
                code=code,
                cutoff=cutoff_value,
            )
            bar = batch.rows[-1] if batch is not None else None
            evidence = self._market_bar_evidence(bar, market=market)
            evidence_as_of = self._evidence_datetime(evidence.get("as_of"))
            not_final = bool(
                evidence_as_of is not None
                and self._daily_bar_not_final(
                    market=market,
                    as_of=evidence_as_of,
                    cutoff=cutoff_value,
                )
            )
            stale = bool(
                evidence_as_of is None
                or not_final
                or (
                    evidence_as_of is not None
                    and daily_bar_stale(
                        market=market,
                        as_of=evidence_as_of,
                        cutoff=cutoff_value,
                    )
                )
                or cutoff_value - evidence_as_of > self.max_price_age
            )
            payload.append({
                "market": market,
                "code": code,
                "type": "strategy_benchmark",
                "policy_source": "portfolio_current_policy_v1.json",
                "policy_version": STRATEGY_BENCHMARK_EVIDENCE_VERSION,
                "price": evidence.get("price"),
                "adjustment_identity": evidence.get("adjustment_identity"),
                "evidence_source": evidence.get("source"),
                "evidence_version": evidence.get("source_version"),
                "evidence_as_of": evidence.get("as_of"),
                "captured_at": evidence.get("captured_at"),
                "evidence_hash": evidence.get("source_hash"),
                "evidence_batch_hash": evidence.get("batch_hash"),
                "not_final": not_final,
                "stale": stale,
            })
        return payload

    @staticmethod
    def _analysis_runtime_payload() -> Dict[str, Any]:
        architecture = get_config().agent_arch
        return {
            "architecture": architecture,
            "automatic_multi_agent": architecture == "multi",
        }

    def _evaluate_risk_budget(
        self,
        *,
        accounts: List[Any],
        positions: List[Dict[str, Any]],
        instruments: Dict[Tuple[str, str], PortfolioInstrument],
        latest_daily: Dict[int, Any],
        daily_rows: List[Any],
        risk_policy: Optional[PortfolioRiskPolicy],
    ) -> Dict[str, Any]:
        evidence_blockers: List[Dict[str, Any]] = []
        breaches: List[Dict[str, Any]] = []
        scopes: List[Dict[str, Any]] = []
        thresholds = self._risk_policy_payload(risk_policy)

        if risk_policy is None:
            evidence_blockers.append({
                "code": "risk_policy_missing",
                "scope": "portfolio",
            })
        if not positions:
            evidence_blockers.append({
                "code": "risk_positions_missing",
                "scope": "portfolio",
            })

        accounts_by_currency: Dict[str, List[Any]] = defaultdict(list)
        account_currency: Dict[int, str] = {}
        for account in accounts:
            currency = str(account.base_currency or "").strip().upper()
            if not currency:
                evidence_blockers.append({
                    "code": "risk_account_currency_missing",
                    "scope": "account",
                    "account_id": account.id,
                })
                continue
            accounts_by_currency[currency].append(account)
            account_currency[account.id] = currency

        positions_by_currency: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for position in positions:
            currency = account_currency.get(position["account_id"])
            if currency is not None:
                positions_by_currency[currency].append(position)

        for currency in sorted(accounts_by_currency):
            scope_accounts = accounts_by_currency[currency]
            account_ids = {account.id for account in scope_accounts}
            scope_positions = positions_by_currency.get(currency, [])
            scope_blockers: List[Dict[str, Any]] = []
            current_rows = []
            for account in scope_accounts:
                daily = latest_daily.get(account.id)
                if daily is None:
                    scope_blockers.append({
                        "code": "risk_current_snapshot_missing",
                        "scope": "account",
                        "currency": currency,
                        "account_id": account.id,
                    })
                    continue
                current_rows.append(daily)
                if str(daily.base_currency or "").strip().upper() != currency:
                    scope_blockers.append({
                        "code": "risk_current_snapshot_currency_mismatch",
                        "scope": "account",
                        "currency": currency,
                        "account_id": account.id,
                    })
                if bool(daily.fx_stale):
                    scope_blockers.append({
                        "code": "risk_current_snapshot_fx_stale",
                        "scope": "account",
                        "currency": currency,
                        "account_id": account.id,
                    })

            total_cash = sum(float(row.total_cash or 0.0) for row in current_rows)
            total_equity = sum(float(row.total_equity or 0.0) for row in current_rows)
            if total_equity <= 0:
                scope_blockers.append({
                    "code": "risk_total_equity_non_positive",
                    "scope": "currency",
                    "currency": currency,
                })

            sector_values: Dict[str, float] = defaultdict(float)
            high_risk_value = 0.0
            max_position: Optional[Dict[str, Any]] = None
            for position in scope_positions:
                blocker_base = {
                    "scope": "instrument",
                    "currency": currency,
                    "account_id": position["account_id"],
                    "market": position["market"],
                    "symbol": position["symbol"],
                }
                if position["valuation_currency"] != currency:
                    scope_blockers.append({
                        "code": "risk_position_valuation_currency_mismatch",
                        **blocker_base,
                    })
                if not position["price_available"]:
                    scope_blockers.append({
                        "code": "risk_position_price_missing",
                        **blocker_base,
                    })
                elif position["price_stale"]:
                    scope_blockers.append({
                        "code": "risk_position_price_stale",
                        **blocker_base,
                    })

                market_value = float(position["market_value_base"] or 0.0)
                position_weight = (
                    market_value / total_equity * 100.0
                    if total_equity > 0
                    else 0.0
                )
                if max_position is None or position_weight > max_position["weight_pct"]:
                    max_position = {
                        "account_id": position["account_id"],
                        "market": position["market"],
                        "symbol": position["symbol"],
                        "weight_pct": position_weight,
                    }

                instrument = instruments.get((position["market"], position["symbol"]))
                if instrument is None or instrument.verification_status != "verified":
                    scope_blockers.append({
                        "code": "risk_instrument_identity_unverified",
                        **blocker_base,
                    })
                    continue

                sector_exposures = self._risk_sector_exposures(instrument)
                if sector_exposures is None:
                    scope_blockers.append({
                        "code": "risk_sector_evidence_missing",
                        **blocker_base,
                    })
                else:
                    for exposure in sector_exposures:
                        sector_values[exposure["sector"]] += (
                            market_value * exposure["weight_pct"] / 100.0
                        )

                if (
                    instrument.instrument_type == "daily_leveraged_product"
                    or bool(instrument.daily_reset)
                ):
                    high_risk_value += market_value

            drawdown = self._currency_drawdown(
                currency=currency,
                account_ids=account_ids,
                daily_rows=daily_rows,
            )
            if not drawdown["available"]:
                scope_blockers.append({
                    "code": "risk_drawdown_history_insufficient",
                    "scope": "currency",
                    "currency": currency,
                    "complete_dates": drawdown["series_points"],
                })

            sector_exposures = [
                {
                    "sector": sector,
                    "weight_pct": value / total_equity * 100.0 if total_equity > 0 else 0.0,
                }
                for sector, value in sector_values.items()
            ]
            sector_exposures.sort(key=lambda item: (-item["weight_pct"], item["sector"]))
            max_sector = sector_exposures[0] if sector_exposures else None
            cash_buffer_pct = total_cash / total_equity * 100.0 if total_equity > 0 else 0.0
            max_single_position_pct = max_position["weight_pct"] if max_position else 0.0
            max_sector_pct = max_sector["weight_pct"] if max_sector else 0.0
            high_risk_product_pct = (
                high_risk_value / total_equity * 100.0 if total_equity > 0 else 0.0
            )
            max_drawdown_pct = drawdown["max_drawdown_pct"]

            scope_blockers = self._sorted_unique_records(scope_blockers)
            scope = {
                "currency": currency,
                "account_ids": sorted(account_ids),
                "snapshot_dates": sorted({row.snapshot_date.isoformat() for row in current_rows}),
                "total_cash": self._rounded(total_cash),
                "total_equity": self._rounded(total_equity),
                "cash_buffer_pct": self._rounded(cash_buffer_pct),
                "max_single_position_pct": self._rounded(max_single_position_pct),
                "max_single_position": self._rounded_record(max_position),
                "max_sector_pct": self._rounded(max_sector_pct),
                "max_sector": self._rounded_record(max_sector),
                "sector_exposures": [self._rounded_record(item) for item in sector_exposures],
                "high_risk_product_pct": self._rounded(high_risk_product_pct),
                "max_drawdown_pct": (
                    self._rounded(max_drawdown_pct)
                    if max_drawdown_pct is not None
                    else None
                ),
                "drawdown_series_points": drawdown["series_points"],
                "drawdown_rejected_fx_stale_rows": drawdown["rejected_fx_stale_rows"],
                "evaluated": not scope_blockers and risk_policy is not None,
            }
            scopes.append(scope)
            evidence_blockers.extend(scope_blockers)

            if scope["evaluated"] and thresholds is not None:
                breaches.extend(
                    self._scope_breaches(
                        scope=scope,
                        thresholds=thresholds,
                    )
                )

        evidence_blockers = self._sorted_unique_records(evidence_blockers)
        breaches = self._sorted_unique_records(breaches)
        evaluated = bool(scopes) and not evidence_blockers and risk_policy is not None
        return {
            "evaluated": evaluated,
            "base_scope": "currency",
            "thresholds": thresholds,
            "scopes": scopes,
            "breaches": breaches,
            "evidence_blockers": evidence_blockers,
            "reason": None if evaluated else "risk_evidence_incomplete",
        }

    @staticmethod
    def _risk_sector_exposures(
        instrument: PortfolioInstrument,
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            metadata = json.loads(instrument.metadata_json or "{}")
            if not isinstance(metadata, dict) or "risk_sector" not in metadata:
                return None
            normalized = PortfolioInstrumentService.normalize_risk_sector(
                metadata["risk_sector"]
            )
            return normalized["exposures"]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _currency_drawdown(
        *,
        currency: str,
        account_ids: set[int],
        daily_rows: List[Any],
    ) -> Dict[str, Any]:
        rows_by_date: Dict[Any, Dict[int, Any]] = defaultdict(dict)
        rejected_fx_stale_rows = 0
        for row in daily_rows:
            if row.account_id not in account_ids:
                continue
            if str(row.base_currency or "").strip().upper() != currency:
                continue
            if bool(row.fx_stale):
                rejected_fx_stale_rows += 1
                continue
            rows_by_date[row.snapshot_date][row.account_id] = row

        series = []
        for snapshot_date in sorted(rows_by_date):
            rows = rows_by_date[snapshot_date]
            if set(rows) != account_ids:
                continue
            series.append(
                (
                    snapshot_date.isoformat(),
                    sum(float(row.total_equity or 0.0) for row in rows.values()),
                )
            )
        if len(series) < 2:
            return {
                "available": False,
                "series_points": len(series),
                "max_drawdown_pct": None,
                "rejected_fx_stale_rows": rejected_fx_stale_rows,
            }

        peak = 0.0
        max_drawdown_pct = 0.0
        for _, equity in series:
            peak = max(peak, equity)
            drawdown_pct = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        return {
            "available": True,
            "series_points": len(series),
            "max_drawdown_pct": max_drawdown_pct,
            "rejected_fx_stale_rows": rejected_fx_stale_rows,
        }

    @classmethod
    def _scope_breaches(
        cls,
        *,
        scope: Dict[str, Any],
        thresholds: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        checks = (
            (
                "cash_buffer_below_minimum",
                "cash_buffer_pct",
                "min_cash_buffer_pct",
                lambda actual, limit: actual < limit,
            ),
            (
                "single_position_above_maximum",
                "max_single_position_pct",
                "max_single_position_pct",
                lambda actual, limit: actual > limit,
            ),
            (
                "sector_exposure_above_maximum",
                "max_sector_pct",
                "max_sector_pct",
                lambda actual, limit: actual > limit,
            ),
            (
                "high_risk_product_above_maximum",
                "high_risk_product_pct",
                "max_high_risk_product_pct",
                lambda actual, limit: actual > limit,
            ),
            (
                "portfolio_drawdown_above_maximum",
                "max_drawdown_pct",
                "max_portfolio_drawdown_pct",
                lambda actual, limit: actual > limit,
            ),
        )
        breaches = []
        for code, actual_field, limit_field, is_breach in checks:
            actual = scope[actual_field]
            limit = thresholds[limit_field]
            if actual is not None and is_breach(float(actual), float(limit)):
                breaches.append({
                    "code": code,
                    "scope": "currency",
                    "currency": scope["currency"],
                    "actual_pct": cls._rounded(actual),
                    "limit_pct": cls._rounded(limit),
                })
        return breaches

    @staticmethod
    def _rounded(value: Any) -> float:
        return round(float(value), 8)

    @classmethod
    def _rounded_record(
        cls,
        value: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        return {
            key: cls._rounded(item) if key == "weight_pct" else item
            for key, item in value.items()
        }

    @staticmethod
    def _sorted_unique_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        unique = {
            json.dumps(record, sort_keys=True, separators=(",", ":")): record
            for record in records
        }
        return [unique[key] for key in sorted(unique)]

    def _position_payload(
        self,
        row: PortfolioPosition,
        *,
        price_bar: Any,
        cutoff: datetime,
        blockers: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        market = str(row.market or "").strip().lower()
        symbol = str(row.symbol or "").strip().upper()
        currency = str(row.currency or "").strip().upper()
        valuation_currency = str(row.valuation_currency or "").strip().upper()
        updated_at = (
            self._legacy_local_to_utc_naive(row.updated_at)
            if row.updated_at
            else None
        )
        price_evidence = self._market_bar_evidence(price_bar, market=market)
        price_evidence_available = bool(
            price_evidence.get("price")
            and float(price_evidence["price"]) > 0
        )
        price_as_of = self._evidence_datetime(price_evidence.get("as_of"))
        price_evidence_not_final = bool(
            price_as_of is not None
            and self._daily_bar_not_final(
                market=market,
                as_of=price_as_of,
                cutoff=cutoff,
            )
        )
        price_evidence_stale = bool(
            price_as_of is None
            or price_evidence_not_final
            or (
                price_as_of is not None
                and daily_bar_stale(
                    market=market,
                    as_of=price_as_of,
                    cutoff=cutoff,
                )
            )
            or (cutoff >= price_as_of and cutoff - price_as_of > self.max_price_age)
        )
        price_available = bool(row.last_price and float(row.last_price) > 0)
        price_stale = bool(
            updated_at is None
            or (cutoff >= updated_at and cutoff - updated_at > self.max_price_age)
        )
        blocker_base = {
            "scope": "instrument",
            "account_id": row.account_id,
            "market": market,
            "symbol": symbol,
        }
        if not price_evidence_available:
            blockers.append({"code": "decision_price_missing", **blocker_base})
        elif price_evidence_not_final:
            blockers.append({"code": "decision_price_not_final", **blocker_base})
        elif price_evidence_stale:
            blockers.append({"code": "decision_price_stale", **blocker_base})

        fx_required = bool(currency and valuation_currency and currency != valuation_currency)
        fx_payload: Dict[str, Any] = {
            "required": fx_required,
            "available": not fx_required,
            "pair": f"{currency}/{valuation_currency}" if currency and valuation_currency else None,
            "rate": 1.0 if not fx_required and currency and valuation_currency else None,
            "as_of": self._iso_utc(cutoff) if not fx_required else None,
            "source": "identity" if not fx_required else None,
            "source_version": PORTFOLIO_FX_EVIDENCE_VERSION if not fx_required else None,
            "source_hash": None,
            "stale": False,
        }
        if not fx_required and currency and valuation_currency:
            fx_payload["source_hash"] = self._hash({
                "pair": fx_payload["pair"],
                "rate": fx_payload["rate"],
                "as_of": fx_payload["as_of"],
            })
        if fx_required:
            fx_row = self.repo.get_latest_fx_rate(
                from_currency=currency,
                to_currency=valuation_currency,
                as_of=cutoff.date(),
            )
            if fx_row is None:
                blockers.append({"code": "fx_rate_missing", **blocker_base})
            else:
                source_label = str(fx_row.source or "").strip()
                source, separator, source_version = source_label.partition("@")
                if not separator:
                    source = source_label
                    source_version = PORTFOLIO_FX_EVIDENCE_VERSION
                captured_at = (
                    self._iso_utc(self._legacy_local_to_utc_naive(fx_row.updated_at))
                    if fx_row.updated_at
                    else None
                )
                fx_body = {
                    "pair": f"{currency}/{valuation_currency}",
                    "rate": float(fx_row.rate),
                    "as_of": fx_row.rate_date.isoformat(),
                    "source": source,
                    "source_version": source_version,
                    "captured_at": captured_at,
                }
                rate_date_stale = bool(
                    fx_row.rate_date > cutoff.date()
                    or cutoff.date() - fx_row.rate_date > FX_MAX_AGE
                )
                fx_stale = bool(fx_row.is_stale or rate_date_stale)
                fx_payload = {
                    "required": True,
                    "available": True,
                    **fx_body,
                    "source_hash": self._hash(fx_body),
                    "stale": fx_stale,
                }
                if fx_stale:
                    blockers.append({"code": "fx_rate_stale", **blocker_base})

        return {
            "account_id": row.account_id,
            "symbol": symbol,
            "market": market,
            "currency": currency,
            "quantity": float(row.quantity),
            "last_price": price_evidence.get("price"),
            "market_value_base": float(row.market_value_base),
            "valuation_currency": valuation_currency,
            "cache_updated_at": updated_at.isoformat() if updated_at else None,
            "price_available": price_available,
            "price_stale": price_stale,
            "price_evidence_available": price_evidence_available,
            "price_evidence_not_final": price_evidence_not_final,
            "price_evidence_stale": price_evidence_stale,
            "price_source": price_evidence.get("source"),
            "price_source_version": price_evidence.get("source_version"),
            "price_as_of": price_evidence.get("as_of"),
            "price_captured_at": price_evidence.get("captured_at"),
            "price_source_hash": price_evidence.get("source_hash"),
            "price_evidence_batch_hash": price_evidence.get("batch_hash"),
            "adjustment_identity": price_evidence.get("adjustment_identity"),
            "fx": fx_payload,
        }

    @classmethod
    def _accounts_payload(cls, accounts: List[Any], latest_daily: Dict[int, Any]) -> List[Dict[str, Any]]:
        payload = []
        for account in sorted(accounts, key=lambda row: row.id):
            daily = latest_daily.get(account.id)
            body = {
                "account_id": account.id,
                "market": str(account.market or "").lower(),
                "base_currency": str(account.base_currency or "").upper(),
                "snapshot_date": daily.snapshot_date.isoformat() if daily else None,
                "total_cash": float(daily.total_cash) if daily else None,
                "total_market_value": float(daily.total_market_value) if daily else None,
                "total_equity": float(daily.total_equity) if daily else None,
                "fx_stale": bool(daily.fx_stale) if daily else None,
                "captured_at": (
                    cls._iso_utc(cls._legacy_local_to_utc_naive(daily.updated_at))
                    if daily and daily.updated_at
                    else None
                ),
            }
            payload.append({
                **body,
                "evidence_source": "portfolio_daily_snapshots",
                "evidence_version": PORTFOLIO_ACCOUNT_EVIDENCE_VERSION,
                "evidence_hash": cls._hash(body),
            })
        return payload

    @classmethod
    def _instrument_payload(
        cls,
        row: PortfolioInstrument,
        *,
        cutoff: datetime,
        adjustment_identity: Optional[str],
        prepared_product_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        by_account = {
            str(account_id): dict(evidence)
            for account_id, evidence in (prepared_product_evidence or {}).items()
        }
        registry_evidence = product_evidence_from_instrument(row, cutoff=cutoff)
        body = {
            "symbol": str(row.symbol).upper(),
            "market": str(row.market).lower(),
            "name": cls._instrument_name(row),
            "quote_currency": str(row.quote_currency).upper(),
            "instrument_type": row.instrument_type,
            "underlying_symbol": row.underlying_symbol,
            "underlying_market": row.underlying_market,
            "underlying_currency": row.underlying_currency,
            "leverage_factor": row.leverage_factor,
            "daily_reset": bool(row.daily_reset),
            "conversion_ratio": row.conversion_ratio,
            "trade_lot_size": float(row.trade_lot_size),
            "requires_premium_check": bool(row.requires_premium_check),
            "verification_status": row.verification_status,
            "evidence_source": row.evidence_source,
            "evidence_version": PORTFOLIO_INSTRUMENT_EVIDENCE_VERSION,
            "evidence_as_of": (
                row.evidence_as_of.replace(tzinfo=timezone.utc).isoformat()
                if row.evidence_as_of
                else None
            ),
            "captured_at": (
                cls._iso_utc(cls._utc_naive(row.updated_at))
                if row.updated_at
                else None
            ),
            "adjustment_identity": adjustment_identity,
            "product_evidence": (
                next(iter(by_account.values()))
                if len(by_account) == 1
                else registry_evidence
            ),
            "product_evidence_by_account": by_account,
        }
        return {**body, "evidence_hash": cls._hash(body)}

    @staticmethod
    def _prepared_product_evidence(
        *,
        items: Optional[Sequence[Any]],
        resolved_scope: Sequence[Tuple[int, str, str]],
        instruments: Mapping[Tuple[str, str], PortfolioInstrument],
        cutoff: datetime,
    ) -> Dict[Tuple[str, str], Dict[str, Dict[str, Any]]]:
        if not items:
            return {}
        scope_keys = set(resolved_scope)
        prepared: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = defaultdict(dict)
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("prepared product evidence item must be a mapping")
            try:
                account_id, market, symbol = position_scope_key(item)
            except (TypeError, ValueError) as exc:
                raise ValueError("prepared product evidence identity is invalid") from exc
            scope_key = (account_id, market, symbol)
            if scope_key not in scope_keys:
                raise ValueError("prepared product evidence is outside the research scope")
            instrument = instruments.get((market, symbol))
            if instrument is None:
                raise ValueError("prepared product evidence instrument is missing")
            evidence = item.get("product_evidence")
            if evidence is None:
                continue
            validated = validate_prepared_product_evidence(
                instrument,
                evidence,
                cutoff=cutoff,
            )
            if validated is None:
                raise ValueError("prepared product evidence failed immutable validation")
            prepared[(market, symbol)][str(account_id)] = validated
        return dict(prepared)

    @staticmethod
    def _instrument_name(row: PortfolioInstrument) -> Optional[str]:
        try:
            metadata = json.loads(row.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        name = str(metadata.get("name") or "").strip()
        return name or None

    @classmethod
    def _risk_policy_payload(cls, row: Optional[PortfolioRiskPolicy]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        body = {
            "min_cash_buffer_pct": float(row.min_cash_buffer_pct),
            "max_single_position_pct": float(row.max_single_position_pct),
            "max_sector_pct": float(row.max_sector_pct),
            "max_high_risk_product_pct": float(row.max_high_risk_product_pct),
            "max_portfolio_drawdown_pct": float(row.max_portfolio_drawdown_pct),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        return {
            **body,
            "evidence_source": "portfolio_risk_policy",
            "evidence_version": PORTFOLIO_RISK_POLICY_EVIDENCE_VERSION,
            "evidence_hash": cls._hash(body),
        }

    @classmethod
    def _market_bar_evidence(
        cls,
        row: Any,
        *,
        market: str,
    ) -> Dict[str, Any]:
        if row is None:
            return {}
        source = str(row.data_source or "").strip()
        batch_hash = getattr(row, "batch_hash", None)
        adjustment_identity = (
            str(getattr(row, "adjustment_identity", "") or "").strip()
            or StockRepository._adjustment_marker(source)
        )
        if adjustment_identity == "unknown":
            adjustment_identity = None
        source_version = (
            str(getattr(row, "source_version", "") or "").strip()
            or MARKET_BAR_EVIDENCE_VERSION
        )
        captured_value = getattr(row, "captured_at", None)
        if captured_value is not None:
            captured_at = cls._iso_utc(cls._utc_naive(captured_value))
        else:
            created_at = getattr(row, "created_at", None)
            captured_at = (
                cls._iso_utc(cls._legacy_local_to_utc_naive(created_at))
                if created_at
                else None
            )
        body = {
            "code": str(row.code or "").strip().upper(),
            "date": row.date.isoformat() if row.date else None,
            "price": float(row.close) if row.close is not None else None,
            "source": source,
            "source_version": source_version,
            "adjustment_identity": adjustment_identity,
            "captured_at": captured_at,
            "batch_hash": batch_hash,
        }
        bar_as_of = (
            resolve_market_daily_bar_as_of(market, row.date)
            if row.date is not None
            else None
        )
        return {
            "price": body["price"],
            "as_of": cls._iso_utc(cls._utc_naive(bar_as_of)) if bar_as_of else None,
            "source": source,
            "source_version": source_version,
            "adjustment_identity": adjustment_identity,
            "captured_at": captured_at,
            "source_hash": getattr(row, "bar_hash", None) or cls._hash(body),
            "batch_hash": batch_hash,
        }

    @staticmethod
    def _daily_bar_not_final(
        *,
        market: str,
        as_of: datetime,
        cutoff: datetime,
    ) -> bool:
        return daily_bar_not_final(market=market, as_of=as_of, cutoff=cutoff)

    @classmethod
    def _with_evidence_metadata(
        cls,
        body: Dict[str, Any],
        *,
        as_of_field: str,
        as_of: Optional[str],
        source: str,
        source_version: str,
    ) -> Dict[str, Any]:
        payload = {**body, as_of_field: as_of}
        return {
            **payload,
            "evidence_source": source,
            "evidence_version": source_version,
            "evidence_hash": cls._hash(
                {
                    **payload,
                    "evidence_source": source,
                    "evidence_version": source_version,
                }
            ),
        }

    @classmethod
    def _evidence_datetime(cls, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return cls._utc_naive(parsed)

    @staticmethod
    def _utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @classmethod
    def _execution_identity_hash(cls, snapshot: Dict[str, Any]) -> str:
        account_fields = ("account_id", "market", "base_currency", "total_cash")
        position_fields = ("account_id", "market", "symbol", "currency", "quantity")
        instrument_fields = (
            "market",
            "symbol",
            "name",
            "quote_currency",
            "instrument_type",
            "underlying_symbol",
            "underlying_market",
            "underlying_currency",
            "leverage_factor",
            "daily_reset",
            "conversion_ratio",
            "trade_lot_size",
            "requires_premium_check",
            "verification_status",
        )
        identity = {
            "schema_version": "portfolio-research-execution-identity-v1",
            "cost_method": snapshot.get("cost_method"),
            "analysis_runtime": snapshot.get("analysis_runtime"),
            "scope": snapshot.get("scope") or [],
            "accounts": [
                {key: item.get(key) for key in account_fields}
                for item in snapshot.get("accounts") or []
                if isinstance(item, dict)
            ],
            "positions": [
                {key: item.get(key) for key in position_fields}
                for item in snapshot.get("positions") or []
                if isinstance(item, dict)
            ],
            "instruments": [
                {key: item.get(key) for key in instrument_fields}
                for item in snapshot.get("instruments") or []
                if isinstance(item, dict)
            ],
            "risk_policy": snapshot.get("risk_policy"),
        }
        return cls._hash(identity)

    @staticmethod
    def _hash(value: Dict[str, Any]) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
