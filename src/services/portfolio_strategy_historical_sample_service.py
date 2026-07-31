# -*- coding: utf-8 -*-
"""Build deterministic, point-in-time eligible strategy events from frozen input."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


FROZEN_SOURCE_SCHEMA_VERSION = "frozen-historical-source-v1"
DATASET_SCHEMA_VERSION = "portfolio-strategy-events-v1"
_HORIZONS = (("5d", 5), ("20d", 20), ("60d", 60))


def _canonical_json(value: Any) -> str:
    """Match the registry's canonical JSON rule without importing its database graph."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class PortfolioStrategyHistoricalSampleService:
    """Pure converter for caller-provided, versioned historical source JSON."""

    def build(self, *, source: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(source, dict) or source.get("schema_version") != FROZEN_SOURCE_SCHEMA_VERSION:
            raise ValueError("frozen_source_schema_unsupported")
        if not isinstance(source.get("synthetic"), bool):
            raise ValueError("synthetic_classification_required")
        frozen_at = self._datetime(source.get("frozen_at"))
        if frozen_at is None:
            raise ValueError("frozen_at_invalid")
        reporting_currency = str(source.get("reporting_currency") or "").strip()
        if not reporting_currency:
            raise ValueError("reporting_currency_required")
        source_payload = {key: value for key, value in source.items() if key != "source_snapshot_hash"}
        source_snapshot_hash = _sha256_json(source_payload)
        if "source_snapshot_hash" in source and source["source_snapshot_hash"] != source_snapshot_hash:
            raise ValueError("source_snapshot_hash_mismatch")
        candidates = source.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("candidates_invalid")

        eligible_events: list[dict[str, Any]] = []
        excluded_events: list[dict[str, str]] = []
        semantic_decisions: set[str] = set()
        for candidate in sorted(
            candidates,
            key=lambda item: str(item.get("candidate_id") or "") if isinstance(item, dict) else "",
        ):
            candidate_id = str(candidate.get("candidate_id") or "") if isinstance(candidate, dict) else ""
            event, reason_code, semantic_key = self._build_event(
                candidate, frozen_at=frozen_at, reporting_currency=reporting_currency
            )
            if event is not None and semantic_key in semantic_decisions:
                event = None
                reason_code = "semantic_duplicate_decision"
            if event is None:
                excluded_events.append(
                    {
                        "candidate_id": candidate_id,
                        "reason_code": reason_code or "candidate_contract_invalid",
                    }
                )
                continue
            event["source_snapshot_hash"] = source_snapshot_hash
            event["reporting_currency"] = reporting_currency
            semantic_decisions.add(semantic_key)
            eligible_events.append(event)

        eligible_events.sort(key=lambda event: event["event_id"])
        eligible_event_ids = sorted({event["event_id"] for event in eligible_events})
        payload = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "source_snapshot_hash": source_snapshot_hash,
            "synthetic": source["synthetic"],
            "frozen_at": frozen_at.isoformat(),
            "reporting_currency": reporting_currency,
            "candidate_count": len(candidates),
            "eligible_events": eligible_events,
            "events": eligible_events,
            "eligible_event_ids": eligible_event_ids,
            "eligible_event_set_hash": _sha256_json(eligible_event_ids),
            "excluded_events": excluded_events,
        }
        return {**payload, "dataset_hash": _sha256_json(payload)}

    def _build_event(
        self, candidate: Any, *, frozen_at: datetime, reporting_currency: str
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        if not isinstance(candidate, dict):
            return None, "candidate_contract_invalid", ""
        decision = candidate.get("decision")
        cutoff = self._evidence_as_of(decision)
        decision_id = str(decision.get("decision_id") or "") if isinstance(decision, dict) else ""
        decision_cutoff = self._datetime(decision.get("decision_cutoff")) if isinstance(decision, dict) else None
        semantic_fields = (
            "account_id",
            "strategy_key",
            "strategy_version",
            "position_action",
            "incremental_action",
            "decision_input_hash",
        )
        if (
            cutoff is None
            or not decision_id
            or decision_cutoff is None
            or cutoff > decision_cutoff
            or not all(str(decision.get(field) or "").strip() for field in semantic_fields)
        ):
            return None, "decision_evidence_invalid", ""
        structured_inputs = candidate.get("structured_inputs")
        if (
            not isinstance(structured_inputs, dict)
            or not structured_inputs
            or not all(isinstance(key, str) for key in structured_inputs)
            or {"ai_analysis", "free_text"} & set(structured_inputs)
        ):
            return None, "structured_inputs_invalid", ""
        try:
            structured_inputs_hash = _sha256_json(structured_inputs)
        except (TypeError, ValueError):
            return None, "structured_inputs_invalid", ""
        if decision["decision_input_hash"] != structured_inputs_hash:
            return None, "decision_input_hash_mismatch", ""

        identity = candidate.get("identity")
        if not self._evidence_before(identity, decision_cutoff):
            return None, "identity_evidence_invalid", ""
        market = str(identity.get("market") or "").strip().lower()
        symbol = str(identity.get("symbol") or "").strip()
        instrument_type = str(identity.get("instrument_type") or "").strip()
        currency = str(identity.get("currency") or "").strip()
        if not all((market, symbol, instrument_type, currency)):
            return None, "identity_evidence_invalid", ""
        product = candidate.get("product")
        if not self._evidence_before(product, decision_cutoff) or product.get("instrument_type") != instrument_type:
            return None, "product_evidence_invalid", ""
        if instrument_type == "daily_leveraged_product":
            reset_evidence = product.get("daily_reset_evidence")
            underlying_evidence = product.get("underlying_evidence")
            if (
                not str(product.get("reset_frequency") or "").strip()
                or not str(product.get("underlying_identity") or "").strip()
                or not self._evidence_before(reset_evidence, decision_cutoff)
                or not self._evidence_before(underlying_evidence, decision_cutoff)
                or reset_evidence.get("reset_frequency") != product.get("reset_frequency")
                or underlying_evidence.get("underlying_identity") != product.get("underlying_identity")
            ):
                return None, "daily_leveraged_product_evidence_invalid", ""

        adjustment = candidate.get("adjustment")
        if not self._evidence_before(adjustment, decision_cutoff) or not str(
            adjustment.get("identity") or ""
        ).strip():
            return None, "adjustment_evidence_invalid", ""
        benchmark = candidate.get("benchmark")
        if not self._evidence_before(benchmark, decision_cutoff) or not all(
            str(benchmark.get(field) or "").strip()
            for field in ("symbol", "currency", "adjusted_price_identity")
        ):
            return None, "benchmark_evidence_invalid", ""
        if benchmark["currency"] != currency:
            return None, "benchmark_currency_mismatch", ""
        fx = candidate.get("fx")
        if not self._evidence_before(fx, decision_cutoff) or not str(fx.get("pair") or "").strip() or self._positive(
            fx.get("rate")
        ) is None:
            return None, "fx_evidence_invalid", ""
        if fx["pair"] != f"{currency}/{reporting_currency}":
            return None, "fx_pair_mismatch", ""
        if currency == reporting_currency and float(fx["rate"]) != 1.0:
            return None, "fx_rate_mismatch", ""
        cost_and_trading = candidate.get("cost_and_trading")
        if not self._evidence_before(cost_and_trading, decision_cutoff) or self._positive(
            cost_and_trading.get("lot_size")
        ) is None:
            return None, "cost_and_trading_evidence_invalid", ""

        bars = self._indexed_bars(candidate.get("bars"))
        benchmark_bars = self._indexed_bars(benchmark.get("bars"))
        execution_bar = self._first_execution_bar(bars, decision_cutoff, frozen_at=frozen_at)
        if execution_bar is None:
            return None, "execution_bar_missing", ""
        execution_time, instrument_execution = execution_bar
        benchmark_execution = benchmark_bars.get(execution_time)
        if not self._valid_bar(benchmark_execution, execution_time, frozen_at=frozen_at):
            return None, "benchmark_execution_unaligned", ""

        horizon_results: dict[str, dict[str, Any]] = {}
        for horizon, count in _HORIZONS:
            valid_bars = [
                (timestamp, bar)
                for timestamp, bar in bars.items()
                if timestamp > execution_time and self._valid_bar(bar, timestamp, frozen_at=frozen_at)
            ]
            valid_bars.sort(key=lambda item: item[0])
            if len(valid_bars) < count:
                return None, "horizon_bars_insufficient", ""
            horizon_bars = valid_bars[:count]
            if any(
                not self._valid_bar(benchmark_bars.get(timestamp), timestamp, frozen_at=frozen_at)
                for timestamp, _ in horizon_bars
            ):
                return None, "benchmark_horizon_unaligned", ""
            end_time, end_bar = horizon_bars[-1]
            benchmark_end = benchmark_bars[end_time]
            horizon_results[horizon] = {
                "timestamp": end_time.isoformat(),
                "end_close": float(end_bar["close"]),
                "max_high": max(float(bar["high"]) for _, bar in horizon_bars),
                "min_low": min(float(bar["low"]) for _, bar in horizon_bars),
                "benchmark_end_close": float(benchmark_end["close"]),
                "instrument_bar_hash": str(end_bar["source_hash"]),
                "benchmark_bar_hash": str(benchmark_end["source_hash"]),
                "instrument_bar_as_of": end_bar["as_of"],
                "benchmark_bar_as_of": benchmark_end["as_of"],
            }

        event_id = str(candidate.get("candidate_id") or "")
        if not event_id:
            return None, "candidate_identity_invalid", ""
        semantic_key = _sha256_json(
            {
                **{field: decision[field] for field in semantic_fields},
                "decision_date": decision_cutoff.date().isoformat(),
                "symbol": symbol,
            }
        )
        return (
            {
                "event_id": event_id,
                "decision_cutoff": decision_cutoff.isoformat(),
                "market": market,
                "symbol": symbol,
                "instrument_type": instrument_type,
                "currency": currency,
                "reporting_currency": "",
                "adjusted_price_identity": adjustment["identity"],
                "adjustment_evidence": {
                    key: adjustment[key] for key in ("identity", "as_of", "source", "source_hash")
                },
                "product_evidence": {
                    key: product[key]
                    for key in (
                        "instrument_type",
                        "as_of",
                        "source",
                        "source_hash",
                        "reset_frequency",
                        "underlying_identity",
                    )
                    if key in product
                },
                **(
                    {
                        "daily_reset_evidence": product["daily_reset_evidence"],
                        "underlying_evidence": product["underlying_evidence"],
                    }
                    if instrument_type == "daily_leveraged_product"
                    else {}
                ),
                "benchmark": {
                    "symbol": benchmark["symbol"],
                    "currency": benchmark["currency"],
                    "adjusted_price_identity": benchmark["adjusted_price_identity"],
                    "source": benchmark["source"],
                    "source_hash": benchmark["source_hash"],
                    "as_of": benchmark["as_of"],
                },
                "fx": {key: fx[key] for key in ("pair", "rate", "as_of", "source", "source_hash")},
                "cost_and_trading": {
                    key: cost_and_trading[key]
                    for key in ("lot_size", "as_of", "source", "source_hash")
                },
                "source_snapshot_hash": "",
                "decision_evidence": {
                    key: decision[key] for key in ("decision_id", *semantic_fields, "as_of", "source", "source_hash")
                },
                "structured_inputs": candidate.get("structured_inputs"),
                "market_regime": candidate.get("market_regime"),
                "period": candidate.get("period"),
                "execution": {
                    "timestamp": execution_time.isoformat(),
                    "price": float(instrument_execution["open"]),
                    "benchmark_price": float(benchmark_execution["open"]),
                    "instrument_bar_hash": str(instrument_execution["source_hash"]),
                    "benchmark_bar_hash": str(benchmark_execution["source_hash"]),
                    "instrument_bar_as_of": instrument_execution["as_of"],
                    "benchmark_bar_as_of": benchmark_execution["as_of"],
                },
                "horizon_results": horizon_results,
            },
            None,
            semantic_key,
        )

    @classmethod
    def _indexed_bars(cls, rows: Any) -> dict[datetime, dict[str, Any]]:
        if not isinstance(rows, list):
            return {}
        indexed: dict[datetime, dict[str, Any]] = {}
        for row in rows:
            timestamp = cls._datetime(row.get("timestamp")) if isinstance(row, dict) else None
            if timestamp is not None and timestamp not in indexed:
                indexed[timestamp] = row
        return indexed

    @classmethod
    def _first_execution_bar(
        cls, bars: dict[datetime, dict[str, Any]], cutoff: datetime, *, frozen_at: datetime
    ) -> tuple[datetime, dict[str, Any]] | None:
        for timestamp in sorted(bars):
            bar = bars[timestamp]
            if timestamp > cutoff and cls._valid_bar(bar, timestamp, frozen_at=frozen_at):
                return timestamp, bar
        return None

    @classmethod
    def _valid_bar(cls, bar: Any, timestamp: datetime, *, frozen_at: datetime) -> bool:
        as_of = cls._evidence_as_of(bar)
        return (
            isinstance(bar, dict)
            and bar.get("tradable") is True
            and cls._positive(bar.get("open")) is not None
            and cls._positive(bar.get("close")) is not None
            and cls._positive(bar.get("high")) is not None
            and cls._positive(bar.get("low")) is not None
            and as_of is not None
            and timestamp <= as_of <= frozen_at
        )

    @classmethod
    def _evidence_before(cls, evidence: Any, cutoff: datetime) -> bool:
        as_of = cls._evidence_as_of(evidence)
        return as_of is not None and as_of <= cutoff

    @classmethod
    def _evidence_as_of(cls, evidence: Any) -> datetime | None:
        if not isinstance(evidence, dict) or not str(evidence.get("source") or "").strip():
            return None
        if not str(evidence.get("source_hash") or "").strip():
            return None
        return cls._datetime(evidence.get("as_of"))

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None

    @staticmethod
    def _positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
