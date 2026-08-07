# -*- coding: utf-8 -*-
"""Normalize immutable, cutoff-bound evidence for complex portfolio products."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Optional


PRODUCT_EVIDENCE_SCHEMA_VERSION = "portfolio-product-evidence-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def build_product_evidence_component(
    *,
    as_of: Any,
    source: Any,
    source_version: Any,
    **values: Any,
) -> dict[str, Any]:
    """Build one immutable component envelope consumed by the strict validator."""

    component = {
        "available": True,
        "as_of": (
            as_of.isoformat()
            if isinstance(as_of, (datetime,))
            else str(as_of or "").strip()
        ),
        "source": str(source or "").strip(),
        "source_version": str(source_version or "").strip(),
        **values,
    }
    component["source_hash"] = _hash(component)
    return component


def product_evidence_from_instrument(
    instrument: Any,
    *,
    cutoff: datetime,
) -> Optional[dict[str, Any]]:
    """Return validated product evidence or a product-specific fail-closed payload."""

    instrument_type = str(_value(instrument, "instrument_type") or "").strip().lower()
    if instrument_type not in {"qdii", "daily_leveraged_product"}:
        return None
    fallback = _fallback(instrument, instrument_type=instrument_type)
    raw = _raw_product_evidence(instrument)
    if not isinstance(raw, Mapping):
        return fallback

    cutoff_value = _aware_datetime(cutoff)
    blockers: list[str] = []
    if cutoff_value is None:
        blockers.append("product_evidence_cutoff_invalid")
    if str(raw.get("schema_version") or "") != PRODUCT_EVIDENCE_SCHEMA_VERSION:
        blockers.append("product_evidence_schema_invalid")
    expected_identity = (
        str(_value(instrument, "market") or "").strip().lower(),
        str(_value(instrument, "symbol") or "").strip().upper(),
        instrument_type,
    )
    actual_identity = (
        str(raw.get("market") or "").strip().lower(),
        str(raw.get("symbol") or "").strip().upper(),
        str(raw.get("instrument_type") or "").strip().lower(),
    )
    if actual_identity != expected_identity:
        blockers.append("product_evidence_identity_mismatch")
    evidence_cutoff = _aware_datetime(raw.get("evidence_cutoff"))
    if evidence_cutoff is None:
        blockers.append("product_evidence_cutoff_invalid")
    elif cutoff_value is not None and evidence_cutoff.astimezone(timezone.utc) != cutoff_value.astimezone(
        timezone.utc
    ):
        blockers.append("product_evidence_cutoff_mismatch")

    if instrument_type == "qdii":
        component_specs = (
            ("nav_iopv", "nav_iopv_available", "qdii_nav_iopv_missing", _valid_nav_iopv),
            (
                "premium_discount",
                "premium_discount_available",
                "qdii_premium_discount_missing",
                lambda item, _: _finite(item.get("premium_discount_pct")),
            ),
            ("underlying_fx", "underlying_fx_available", "qdii_underlying_fx_missing", _valid_fx),
            ("spread", "spread_available", "qdii_spread_missing", _valid_spread),
            (
                "tracking",
                "tracking_available",
                "qdii_tracking_evidence_missing",
                lambda item, _: _finite(item.get("tracking_difference_pct")),
            ),
        )
    else:
        component_specs = (
            (
                "official_terms",
                "official_terms_available",
                "daily_reset_official_terms_missing",
                _valid_official_terms,
            ),
            (
                "underlying_same_cutoff",
                "underlying_same_cutoff_available",
                "daily_reset_underlying_same_cutoff_missing",
                _valid_underlying,
            ),
            (
                "intraday_leverage",
                "intraday_leverage_available",
                "daily_reset_intraday_leverage_missing",
                _valid_intraday_leverage,
            ),
            (
                "path_decay_rebalance",
                "path_decay_rebalance_available",
                "daily_reset_path_decay_rebalance_missing",
                _valid_path_rebalance,
            ),
            ("liquidity", "liquidity_available", "daily_reset_liquidity_missing", _valid_spread),
            (
                "horizon_fit",
                "horizon_fit_evaluated",
                "daily_reset_horizon_fit_missing",
                _valid_horizon_fit,
            ),
        )

    normalized_components: dict[str, Any] = {}
    flags: dict[str, bool] = {}
    identity_context = {
        "market": expected_identity[0],
        "symbol": expected_identity[1],
        "instrument_type": instrument_type,
        "underlying_market": str(_value(instrument, "underlying_market") or "").strip().lower(),
        "underlying_symbol": str(_value(instrument, "underlying_symbol") or "").strip().upper(),
        "underlying_currency": str(_value(instrument, "underlying_currency") or "").strip().upper(),
        "leverage_factor": _value(instrument, "leverage_factor"),
    }
    common_valid = not blockers
    for component_name, flag_name, blocker, validator in component_specs:
        component = raw.get(component_name)
        component_valid = bool(
            common_valid
            and isinstance(component, Mapping)
            and _valid_component_envelope(component, cutoff=cutoff_value)
            and validator(component, identity_context)
        )
        flags[flag_name] = component_valid
        if isinstance(component, Mapping):
            normalized_components[component_name] = dict(component)
        if not component_valid:
            blockers.append(blocker)

    if instrument_type == "daily_leveraged_product":
        underlying_identity_available = bool(
            _value(instrument, "verification_status") == "verified"
            and identity_context["underlying_market"]
            and identity_context["underlying_symbol"]
            and identity_context["underlying_currency"]
            and _positive(identity_context["leverage_factor"])
            and _value(instrument, "daily_reset") is True
        )
        flags["underlying_identity_available"] = underlying_identity_available
        if not underlying_identity_available:
            blockers.append("daily_reset_underlying_identity_missing")

    blockers = list(dict.fromkeys(blockers))
    body: dict[str, Any] = {
        "schema_version": PRODUCT_EVIDENCE_SCHEMA_VERSION,
        "instrument_type": instrument_type,
        "market": expected_identity[0],
        "symbol": expected_identity[1],
        "evidence_cutoff": (
            evidence_cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if evidence_cutoff is not None
            else None
        ),
        "status": "insufficient" if blockers else "ready",
        **flags,
        "components": normalized_components,
    }
    if instrument_type == "daily_leveraged_product":
        body.update(
            {
                "underlying_identity": (
                    f"{identity_context['underlying_market']}:{identity_context['underlying_symbol']}"
                    if identity_context["underlying_market"] and identity_context["underlying_symbol"]
                    else None
                ),
                "daily_reset": _value(instrument, "daily_reset") is True,
                "leverage_factor": _value(instrument, "leverage_factor"),
            }
        )
    body["evidence_hash"] = _hash(body)
    body["blockers"] = blockers
    return body


def frozen_product_evidence_is_ready(
    instrument: Any,
    *,
    cutoff: datetime,
) -> bool:
    """Revalidate normalized frozen product evidence at its original cutoff."""

    instrument_type = str(_value(instrument, "instrument_type") or "").strip().lower()
    if instrument_type not in {"qdii", "daily_leveraged_product"}:
        return True
    frozen = _raw_product_evidence(instrument)
    if not isinstance(frozen, Mapping) or frozen.get("status") != "ready":
        return False
    components = frozen.get("components")
    if not isinstance(components, Mapping):
        return False
    reconstructed = {
        "schema_version": frozen.get("schema_version"),
        "market": frozen.get("market"),
        "symbol": frozen.get("symbol"),
        "instrument_type": frozen.get("instrument_type"),
        "evidence_cutoff": frozen.get("evidence_cutoff"),
        **dict(components),
    }
    validated = product_evidence_from_instrument(
        {**dict(instrument), "product_evidence": reconstructed}
        if isinstance(instrument, Mapping)
        else instrument,
        cutoff=cutoff,
    )
    return bool(
        isinstance(validated, Mapping)
        and validated.get("status") == "ready"
        and not validated.get("blockers")
        and validated.get("evidence_hash") == frozen.get("evidence_hash")
    )


def validate_prepared_product_evidence(
    instrument: Any,
    evidence: Any,
    *,
    cutoff: datetime,
) -> Optional[dict[str, Any]]:
    """Revalidate server-prepared normalized evidence before freezing it."""

    if not isinstance(evidence, Mapping):
        return None
    components = evidence.get("components")
    evidence_hash = str(evidence.get("evidence_hash") or "").strip().lower()
    if not isinstance(components, Mapping) or not _SHA256_RE.fullmatch(evidence_hash):
        return None

    reconstructed = {
        "schema_version": evidence.get("schema_version"),
        "market": evidence.get("market"),
        "symbol": evidence.get("symbol"),
        "instrument_type": evidence.get("instrument_type"),
        "evidence_cutoff": evidence.get("evidence_cutoff"),
        **dict(components),
    }
    identity = {
        field: _value(instrument, field)
        for field in (
            "market",
            "symbol",
            "instrument_type",
            "underlying_symbol",
            "underlying_market",
            "underlying_currency",
            "leverage_factor",
            "daily_reset",
            "verification_status",
        )
    }
    validated = product_evidence_from_instrument(
        {**identity, "product_evidence": reconstructed},
        cutoff=cutoff,
    )
    if not isinstance(validated, Mapping):
        return None
    if validated.get("evidence_hash") != evidence_hash:
        return None
    return dict(validated)


def product_evidence_for_account(
    instrument: Any,
    *,
    account_id: Any,
) -> Optional[dict[str, Any]]:
    """Select frozen product evidence without crossing account boundaries."""

    by_account = _value(instrument, "product_evidence_by_account")
    if isinstance(by_account, Mapping) and account_id is not None:
        selected = by_account.get(str(account_id))
        if isinstance(selected, Mapping):
            return dict(selected)
    direct = _value(instrument, "product_evidence")
    return dict(direct) if isinstance(direct, Mapping) else None


def _fallback(instrument: Any, *, instrument_type: str) -> dict[str, Any]:
    if instrument_type == "qdii":
        return {
            "instrument_type": "qdii",
            "status": "insufficient",
            "nav_iopv_available": False,
            "premium_discount_available": False,
            "underlying_fx_available": False,
            "spread_available": False,
            "tracking_available": False,
            "blockers": [
                "qdii_nav_iopv_missing",
                "qdii_premium_discount_missing",
                "qdii_underlying_fx_missing",
                "qdii_spread_missing",
                "qdii_tracking_evidence_missing",
            ],
        }
    official_terms_available = bool(
        _value(instrument, "verification_status") == "verified"
        and str(_value(instrument, "evidence_source") or "").strip()
        and _value(instrument, "evidence_as_of") is not None
    )
    underlying_identity_available = bool(
        _value(instrument, "verification_status") == "verified"
        and _value(instrument, "underlying_symbol")
        and _value(instrument, "underlying_market")
        and _value(instrument, "underlying_currency")
        and _positive(_value(instrument, "leverage_factor"))
        and _value(instrument, "daily_reset") is True
    )
    blockers = []
    if not official_terms_available:
        blockers.append("daily_reset_official_terms_missing")
    if not underlying_identity_available:
        blockers.append("daily_reset_underlying_identity_missing")
    blockers.extend(
        [
            "daily_reset_underlying_same_cutoff_missing",
            "daily_reset_intraday_leverage_missing",
            "daily_reset_path_decay_rebalance_missing",
            "daily_reset_liquidity_missing",
            "daily_reset_horizon_fit_missing",
        ]
    )
    underlying_market = str(_value(instrument, "underlying_market") or "").strip().lower()
    underlying_symbol = str(_value(instrument, "underlying_symbol") or "").strip().upper()
    return {
        "instrument_type": "daily_leveraged_product",
        "status": "insufficient",
        "official_terms_available": official_terms_available,
        "underlying_identity_available": underlying_identity_available,
        "underlying_identity": (
            f"{underlying_market}:{underlying_symbol}"
            if underlying_market and underlying_symbol
            else None
        ),
        "underlying_same_cutoff_available": False,
        "intraday_leverage_available": False,
        "path_decay_rebalance_available": False,
        "liquidity_available": False,
        "horizon_fit_evaluated": False,
        "daily_reset": _value(instrument, "daily_reset") is True,
        "leverage_factor": _value(instrument, "leverage_factor"),
        "blockers": blockers,
    }


def _raw_product_evidence(instrument: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(instrument, Mapping):
        direct = instrument.get("product_evidence")
        if isinstance(direct, Mapping):
            return direct
        metadata = instrument.get("metadata")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("product_evidence"), Mapping):
            return metadata["product_evidence"]
        metadata_json = instrument.get("metadata_json")
    else:
        metadata_json = getattr(instrument, "metadata_json", None)
    if not metadata_json:
        return None
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    product_evidence = metadata.get("product_evidence") if isinstance(metadata, Mapping) else None
    return product_evidence if isinstance(product_evidence, Mapping) else None


def _valid_component_envelope(
    component: Mapping[str, Any],
    *,
    cutoff: Optional[datetime],
) -> bool:
    if component.get("available") is not True or cutoff is None:
        return False
    as_of = _aware_datetime(component.get("as_of"))
    if as_of is None or as_of.astimezone(timezone.utc) > cutoff.astimezone(timezone.utc):
        return False
    if not str(component.get("source") or "").strip() or not str(
        component.get("source_version") or ""
    ).strip():
        return False
    source_hash = str(component.get("source_hash") or "").strip().lower()
    return bool(
        _SHA256_RE.fullmatch(source_hash)
        and source_hash == _hash({key: value for key, value in component.items() if key != "source_hash"})
    )


def _valid_nav_iopv(component: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    return _positive(component.get("nav")) or _positive(component.get("iopv"))


def _valid_fx(component: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    return bool(str(component.get("pair") or "").strip() and _positive(component.get("rate")))


def _valid_spread(component: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    return _non_negative(component.get("spread_bps"))


def _valid_official_terms(component: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    return bool(
        str(component.get("terms_url") or "").strip()
        and component.get("daily_reset") is True
        and _numbers_equal(component.get("leverage_factor"), identity.get("leverage_factor"))
    )


def _valid_underlying(component: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    return bool(
        component.get("completed_session") is True
        and str(component.get("market") or "").strip().lower() == identity.get("underlying_market")
        and str(component.get("symbol") or "").strip().upper() == identity.get("underlying_symbol")
        and str(component.get("currency") or "").strip().upper() == identity.get("underlying_currency")
    )


def _valid_intraday_leverage(component: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    if not _numbers_equal(component.get("leverage_factor"), identity.get("leverage_factor")):
        return False
    if not all(
        _finite(component.get(field))
        for field in ("product_return_pct", "underlying_return_pct", "observed_leverage")
    ):
        return False
    underlying_return = float(component["underlying_return_pct"])
    if math.isclose(underlying_return, 0.0, rel_tol=0.0, abs_tol=1e-12):
        return False
    observed = float(component["observed_leverage"])
    calculated = float(component["product_return_pct"]) / underlying_return
    return math.isclose(observed, calculated, rel_tol=0.0, abs_tol=0.05)


def _valid_path_rebalance(component: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    return bool(
        component.get("path_dependency_disclosed") is True
        and str(component.get("rebalance_frequency") or "").strip().lower() == "daily"
    )


def _valid_horizon_fit(component: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    return bool(
        component.get("evaluated") is True
        and isinstance(component.get("fits_holding_period"), bool)
    )


def _aware_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _non_negative(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _numbers_equal(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _value(source: Any, field: str) -> Any:
    return source.get(field) if isinstance(source, Mapping) else getattr(source, field, None)


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
