# -*- coding: utf-8 -*-
"""Validated service boundary for canonical portfolio instrument identity."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from data_provider.base import canonical_stock_code
from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import (
    PORTFOLIO_INSTRUMENT_TYPES,
    PORTFOLIO_INSTRUMENT_VERIFICATION_STATUSES,
    PortfolioInstrument,
    to_utc_naive_datetime,
)


VALID_INSTRUMENT_MARKETS = frozenset({"cn", "hk", "us", "jp", "kr", "tw"})
INSTRUMENT_FIELDS = frozenset({
    "symbol",
    "market",
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
    "evidence_source",
    "evidence_as_of",
    "metadata",
})


class PortfolioInstrumentService:
    """Own validation and normalization before instrument-registry writes."""

    def __init__(self, repo: Optional[PortfolioRepository] = None):
        self.repo = repo or PortfolioRepository()

    def create_instrument(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        fields = self._normalize_and_validate(payload)
        return self._to_dict(self.repo.create_instrument(fields))

    def get_instrument(self, *, symbol: str, market: str) -> Optional[Dict[str, Any]]:
        row = self.repo.get_instrument(
            symbol=self._normalize_symbol(symbol, "symbol"),
            market=self._normalize_market(market, "market"),
        )
        return self._to_dict(row) if row is not None else None

    def list_instruments(self) -> List[Dict[str, Any]]:
        return [self._to_dict(row) for row in self.repo.list_instruments()]

    def update_instrument(
        self,
        *,
        symbol: str,
        market: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        symbol_norm = self._normalize_symbol(symbol, "symbol")
        market_norm = self._normalize_market(market, "market")
        existing = self.repo.get_instrument(symbol=symbol_norm, market=market_norm)
        if existing is None:
            raise ValueError(f"portfolio instrument not found: {market_norm}/{symbol_norm}")
        if not payload:
            raise ValueError("No fields provided for update")
        if "symbol" in payload and self._normalize_symbol(payload["symbol"], "symbol") != symbol_norm:
            raise ValueError("symbol cannot be changed")
        if "market" in payload and self._normalize_market(payload["market"], "market") != market_norm:
            raise ValueError("market cannot be changed")

        merged = self._to_input_dict(existing)
        merged.update(payload)
        merged["symbol"] = symbol_norm
        merged["market"] = market_norm
        fields = self._normalize_and_validate(merged)
        return self._to_dict(self.repo.upsert_instrument(fields))

    def delete_instrument(self, *, symbol: str, market: str) -> bool:
        return self.repo.delete_instrument(
            symbol=self._normalize_symbol(symbol, "symbol"),
            market=self._normalize_market(market, "market"),
        )

    @classmethod
    def _normalize_and_validate(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("instrument payload must be an object")
        unsupported = sorted(set(payload) - INSTRUMENT_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported instrument fields: {', '.join(unsupported)}")

        symbol = cls._normalize_symbol(payload.get("symbol"), "symbol")
        market = cls._normalize_market(payload.get("market"), "market")
        quote_currency = cls._normalize_currency(
            payload.get("quote_currency"),
            "quote_currency",
        )
        instrument_type = str(payload.get("instrument_type") or "").strip().lower()
        if instrument_type not in PORTFOLIO_INSTRUMENT_TYPES:
            raise ValueError(
                "instrument_type must be one of: "
                + ", ".join(PORTFOLIO_INSTRUMENT_TYPES)
            )
        verification_status = str(
            payload.get("verification_status") or "missing"
        ).strip().lower()
        if verification_status not in PORTFOLIO_INSTRUMENT_VERIFICATION_STATUSES:
            raise ValueError(
                "verification_status must be one of: "
                + ", ".join(PORTFOLIO_INSTRUMENT_VERIFICATION_STATUSES)
            )

        fields: Dict[str, Any] = {
            "symbol": symbol,
            "market": market,
            "quote_currency": quote_currency,
            "instrument_type": instrument_type,
            "underlying_symbol": cls._optional_symbol(payload.get("underlying_symbol")),
            "underlying_market": cls._optional_market(payload.get("underlying_market")),
            "underlying_currency": cls._optional_currency(payload.get("underlying_currency")),
            "leverage_factor": cls._optional_positive_number(
                payload.get("leverage_factor"),
                "leverage_factor",
            ),
            "daily_reset": cls._boolean(payload.get("daily_reset", False), "daily_reset"),
            "conversion_ratio": cls._optional_positive_number(
                payload.get("conversion_ratio"),
                "conversion_ratio",
            ),
            "trade_lot_size": cls._positive_number(
                payload.get("trade_lot_size"),
                "trade_lot_size",
            ),
            "requires_premium_check": cls._boolean(
                payload.get("requires_premium_check", False),
                "requires_premium_check",
            ),
            "verification_status": verification_status,
            "evidence_source": str(payload.get("evidence_source") or "").strip() or None,
            "evidence_as_of": cls._optional_datetime(payload.get("evidence_as_of")),
            "metadata_json": cls._metadata_json(payload.get("metadata")),
        }
        cls._validate_product_structure(fields)
        cls._validate_verification(fields)
        return fields

    @staticmethod
    def _validate_product_structure(fields: Dict[str, Any]) -> None:
        instrument_type = fields["instrument_type"]
        if instrument_type == "qdii" and not fields["requires_premium_check"]:
            raise ValueError("requires_premium_check must be true for qdii")
        if instrument_type in {"adr_ads", "daily_leveraged_product"}:
            for field_name in (
                "underlying_symbol",
                "underlying_market",
                "underlying_currency",
            ):
                if not fields[field_name]:
                    raise ValueError(f"{field_name} is required for {instrument_type}")
        if instrument_type == "adr_ads":
            if fields["conversion_ratio"] is None:
                raise ValueError("conversion_ratio is required for adr_ads")
            if not fields["requires_premium_check"]:
                raise ValueError("requires_premium_check must be true for adr_ads")
        if instrument_type == "daily_leveraged_product":
            if fields["leverage_factor"] is None:
                raise ValueError("leverage_factor is required for daily_leveraged_product")
            if not fields["daily_reset"]:
                raise ValueError("daily_reset must be true for daily_leveraged_product")

    @staticmethod
    def _validate_verification(fields: Dict[str, Any]) -> None:
        if fields["verification_status"] != "verified":
            return
        if fields["instrument_type"] == "unknown":
            raise ValueError("unknown instrument_type cannot be verified")
        if not fields["evidence_source"]:
            raise ValueError("evidence_source is required for verified identity")
        if fields["evidence_as_of"] is None:
            raise ValueError("evidence_as_of is required for verified identity")

    @staticmethod
    def _normalize_symbol(value: Any, field_name: str) -> str:
        normalized = canonical_stock_code(str(value or ""))
        if not normalized:
            raise ValueError(f"{field_name} is required")
        return normalized

    @classmethod
    def _optional_symbol(cls, value: Any) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return cls._normalize_symbol(value, "underlying_symbol")

    @staticmethod
    def _normalize_market(value: Any, field_name: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in VALID_INSTRUMENT_MARKETS:
            raise ValueError(
                f"{field_name} must be one of: "
                + ", ".join(sorted(VALID_INSTRUMENT_MARKETS))
            )
        return normalized

    @classmethod
    def _optional_market(cls, value: Any) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return cls._normalize_market(value, "underlying_market")

    @staticmethod
    def _normalize_currency(value: Any, field_name: str) -> str:
        normalized = str(value or "").strip().upper()
        if not normalized:
            raise ValueError(f"{field_name} is required")
        return normalized

    @classmethod
    def _optional_currency(cls, value: Any) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return cls._normalize_currency(value, "underlying_currency")

    @staticmethod
    def _positive_number(value: Any, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be > 0")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be > 0") from exc
        if number <= 0:
            raise ValueError(f"{field_name} must be > 0")
        return number

    @classmethod
    def _optional_positive_number(cls, value: Any, field_name: str) -> Optional[float]:
        if value is None:
            return None
        return cls._positive_number(value, field_name)

    @staticmethod
    def _boolean(value: Any, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"{field_name} must be a boolean")

    @staticmethod
    def _optional_datetime(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("evidence_as_of must be an ISO datetime") from exc
        else:
            raise ValueError("evidence_as_of must be a datetime")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("evidence_as_of must include a timezone offset")
        return to_utc_naive_datetime(parsed)

    @classmethod
    def _metadata_json(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        normalized = dict(value)
        if "risk_sector" in normalized:
            normalized["risk_sector"] = cls.normalize_risk_sector(
                normalized["risk_sector"]
            )
        return json.dumps(
            normalized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def normalize_risk_sector(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("risk_sector must be an object")

        taxonomy = str(value.get("taxonomy") or "").strip()
        if taxonomy != "portfolio-risk-v1":
            raise ValueError("risk_sector.taxonomy must be portfolio-risk-v1")

        as_of = str(value.get("as_of") or "").strip()
        try:
            date.fromisoformat(as_of)
        except ValueError as exc:
            raise ValueError("risk_sector.as_of must be an ISO date") from exc

        source = str(value.get("source") or "").strip()
        if not source:
            raise ValueError("risk_sector.source is required")

        raw_exposures = value.get("exposures")
        if not isinstance(raw_exposures, list) or not raw_exposures:
            raise ValueError("risk_sector.exposures must be a non-empty array")

        exposures = []
        seen_sectors = set()
        total_weight = 0.0
        for item in raw_exposures:
            if not isinstance(item, dict):
                raise ValueError("risk_sector.exposures items must be objects")
            sector = str(item.get("sector") or "").strip()
            if not sector:
                raise ValueError("risk_sector.exposures.sector is required")
            if sector in seen_sectors:
                raise ValueError("risk_sector.exposures sectors must be unique")
            seen_sectors.add(sector)
            try:
                weight = float(item.get("weight_pct"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "risk_sector.exposures.weight_pct must be > 0"
                ) from exc
            if not math.isfinite(weight) or weight <= 0 or weight > 100:
                raise ValueError("risk_sector.exposures.weight_pct must be > 0 and <= 100")
            total_weight += weight
            exposures.append({"sector": sector, "weight_pct": weight})

        if not math.isclose(total_weight, 100.0, rel_tol=0.0, abs_tol=0.05):
            raise ValueError("risk_sector.exposures weights must total 100")

        exposures.sort(key=lambda item: item["sector"])
        return {
            "taxonomy": taxonomy,
            "as_of": as_of,
            "source": source,
            "exposures": exposures,
        }

    @staticmethod
    def _metadata(value: Optional[str]) -> Dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _to_input_dict(cls, row: PortfolioInstrument) -> Dict[str, Any]:
        return {
            "symbol": row.symbol,
            "market": row.market,
            "quote_currency": row.quote_currency,
            "instrument_type": row.instrument_type,
            "underlying_symbol": row.underlying_symbol,
            "underlying_market": row.underlying_market,
            "underlying_currency": row.underlying_currency,
            "leverage_factor": row.leverage_factor,
            "daily_reset": row.daily_reset,
            "conversion_ratio": row.conversion_ratio,
            "trade_lot_size": row.trade_lot_size,
            "requires_premium_check": row.requires_premium_check,
            "verification_status": row.verification_status,
            "evidence_source": row.evidence_source,
            "evidence_as_of": (
                row.evidence_as_of.replace(tzinfo=timezone.utc)
                if row.evidence_as_of
                else None
            ),
            "metadata": cls._metadata(row.metadata_json),
        }

    @classmethod
    def _to_dict(cls, row: PortfolioInstrument) -> Dict[str, Any]:
        result = cls._to_input_dict(row)
        result.update({
            "id": row.id,
            "evidence_as_of": (
                row.evidence_as_of.replace(tzinfo=timezone.utc).isoformat()
                if row.evidence_as_of
                else None
            ),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })
        return result
