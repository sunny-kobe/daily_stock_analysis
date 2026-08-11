# -*- coding: utf-8 -*-
"""Build and persist immutable evidence captured at portfolio decision time."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from src.repositories.decision_evidence_snapshot_repo import (
    DecisionEvidenceSnapshotRepository,
)
from src.schemas.decision_evidence_snapshot import (
    DecisionEvidenceSnapshot,
    canonical_json_hash,
)
from src.schemas.portfolio_decision_quality import QUALITY_HORIZONS
from src.schemas.strategy_validation import StrategyVersionManifest
from src.services.strategy_registry_service import (
    DEFAULT_PORTFOLIO_STRATEGY_MANIFEST_PATH,
    StrategyRegistryService,
    canonical_json,
    load_strategy_manifest,
)
from src.services.portfolio_research_market_evidence import daily_bar_not_final
from src.storage import DatabaseManager


DEFAULT_STRATEGY_MANIFEST = DEFAULT_PORTFOLIO_STRATEGY_MANIFEST_PATH
SOURCE_ENVELOPE_VERSION = "decision-source-envelope-v1"
MAX_MARKET_EVIDENCE_AGE = timedelta(hours=72)
MAX_FX_EVIDENCE_AGE = timedelta(days=7)


class DecisionEvidenceSnapshotService:
    """Freeze strategy identity, structured inputs and source evidence."""

    def __init__(
        self,
        *,
        db_manager: DatabaseManager | None = None,
        repo: DecisionEvidenceSnapshotRepository | None = None,
        strategy_registry: StrategyRegistryService | None = None,
        strategy_manifest_path: Path | None = None,
    ):
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = repo or DecisionEvidenceSnapshotRepository(self.db)
        self.strategy_registry = strategy_registry or StrategyRegistryService(self.db)
        self.strategy_manifest_path = strategy_manifest_path or DEFAULT_STRATEGY_MANIFEST

    def freeze(
        self,
        *,
        signal: Mapping[str, Any],
        portfolio_decision: Mapping[str, Any],
        research_snapshot: Mapping[str, Any],
        portfolio_context: Mapping[str, Any] | None = None,
        context_snapshot: Mapping[str, Any] | None = None,
        quality_context_id: int | None = None,
        _persist: bool = True,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        manifest = self._load_manifest()
        strategy = self._resolve_strategy(manifest, blockers)
        cutoff = self._aware_datetime(
            research_snapshot.get("cutoff") or signal.get("created_at")
        )
        if cutoff is None:
            raise ValueError("decision_cutoff_invalid")
        research_snapshot_hash = self._validate_research_snapshot_hash(
            research_snapshot,
            blockers,
        )

        market = str(signal.get("market") or "").strip().lower()
        symbol = str(signal.get("stock_code") or "").strip().upper()
        account_id = self._account_id(portfolio_decision, portfolio_context)
        account = self._find(
            research_snapshot.get("accounts"),
            lambda item: item.get("account_id") == account_id,
        )
        position = self._find(
            research_snapshot.get("positions"),
            lambda item: (
                item.get("account_id") == account_id
                and str(item.get("market") or "").lower() == market
                and str(item.get("symbol") or "").upper() == symbol
            ),
        )
        instrument = self._find(
            research_snapshot.get("instruments"),
            lambda item: (
                str(item.get("market") or "").lower() == market
                and str(item.get("symbol") or "").upper() == symbol
            ),
        )
        expected_benchmark = str(
            manifest.get("benchmark_policy", {}).get("benchmarks", {}).get(market) or ""
        ).strip()
        benchmark = self._find(
            research_snapshot.get("benchmarks"),
            lambda item: str(item.get("market") or "").lower() == market,
        )

        if not market or not symbol:
            blockers.append("decision_identity_missing")
        if account_id is None:
            blockers.append("account_identity_missing")
        if account is None:
            blockers.append("account_evidence_missing")
        if position is None:
            blockers.append("position_evidence_missing")
        if instrument is None:
            blockers.append("instrument_evidence_missing")
        instrument_type = str((instrument or {}).get("instrument_type") or "").strip()
        if market not in manifest.get("markets", []):
            blockers.append("strategy_market_out_of_scope")
        if instrument_type not in manifest.get("instrument_types", []):
            blockers.append("strategy_instrument_out_of_scope")

        reporting_currency = str((account or {}).get("base_currency") or "").strip().upper()
        if not reporting_currency:
            reporting_currency = "UNKNOWN"
            blockers.append("reporting_currency_missing")

        self._validate_account(account, blockers)
        self._validate_position(position, cutoff=cutoff, blockers=blockers)
        self._validate_instrument(instrument, blockers)
        product_evidence = self._product_evidence(
            instrument=instrument,
            portfolio_context=portfolio_context,
            context_snapshot=context_snapshot,
        )
        self._validate_product_evidence(
            instrument,
            product_evidence=product_evidence,
            blockers=blockers,
        )
        self._validate_benchmark(
            benchmark,
            expected_code=expected_benchmark,
            cutoff=cutoff,
            blockers=blockers,
        )
        self._validate_adjustment_compatibility(position, benchmark, blockers)
        self._validate_fx(
            position,
            reporting_currency=reporting_currency,
            cutoff=cutoff,
            blockers=blockers,
        )
        self._validate_risk(research_snapshot, blockers)
        self._validate_decision(portfolio_decision, blockers)
        self._validate_signal_readiness(signal, blockers)
        if not isinstance(context_snapshot, Mapping) or not context_snapshot:
            blockers.append("research_context_missing")

        evidence_bundle = {
            "account": self._envelope(
                account,
                cutoff=cutoff,
                label="account",
                as_of_field="snapshot_date",
                source_field="evidence_source",
                version_field="evidence_version",
                hash_field="evidence_hash",
                blockers=blockers,
            ),
            "position": self._envelope(
                position,
                cutoff=cutoff,
                label="position",
                as_of_field="price_as_of",
                source_field="price_source",
                version_field="price_source_version",
                hash_field="price_source_hash",
                blockers=blockers,
                market=market,
                excluded_body_fields={"fx"},
                requires_finalized_bar=True,
            ),
            "instrument": self._envelope(
                instrument,
                cutoff=cutoff,
                label="instrument",
                as_of_field="evidence_as_of",
                source_field="evidence_source",
                version_field="evidence_version",
                hash_field="evidence_hash",
                blockers=blockers,
            ),
            "benchmark": self._envelope(
                benchmark,
                cutoff=cutoff,
                label="benchmark",
                as_of_field="evidence_as_of",
                source_field="evidence_source",
                version_field="evidence_version",
                hash_field="evidence_hash",
                blockers=blockers,
                market=market,
                requires_finalized_bar=True,
            ),
            "fx": self._envelope(
                (position or {}).get("fx"),
                cutoff=cutoff,
                label="fx",
                as_of_field="as_of",
                source_field="source",
                version_field="source_version",
                hash_field="source_hash",
                blockers=blockers,
            ),
            "risk_policy": self._envelope(
                research_snapshot.get("risk_policy"),
                cutoff=cutoff,
                label="risk_policy",
                as_of_field="updated_at",
                source_field="evidence_source",
                version_field="evidence_version",
                hash_field="evidence_hash",
                blockers=blockers,
            ),
            "risk_budget": self._envelope(
                research_snapshot.get("risk_budget"),
                cutoff=cutoff,
                label="risk_budget",
                as_of_field="as_of",
                source_field="evidence_source",
                version_field="evidence_version",
                hash_field="evidence_hash",
                blockers=blockers,
            ),
            "cost_model": self._fixed_envelope(
                body=manifest.get("cost_model") or {},
                as_of=cutoff,
                source="strategy_registry",
                source_version=strategy["version"],
                source_hash=strategy["manifest_hash"],
            ),
            "decision_rationale": self._fixed_envelope(
                body={
                    key: portfolio_decision.get(key)
                    for key in (
                        "position_action",
                        "incremental_action",
                        "confidence_by_horizon",
                        "supporting_evidence",
                        "opposing_evidence",
                        "watch_conditions",
                        "invalidation",
                        "next_review",
                    )
                },
                as_of=cutoff,
                source="decision_signal",
                source_version="portfolio-decision-v1",
            ),
            "research_context": self._research_context_envelopes(
                context_snapshot,
                cutoff=cutoff,
                blockers=blockers,
            ),
        }
        structured_inputs = self._structured_inputs(
            account_id=account_id,
            market=market,
            symbol=symbol,
            account=account,
            position=position,
            instrument=instrument,
            benchmark=benchmark,
            strategy=strategy,
            research_snapshot_hash=research_snapshot_hash,
            evidence_bundle=evidence_bundle,
        )
        blockers = sorted(set(blockers))
        snapshot = DecisionEvidenceSnapshot.model_validate(
            {
                "schema_version": "decision-evidence-snapshot-v1",
                "signal_id": int(signal["id"]),
                "quality_context_id": quality_context_id,
                "strategy_key": strategy["strategy_key"],
                "strategy_version": strategy["version"],
                "strategy_manifest_hash": strategy["manifest_hash"],
                "decision_cutoff": cutoff,
                "reporting_currency": reporting_currency,
                "structured_inputs": structured_inputs,
                "evidence_bundle": evidence_bundle,
                "readiness_status": "insufficient_evidence" if blockers else "complete",
                "blockers": blockers,
                "snapshot_hash": research_snapshot_hash,
            }
        )
        if not _persist:
            return {
                "id": None,
                "signal_id": None,
                "status": snapshot.readiness_status,
                "display_status": (
                    "已保存"
                    if snapshot.readiness_status == "complete"
                    else "资料不足"
                ),
                "strategy_key": snapshot.strategy_key,
                "strategy_version": snapshot.strategy_version,
                "strategy_name": strategy["name"],
                "decision_input_hash": snapshot.decision_input_hash,
                "evidence_hash": snapshot.evidence_bundle_hash,
                "snapshot_hash": snapshot.snapshot_hash,
                "unable_reasons": snapshot.blockers,
                "created_at": None,
                "created": False,
            }
        row, created = self.repo.create_if_absent(snapshot.to_record_fields())
        return {
            "id": row.id,
            "signal_id": row.signal_id,
            "status": row.readiness_status,
            "display_status": "已保存" if row.readiness_status == "complete" else "资料不足",
            "strategy_key": row.strategy_key,
            "strategy_version": row.strategy_version,
            "strategy_name": strategy["name"],
            "decision_input_hash": row.decision_input_hash,
            "evidence_hash": row.evidence_bundle_hash,
            "snapshot_hash": row.snapshot_hash,
            "unable_reasons": json.loads(row.blockers_json),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "created": created,
        }

    def assess(
        self,
        *,
        signal: Mapping[str, Any],
        portfolio_decision: Mapping[str, Any],
        research_snapshot: Mapping[str, Any],
        portfolio_context: Mapping[str, Any] | None = None,
        context_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate a deterministic evidence draft without writing a sidecar."""

        draft_signal = dict(signal)
        draft_signal["id"] = 1
        return self.freeze(
            signal=draft_signal,
            portfolio_decision=portfolio_decision,
            research_snapshot=research_snapshot,
            portfolio_context=portfolio_context,
            context_snapshot=context_snapshot,
            quality_context_id=None,
            _persist=False,
        )

    def find_equivalent_signal(
        self,
        *,
        snapshot_hash: str,
        account_id: int,
        market: str,
        symbol: str,
        decision_profile: str,
    ):
        blockers: list[str] = []
        strategy = self._resolve_strategy(self._load_manifest(), blockers)
        if blockers:
            return None
        return self.repo.find_first_equivalent_signal(
            snapshot_hash=snapshot_hash,
            account_id=account_id,
            market=market,
            symbol=symbol,
            decision_profile=decision_profile,
            strategy_key=strategy["strategy_key"],
            strategy_version=strategy["version"],
        )

    def get_summary(self, *, signal_id: int) -> dict[str, Any]:
        row = self.repo.get_by_signal_id(signal_id=signal_id)
        if row is None:
            return {
                "signal_id": signal_id,
                "status": "missing",
                "display_status": "资料不足",
                "strategy_key": None,
                "strategy_version": None,
                "strategy_name": None,
                "unable_reasons": ["legacy_evidence_snapshot_missing"],
                "created_at": None,
            }
        try:
            strategy_name = self.strategy_registry.get_version(
                row.strategy_key, row.strategy_version
            )["name"]
        except ValueError:
            strategy_name = None
        try:
            structured_inputs = json.loads(row.structured_inputs_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            structured_inputs = {}
        if not isinstance(structured_inputs, Mapping):
            structured_inputs = {}
        return {
            "signal_id": row.signal_id,
            "status": row.readiness_status,
            "display_status": "已保存" if row.readiness_status == "complete" else "资料不足",
            "strategy_key": row.strategy_key,
            "strategy_version": row.strategy_version,
            "strategy_name": strategy_name,
            "unable_reasons": json.loads(row.blockers_json),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "identity": dict(structured_inputs.get("identity") or {}),
            "instrument": dict(structured_inputs.get("instrument") or {}),
            "benchmark": dict(structured_inputs.get("benchmark") or {}),
            "research_snapshot_hash": structured_inputs.get("research_snapshot_hash"),
        }

    def _load_manifest(self) -> dict[str, Any]:
        return load_strategy_manifest(self.strategy_manifest_path)

    def _resolve_strategy(
        self,
        manifest: Mapping[str, Any],
        blockers: list[str],
    ) -> dict[str, Any]:
        validated = StrategyVersionManifest.model_validate(manifest)
        payload = validated.model_dump(mode="json")
        manifest_hash = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        try:
            registered = self.strategy_registry.get_version(
                validated.strategy_key,
                validated.version,
            )
        except ValueError as exc:
            if str(exc) != "strategy_version_not_found":
                raise
            blockers.append("strategy_version_missing")
            registered = None
        if registered is not None and registered["manifest_hash"] != manifest_hash:
            blockers.append("strategy_manifest_hash_mismatch")
        return {
            **payload,
            "manifest_hash": manifest_hash,
        }

    @classmethod
    def _envelope(
        cls,
        value: Any,
        *,
        cutoff: datetime,
        label: str,
        as_of_field: str,
        source_field: str,
        version_field: str,
        hash_field: str,
        blockers: list[str],
        market: str | None = None,
        excluded_body_fields: set[str] | None = None,
        requires_finalized_bar: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {
                "schema_version": SOURCE_ENVELOPE_VERSION,
                "status": "missing",
                "body": {},
            }
        payload = dict(value)
        parsed = cls._evidence_datetime(payload.get(as_of_field))
        source = str(payload.get(source_field) or "").strip()
        source_version = str(payload.get(version_field) or "").strip()
        source_hash = str(payload.get(hash_field) or "").strip().lower()
        invalid = False
        if parsed is None:
            blockers.append(f"{label}_evidence_as_of_missing")
            invalid = True
        elif parsed > cutoff:
            blockers.append(f"{label}_evidence_after_cutoff")
            invalid = True
        if (
            requires_finalized_bar
            and parsed is not None
            and daily_bar_not_final(
                market=str(market or ""),
                as_of=parsed,
                cutoff=cutoff,
            )
        ):
            blockers.append(f"{label}_evidence_not_final")
            invalid = True
        if not source or not source_version or not cls._is_sha256(source_hash):
            blockers.append(f"{label}_evidence_source_incomplete")
            invalid = True
        metadata_fields = {
            as_of_field,
            source_field,
            version_field,
            hash_field,
            *(excluded_body_fields or set()),
        }
        body = {
            key: item
            for key, item in payload.items()
            if key not in metadata_fields
        }
        if cls._body_has_future_time(body, cutoff):
            blockers.append(f"{label}_evidence_after_cutoff")
            invalid = True
        if invalid:
            return {
                "schema_version": SOURCE_ENVELOPE_VERSION,
                "status": "insufficient_evidence",
                "body": {},
            }
        return cls._fixed_envelope(
            body=body,
            as_of=parsed,
            source=source,
            source_version=source_version,
            source_hash=source_hash,
        )

    @staticmethod
    def _fixed_envelope(
        *,
        body: Mapping[str, Any],
        as_of: datetime,
        source: str,
        source_version: str,
        source_hash: str | None = None,
    ) -> dict[str, Any]:
        normalized_body = dict(body)
        return {
            "schema_version": SOURCE_ENVELOPE_VERSION,
            "as_of": as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": source,
            "source_version": source_version,
            "source_hash": source_hash or canonical_json_hash(normalized_body),
            "body": normalized_body,
        }

    @classmethod
    def _research_context_envelopes(
        cls,
        context_snapshot: Mapping[str, Any] | None,
        *,
        cutoff: datetime,
        blockers: list[str],
    ) -> list[dict[str, Any]]:
        raw_items = (
            context_snapshot.get("decision_evidence")
            if isinstance(context_snapshot, Mapping)
            else None
        )
        if not isinstance(raw_items, list) or not raw_items:
            blockers.append("research_context_missing")
            return []
        accepted = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                blockers.append("research_context_envelope_invalid")
                continue
            body = item.get("body")
            as_of = cls._evidence_datetime(item.get("as_of"))
            source_hash = str(item.get("source_hash") or "").strip().lower()
            if (
                item.get("schema_version") != SOURCE_ENVELOPE_VERSION
                or as_of is None
                or not str(item.get("source") or "").strip()
                or not str(item.get("source_version") or "").strip()
                or not cls._is_sha256(source_hash)
                or not isinstance(body, Mapping)
            ):
                blockers.append("research_context_envelope_invalid")
                continue
            if as_of > cutoff or cls._body_has_future_time(body, cutoff):
                blockers.append("research_context_after_cutoff")
                continue
            accepted.append(
                cls._fixed_envelope(
                    body=body,
                    as_of=as_of,
                    source=str(item["source"]).strip(),
                    source_version=str(item["source_version"]).strip(),
                    source_hash=source_hash,
                )
            )
        if not accepted and "research_context_after_cutoff" not in blockers:
            blockers.append("research_context_missing")
        return accepted

    @classmethod
    def _body_has_future_time(
        cls,
        value: Any,
        cutoff: datetime,
    ) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized_key = str(key).lower()
                if (
                    normalized_key in {"date", "timestamp", "as_of"}
                    or normalized_key.endswith(("_at", "_date", "_time"))
                ):
                    parsed = cls._evidence_datetime(nested)
                    if parsed is None or parsed > cutoff:
                        return True
                if cls._body_has_future_time(nested, cutoff):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(cls._body_has_future_time(item, cutoff) for item in value)
        return False

    @staticmethod
    def _structured_inputs(
        *,
        account_id: int | None,
        market: str,
        symbol: str,
        account: Mapping[str, Any] | None,
        position: Mapping[str, Any] | None,
        instrument: Mapping[str, Any] | None,
        benchmark: Mapping[str, Any] | None,
        strategy: Mapping[str, Any],
        research_snapshot_hash: Any,
        evidence_bundle: Mapping[str, Any],
    ) -> dict[str, Any]:
        account_body = evidence_bundle["account"].get("body", {})
        position_body = evidence_bundle["position"].get("body", {})
        instrument_body = evidence_bundle["instrument"].get("body", {})
        benchmark_body = evidence_bundle["benchmark"].get("body", {})
        fx_body = evidence_bundle["fx"].get("body", {})

        def evidence_hash(key: str) -> str:
            return canonical_json_hash(evidence_bundle[key])

        return {
            "identity": {
                "account_id": account_id,
                "market": market,
                "symbol": symbol,
            },
            "account": {
                "reporting_currency": account_body.get("base_currency"),
                "total_cash": account_body.get("total_cash"),
                "total_equity": account_body.get("total_equity"),
                "evidence_hash": evidence_hash("account"),
            },
            "position": {
                "quantity": position_body.get("quantity"),
                "price": position_body.get("last_price"),
                "currency": position_body.get("currency"),
                "evidence_hash": evidence_hash("position"),
            },
            "instrument": {
                "name": instrument_body.get("name"),
                "instrument_type": instrument_body.get("instrument_type"),
                "trade_lot_size": instrument_body.get("trade_lot_size"),
                "adjustment_identity": instrument_body.get("adjustment_identity"),
                "product_evidence_hash": (
                    instrument_body.get("product_evidence") or {}
                ).get("evidence_hash"),
                "evidence_hash": evidence_hash("instrument"),
            },
            "benchmark": {
                "market": benchmark_body.get("market"),
                "code": benchmark_body.get("code"),
                "type": benchmark_body.get("type"),
                "price": benchmark_body.get("price"),
                "adjustment_identity": benchmark_body.get("adjustment_identity"),
                "evidence_hash": evidence_hash("benchmark"),
            },
            "fx": {
                "pair": fx_body.get("pair"),
                "rate": fx_body.get("rate"),
                "evidence_hash": evidence_hash("fx"),
            },
            "risk": {
                "policy": evidence_bundle["risk_policy"].get("body", {}),
                "budget": evidence_bundle["risk_budget"].get("body", {}),
                "policy_evidence_hash": evidence_hash("risk_policy"),
                "budget_evidence_hash": evidence_hash("risk_budget"),
            },
            "cost_model": {
                **evidence_bundle["cost_model"].get("body", {}),
                "evidence_hash": evidence_hash("cost_model"),
            },
            "research_context_hashes": [
                canonical_json_hash(item)
                for item in evidence_bundle["research_context"]
            ],
            "research_snapshot_hash": research_snapshot_hash,
            "strategy_key": strategy["strategy_key"],
            "strategy_version": strategy["version"],
            "strategy_manifest_hash": strategy["manifest_hash"],
        }

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    @classmethod
    def _validate_research_snapshot_hash(
        cls,
        snapshot: Mapping[str, Any],
        blockers: list[str],
    ) -> str:
        provided = str(snapshot.get("snapshot_hash") or "").strip().lower()
        body = dict(snapshot)
        body.pop("snapshot_hash", None)
        try:
            encoded = json.dumps(
                body,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            blockers.append("research_snapshot_hash_invalid")
            return hashlib.sha256(b"invalid-research-snapshot").hexdigest()
        expected = hashlib.sha256(encoded).hexdigest()
        if not cls._is_sha256(provided):
            blockers.append("research_snapshot_hash_invalid")
        elif provided != expected:
            blockers.append("research_snapshot_hash_mismatch")
        return expected

    @classmethod
    def _evidence_datetime(cls, value: Any) -> datetime | None:
        parsed = cls._aware_datetime(value)
        if parsed is not None:
            return parsed
        if value is None:
            return None
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _find(items: Any, predicate) -> dict[str, Any] | None:
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, Mapping) and predicate(item):
                return dict(item)
        return None

    @staticmethod
    def _account_id(
        decision: Mapping[str, Any], context: Mapping[str, Any] | None
    ) -> int | None:
        value = (context or {}).get("account_id", decision.get("account_id"))
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _aware_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _validate_account(value: Mapping[str, Any] | None, blockers: list[str]) -> None:
        if value is None:
            return
        if value.get("total_cash") is None or value.get("total_equity") is None:
            blockers.append("account_balance_evidence_missing")

    @classmethod
    def _validate_position(
        cls,
        value: Mapping[str, Any] | None,
        *,
        cutoff: datetime,
        blockers: list[str],
    ) -> None:
        if value is None:
            return
        if not value.get("price_evidence_available", value.get("price_available")) or value.get(
            "price_evidence_stale",
            value.get("price_stale"),
        ):
            blockers.append("decision_price_invalid")
        if not all(value.get(field) for field in ("price_source", "price_as_of", "price_source_hash")):
            blockers.append("decision_price_evidence_missing")
        price_as_of = cls._evidence_datetime(value.get("price_as_of"))
        if price_as_of is not None:
            if daily_bar_not_final(
                market=str(value.get("market") or ""),
                as_of=price_as_of,
                cutoff=cutoff,
            ):
                blockers.append("position_evidence_not_final")
            elif cutoff - price_as_of > MAX_MARKET_EVIDENCE_AGE:
                blockers.append("decision_price_stale")

    @staticmethod
    def _validate_instrument(value: Mapping[str, Any] | None, blockers: list[str]) -> None:
        if value is None:
            return
        if value.get("verification_status") != "verified":
            blockers.append("instrument_identity_unverified")
        if not all(
            value.get(field)
            for field in (
                "instrument_type",
                "quote_currency",
                "trade_lot_size",
                "evidence_source",
                "evidence_as_of",
                "evidence_hash",
                "adjustment_identity",
            )
        ):
            blockers.append("instrument_evidence_incomplete")
        if value.get("instrument_type") == "daily_leveraged_product":
            if not all(
                value.get(field)
                for field in (
                    "daily_reset",
                    "underlying_symbol",
                    "underlying_market",
                    "underlying_currency",
                    "leverage_factor",
                )
            ):
                blockers.append("daily_reset_product_evidence_incomplete")

    @staticmethod
    def _product_evidence(
        *,
        instrument: Mapping[str, Any] | None,
        portfolio_context: Mapping[str, Any] | None,
        context_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if isinstance(instrument, Mapping) and isinstance(
            instrument.get("product_evidence"), Mapping
        ):
            return dict(instrument["product_evidence"])
        for source in (portfolio_context, context_snapshot):
            if isinstance(source, Mapping) and isinstance(
                source.get("product_evidence"), Mapping
            ):
                return dict(source["product_evidence"])
        if isinstance(context_snapshot, Mapping):
            for item in context_snapshot.get("decision_evidence") or []:
                body = item.get("body") if isinstance(item, Mapping) else None
                if isinstance(body, Mapping) and isinstance(
                    body.get("product_evidence"), Mapping
                ):
                    return dict(body["product_evidence"])
        return None

    @staticmethod
    def _validate_product_evidence(
        instrument: Mapping[str, Any] | None,
        *,
        product_evidence: Mapping[str, Any] | None,
        blockers: list[str],
    ) -> None:
        instrument_type = str((instrument or {}).get("instrument_type") or "")
        if instrument_type not in {"qdii", "daily_leveraged_product"}:
            return
        if product_evidence is None:
            blockers.append(
                "daily_reset_product_evidence_missing"
                if instrument_type == "daily_leveraged_product"
                else "qdii_product_evidence_missing"
            )
            return
        if str(product_evidence.get("instrument_type") or "") != instrument_type:
            blockers.append("product_evidence_identity_mismatch")
        if instrument_type == "qdii":
            for field_name, blocker in (
                ("nav_iopv_available", "qdii_nav_iopv_missing"),
                ("premium_discount_available", "qdii_premium_discount_missing"),
                ("underlying_fx_available", "qdii_underlying_fx_missing"),
                ("spread_available", "qdii_spread_missing"),
                ("tracking_available", "qdii_tracking_evidence_missing"),
            ):
                if product_evidence.get(field_name) is not True:
                    blockers.append(blocker)
            return
        for field_name, blocker in (
            ("official_terms_available", "daily_reset_official_terms_missing"),
            ("underlying_identity_available", "daily_reset_underlying_identity_missing"),
            ("underlying_same_cutoff_available", "daily_reset_underlying_same_cutoff_missing"),
            (
                "completed_session_leverage_available",
                "daily_reset_completed_session_leverage_missing",
            ),
            ("path_decay_rebalance_available", "daily_reset_path_decay_rebalance_missing"),
            ("liquidity_available", "daily_reset_liquidity_missing"),
            ("horizon_fit_evaluated", "daily_reset_horizon_fit_missing"),
        ):
            if product_evidence.get(field_name) is not True:
                blockers.append(blocker)

    @classmethod
    def _validate_benchmark(
        cls,
        value: Mapping[str, Any] | None,
        *,
        expected_code: str,
        cutoff: datetime,
        blockers: list[str],
    ) -> None:
        if value is None:
            blockers.append("benchmark_evidence_missing")
            return
        if str(value.get("code") or "") != expected_code:
            blockers.append("benchmark_identity_mismatch")
        if not all(
            value.get(field)
            for field in (
                "price",
                "adjustment_identity",
                "evidence_source",
                "evidence_as_of",
                "evidence_hash",
            )
        ):
            blockers.append("benchmark_evidence_incomplete")
        benchmark_as_of = cls._evidence_datetime(value.get("evidence_as_of"))
        if benchmark_as_of is not None:
            if daily_bar_not_final(
                market=str(value.get("market") or ""),
                as_of=benchmark_as_of,
                cutoff=cutoff,
            ):
                blockers.append("benchmark_evidence_not_final")
            elif cutoff - benchmark_as_of > MAX_MARKET_EVIDENCE_AGE:
                blockers.append("benchmark_evidence_stale")
        if value.get("stale"):
            blockers.append("benchmark_evidence_stale")

    @staticmethod
    def _validate_adjustment_compatibility(
        position: Mapping[str, Any] | None,
        benchmark: Mapping[str, Any] | None,
        blockers: list[str],
    ) -> None:
        position_adjustment = (position or {}).get("adjustment_identity")
        benchmark_adjustment = (benchmark or {}).get("adjustment_identity")
        if not position_adjustment:
            blockers.append("position_adjustment_identity_unknown")
        if not benchmark_adjustment:
            blockers.append("benchmark_adjustment_identity_unknown")
        if (
            position_adjustment
            and benchmark_adjustment
            and position_adjustment != benchmark_adjustment
        ):
            blockers.append("benchmark_adjustment_identity_mismatch")

    @classmethod
    def _validate_fx(
        cls,
        position: Mapping[str, Any] | None,
        *,
        reporting_currency: str,
        cutoff: datetime,
        blockers: list[str],
    ) -> None:
        if position is None:
            return
        fx = position.get("fx")
        if not isinstance(fx, Mapping) or not all(
            fx.get(field) is not None
            for field in ("pair", "rate", "as_of", "source", "source_hash")
        ):
            blockers.append("fx_evidence_missing")
            return
        if not fx.get("available") or fx.get("stale"):
            blockers.append("fx_evidence_invalid")
        fx_as_of = cls._evidence_datetime(fx.get("as_of"))
        if fx_as_of is not None and (
            fx_as_of > cutoff or cutoff - fx_as_of > MAX_FX_EVIDENCE_AGE
        ):
            blockers.append("fx_evidence_stale")
        pair = str(fx.get("pair") or "").strip().upper()
        expected_pair = (
            f"{str(position.get('currency') or '').strip().upper()}/{reporting_currency}"
        )
        if pair != expected_pair:
            blockers.append("fx_pair_mismatch")
        try:
            rate = float(fx.get("rate"))
        except (TypeError, ValueError):
            rate = None
        if rate is None or rate <= 0:
            blockers.append("fx_rate_invalid")
        if pair == expected_pair and expected_pair.split("/")[0] == reporting_currency:
            if rate != 1.0:
                blockers.append("fx_identity_rate_invalid")

    @staticmethod
    def _validate_risk(snapshot: Mapping[str, Any], blockers: list[str]) -> None:
        if not isinstance(snapshot.get("risk_policy"), Mapping):
            blockers.append("risk_policy_missing")
        risk_budget = snapshot.get("risk_budget")
        if not isinstance(risk_budget, Mapping) or not risk_budget.get("evaluated"):
            blockers.append("risk_budget_not_evaluated")

    @staticmethod
    def _validate_signal_readiness(
        signal: Mapping[str, Any],
        blockers: list[str],
    ) -> None:
        data_quality = signal.get("data_quality_summary")
        if isinstance(data_quality, Mapping):
            quality_level = str(data_quality.get("level") or "").strip().lower()
            limitations = data_quality.get("limitations")
            if quality_level == "limited" or (
                isinstance(limitations, list) and bool(limitations)
            ):
                blockers.append("decision_data_quality_limited")

        metadata = signal.get("metadata")
        if (
            isinstance(metadata, Mapping)
            and str(metadata.get("execution_status") or "").strip().lower() == "blocked"
        ):
            blockers.append("decision_execution_blocked")

    @staticmethod
    def _validate_decision(value: Mapping[str, Any], blockers: list[str]) -> None:
        for field in (
            "position_action",
            "incremental_action",
            "supporting_evidence",
            "opposing_evidence",
            "watch_conditions",
            "invalidation",
            "next_review",
        ):
            if not value.get(field):
                blockers.append(f"decision_{field}_missing")
        confidence = value.get("confidence_by_horizon")
        if not isinstance(confidence, Mapping) or set(confidence) != set(QUALITY_HORIZONS):
            blockers.append("confidence_horizons_incomplete")
