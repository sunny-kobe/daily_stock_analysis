# -*- coding: utf-8 -*-
"""Frozen, hashed, read-only DSA portfolio research snapshot."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, or_, select

from src.config import get_config
from src.core.market_profile import get_profile
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_instrument_service import PortfolioInstrumentService
from src.storage import (
    DecisionSignalRecord,
    PortfolioInstrument,
    PortfolioPosition,
    PortfolioRiskPolicy,
)
from src.utils.sanitize import sanitize_decision_signal_payload


RESEARCH_SNAPSHOT_SCHEMA_VERSION = "portfolio-research-snapshot-v1"
MARKET_PROFILE_EVIDENCE_VERSION = "market-profile-v1"
SUPPORTED_BENCHMARK_MARKETS = frozenset({"cn", "hk", "us", "jp", "kr"})
FROZEN_RESEARCH_SNAPSHOT_CONTEXT_KEY = "_frozen_research_snapshot"
POINT_IN_TIME_SCOPE = "current_prospective"


class PortfolioResearchSnapshotService:
    """Build the minimum deterministic DSA truth package used by research gates."""

    def __init__(
        self,
        repo: Optional[PortfolioRepository] = None,
        *,
        max_price_age_hours: float = 72.0,
        max_decision_signals: int = 100,
    ):
        self.repo = repo or PortfolioRepository()
        self.max_price_age = timedelta(hours=max_price_age_hours)
        self.max_decision_signals = max(0, int(max_decision_signals))

    def build(self, *, cutoff: Optional[datetime] = None) -> Dict[str, Any]:
        cutoff_value = self._utc_naive(cutoff or datetime.now(timezone.utc))
        accounts = self.repo.list_accounts(include_inactive=False)
        positions = self.repo.list_cached_positions(cost_method="fifo")
        instruments = {
            (str(row.market).lower(), str(row.symbol).upper()): row
            for row in self.repo.list_instruments()
        }
        risk_policy = self.repo.get_risk_policy()
        daily_rows = self.repo.list_daily_snapshots_for_risk(
            as_of=cutoff_value.date(),
            cost_method="fifo",
            lookback_days=0,
        )
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
        instrument_payload = [
            self._instrument_payload(row)
            for row in instruments.values()
        ]
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
            position_payload.append(
                self._position_payload(
                    position,
                    cutoff=cutoff_value,
                    blockers=blockers,
                )
            )

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
        risk_budget = self._evaluate_risk_budget(
            accounts=accounts,
            positions=position_payload,
            instruments=instruments,
            latest_daily=latest_daily,
            daily_rows=daily_rows,
            risk_policy=risk_policy,
        )
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
        payload = {
            "schema_version": RESEARCH_SNAPSHOT_SCHEMA_VERSION,
            "cutoff": cutoff_value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "timezone": "UTC",
            "cost_method": "fifo",
            "analysis_runtime": self._analysis_runtime_payload(),
            "universe_hash": universe_hash,
            "accounts": account_payload,
            "positions": position_payload,
            "instruments": instrument_payload,
            "benchmarks": self._benchmark_payload(position_payload),
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
        payload["snapshot_hash"] = self._hash(payload)
        return payload

    def _load_decision_signals(
        self,
        *,
        positions: List[Dict[str, Any]],
        cutoff: datetime,
    ) -> Tuple[List[Dict[str, Any]], List[DecisionSignalRecord], bool]:
        identities = sorted(
            {
                (str(item.get("market") or "").lower(), str(item.get("symbol") or "").upper())
                for item in positions
                if float(item.get("quantity") or 0.0) > 0
            }
        )
        if not identities:
            return [], [], False

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
                            DecisionSignalRecord.expires_at.is_(None),
                            DecisionSignalRecord.expires_at > cutoff,
                        ),
                    )
                    .order_by(
                        desc(DecisionSignalRecord.created_at),
                        desc(DecisionSignalRecord.id),
                    )
                    .limit(self.max_decision_signals + 1)
                ).scalars()
            )
            for row in rows:
                session.expunge(row)

        truncated = len(rows) > self.max_decision_signals
        captured_rows = rows[: self.max_decision_signals]
        payload = [self._decision_signal_payload(row) for row in captured_rows]
        payload.sort(
            key=lambda item: (
                item["market"],
                item["stock_code"],
                item["created_at"] or "",
                item["id"],
            )
        )
        return payload, captured_rows, truncated

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

    @classmethod
    def _decision_signal_payload(cls, row: DecisionSignalRecord) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        if row.metadata_json:
            try:
                raw_metadata = json.loads(row.metadata_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_metadata = {}
            if isinstance(raw_metadata, dict):
                metadata = {
                    key: raw_metadata[key]
                    for key in (
                        "quality_context_status",
                        "quality_context_unable_reasons",
                        "portfolio_decision",
                    )
                    if key in raw_metadata
                }
        return {
            "id": row.id,
            "market": str(row.market or "").lower(),
            "stock_code": str(row.stock_code or "").upper(),
            "stock_name": row.stock_name,
            "reason": row.reason,
            "status": row.status,
            "created_at": cls._iso_utc(cls._utc_naive(row.created_at)) if row.created_at else None,
            "updated_at": cls._iso_utc(cls._utc_naive(row.updated_at)) if row.updated_at else None,
            "metadata": sanitize_decision_signal_payload(metadata),
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

    @staticmethod
    def _benchmark_payload(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        markets = sorted(
            {
                str(item.get("market") or "").strip().lower()
                for item in positions
            }
            & SUPPORTED_BENCHMARK_MARKETS
        )
        return [
            {
                "market": market,
                "code": get_profile(market).mood_index_code,
                "type": "market_index",
                "evidence_source": "dsa_market_profile",
                "evidence_version": MARKET_PROFILE_EVIDENCE_VERSION,
            }
            for market in markets
        ]

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
        cutoff: datetime,
        blockers: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        market = str(row.market or "").strip().lower()
        symbol = str(row.symbol or "").strip().upper()
        currency = str(row.currency or "").strip().upper()
        valuation_currency = str(row.valuation_currency or "").strip().upper()
        updated_at = self._utc_naive(row.updated_at) if row.updated_at else None
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
        if not price_available:
            blockers.append({"code": "decision_price_missing", **blocker_base})
        elif price_stale:
            blockers.append({"code": "decision_price_stale", **blocker_base})

        fx_required = bool(currency and valuation_currency and currency != valuation_currency)
        fx_payload: Dict[str, Any] = {
            "required": fx_required,
            "available": not fx_required,
            "rate_date": None,
            "stale": False,
        }
        if fx_required:
            fx_row = self.repo.get_latest_fx_rate(
                from_currency=currency,
                to_currency=valuation_currency,
                as_of=cutoff.date(),
            )
            if fx_row is None:
                blockers.append({"code": "fx_rate_missing", **blocker_base})
            else:
                fx_payload = {
                    "required": True,
                    "available": True,
                    "rate_date": fx_row.rate_date.isoformat(),
                    "stale": bool(fx_row.is_stale),
                }
                if fx_row.is_stale:
                    blockers.append({"code": "fx_rate_stale", **blocker_base})

        return {
            "account_id": row.account_id,
            "symbol": symbol,
            "market": market,
            "currency": currency,
            "quantity": float(row.quantity),
            "last_price": float(row.last_price),
            "market_value_base": float(row.market_value_base),
            "valuation_currency": valuation_currency,
            "cache_updated_at": updated_at.isoformat() if updated_at else None,
            "price_available": price_available,
            "price_stale": price_stale,
            "fx": fx_payload,
        }

    @staticmethod
    def _accounts_payload(accounts: List[Any], latest_daily: Dict[int, Any]) -> List[Dict[str, Any]]:
        payload = []
        for account in sorted(accounts, key=lambda row: row.id):
            daily = latest_daily.get(account.id)
            payload.append({
                "account_id": account.id,
                "market": str(account.market or "").lower(),
                "base_currency": str(account.base_currency or "").upper(),
                "snapshot_date": daily.snapshot_date.isoformat() if daily else None,
                "total_cash": float(daily.total_cash) if daily else None,
                "total_market_value": float(daily.total_market_value) if daily else None,
                "total_equity": float(daily.total_equity) if daily else None,
                "fx_stale": bool(daily.fx_stale) if daily else None,
            })
        return payload

    @staticmethod
    def _instrument_payload(row: PortfolioInstrument) -> Dict[str, Any]:
        return {
            "symbol": str(row.symbol).upper(),
            "market": str(row.market).lower(),
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
            "evidence_as_of": (
                row.evidence_as_of.replace(tzinfo=timezone.utc).isoformat()
                if row.evidence_as_of
                else None
            ),
        }

    @staticmethod
    def _risk_policy_payload(row: Optional[PortfolioRiskPolicy]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "min_cash_buffer_pct": float(row.min_cash_buffer_pct),
            "max_single_position_pct": float(row.max_single_position_pct),
            "max_sector_pct": float(row.max_sector_pct),
            "max_high_risk_product_pct": float(row.max_high_risk_product_pct),
            "max_portfolio_drawdown_pct": float(row.max_portfolio_drawdown_pct),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

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
