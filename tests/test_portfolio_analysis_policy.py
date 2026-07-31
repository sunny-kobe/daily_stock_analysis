from __future__ import annotations

from types import SimpleNamespace

from src.services.portfolio_analysis_policy import resolve_portfolio_analysis_policy


class _InstrumentRepo:
    def __init__(self, row):
        self.row = row

    def get_instrument(self, *, symbol: str, market: str):
        if self.row is None:
            return None
        if (self.row.symbol, self.row.market) != (symbol, market):
            return None
        return self.row


def _instrument(instrument_type: str, **overrides):
    fields = {
        "symbol": "TEST",
        "market": "us",
        "quote_currency": "USD",
        "instrument_type": instrument_type,
        "underlying_symbol": None,
        "underlying_market": None,
        "underlying_currency": None,
        "leverage_factor": None,
        "daily_reset": False,
        "conversion_ratio": None,
        "trade_lot_size": 1.0,
        "requires_premium_check": False,
        "verification_status": "verified",
        "evidence_source": "unit-test",
        "evidence_as_of": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_registry_drives_equity_etf_qdii_adr_and_leveraged_profiles() -> None:
    cases = [
        (_instrument("equity"), "equity_standard", []),
        (_instrument("etf"), "etf_structure", []),
        (
            _instrument("qdii", requires_premium_check=True),
            "qdii_premium",
            ["nav_premium"],
        ),
        (
            _instrument(
                "adr_ads",
                underlying_symbol="2330.TW",
                underlying_market="tw",
                underlying_currency="TWD",
                conversion_ratio=0.2,
                requires_premium_check=True,
            ),
            "adr_parity",
            ["adr_parity"],
        ),
        (
            _instrument(
                "daily_leveraged_product",
                underlying_symbol="000660.KS",
                underlying_market="kr",
                underlying_currency="KRW",
                leverage_factor=2.0,
                daily_reset=True,
                trade_lot_size=100,
            ),
            "leveraged_product_precision",
            ["underlying", "daily_reset", "lot_size"],
        ),
    ]

    for row, expected_profile, expected_checks in cases:
        policy = resolve_portfolio_analysis_policy(
            row.symbol,
            market=row.market,
            repo=_InstrumentRepo(row),
        )
        assert policy["profile"] == expected_profile
        assert policy["instrument_type"] == row.instrument_type
        assert policy["required_checks"] == expected_checks
        assert policy["actionable_identity"] is True


def test_hk07709_compatibility_fields_are_backed_by_verified_registry() -> None:
    row = _instrument(
        "daily_leveraged_product",
        symbol="HK07709",
        market="hk",
        quote_currency="HKD",
        underlying_symbol="000660.KS",
        underlying_market="kr",
        underlying_currency="KRW",
        leverage_factor=2.0,
        daily_reset=True,
        trade_lot_size=100.0,
    )

    policy = resolve_portfolio_analysis_policy(
        "hk07709",
        market="HK",
        repo=_InstrumentRepo(row),
    )

    assert policy["underlying_code"] == "000660.KS"
    assert policy["trade_lot_size"] == 100.0
    assert policy["precision_mode"] is True
    assert policy["skills"] == [
        "leveraged_product_risk",
        "event_driven",
        "expectation_repricing",
        "bull_trend",
    ]


def test_missing_or_provisional_identity_never_gets_executable_product_policy() -> None:
    assert resolve_portfolio_analysis_policy(
        "HK07709",
        market="hk",
        repo=_InstrumentRepo(None),
    ) == {}

    row = _instrument(
        "daily_leveraged_product",
        verification_status="provisional",
        underlying_symbol="000660.KS",
        underlying_market="kr",
        underlying_currency="KRW",
        leverage_factor=2,
        daily_reset=True,
    )
    policy = resolve_portfolio_analysis_policy(
        row.symbol,
        market=row.market,
        repo=_InstrumentRepo(row),
    )

    assert policy["actionable_identity"] is False
    assert policy["skills"] == []
    assert policy["blockers"] == ["instrument_identity_unverified"]
