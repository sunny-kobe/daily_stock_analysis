# -*- coding: utf-8 -*-
"""Portfolio API schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


PortfolioInstrumentType = Literal["equity", "etf", "qdii", "adr_ads", "daily_leveraged_product", "unknown"]
PortfolioVerificationStatus = Literal["verified", "provisional", "missing"]


class PortfolioInstrumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=32)
    market: Literal["cn", "hk", "us", "jp", "kr", "tw"]
    quote_currency: str = Field(..., min_length=3, max_length=8)
    instrument_type: PortfolioInstrumentType
    underlying_symbol: Optional[str] = Field(None, max_length=32)
    underlying_market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    underlying_currency: Optional[str] = Field(None, max_length=8)
    leverage_factor: Optional[float] = Field(None, gt=0)
    daily_reset: bool = False
    conversion_ratio: Optional[float] = Field(None, gt=0)
    trade_lot_size: float = Field(..., gt=0)
    requires_premium_check: bool = False
    verification_status: PortfolioVerificationStatus = "missing"
    evidence_source: Optional[str] = Field(None, max_length=512)
    evidence_as_of: Optional[datetime] = Field(
        None,
        description="Evidence timestamp with an explicit timezone offset; stored as UTC.",
    )
    metadata: Optional[Dict[str, Any]] = None


class PortfolioInstrumentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_currency: Optional[str] = Field(None, min_length=3, max_length=8)
    instrument_type: Optional[PortfolioInstrumentType] = None
    underlying_symbol: Optional[str] = Field(None, max_length=32)
    underlying_market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    underlying_currency: Optional[str] = Field(None, max_length=8)
    leverage_factor: Optional[float] = Field(None, gt=0)
    daily_reset: Optional[bool] = None
    conversion_ratio: Optional[float] = Field(None, gt=0)
    trade_lot_size: Optional[float] = Field(None, gt=0)
    requires_premium_check: Optional[bool] = None
    verification_status: Optional[PortfolioVerificationStatus] = None
    evidence_source: Optional[str] = Field(None, max_length=512)
    evidence_as_of: Optional[datetime] = Field(
        None,
        description="Evidence timestamp with an explicit timezone offset; stored as UTC.",
    )
    metadata: Optional[Dict[str, Any]] = None


class PortfolioInstrumentItem(BaseModel):
    id: int
    symbol: str
    market: str
    quote_currency: str
    instrument_type: str
    underlying_symbol: Optional[str] = None
    underlying_market: Optional[str] = None
    underlying_currency: Optional[str] = None
    leverage_factor: Optional[float] = None
    daily_reset: bool
    conversion_ratio: Optional[float] = None
    trade_lot_size: float
    requires_premium_check: bool
    verification_status: str
    evidence_source: Optional[str] = None
    evidence_as_of: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PortfolioInstrumentListResponse(BaseModel):
    items: List[PortfolioInstrumentItem] = Field(default_factory=list)


class PortfolioRiskPolicyUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_cash_buffer_pct: Optional[float] = Field(None, ge=0, le=100)
    max_single_position_pct: Optional[float] = Field(None, ge=0, le=100)
    max_sector_pct: Optional[float] = Field(None, ge=0, le=100)
    max_high_risk_product_pct: Optional[float] = Field(None, ge=0, le=100)
    max_portfolio_drawdown_pct: Optional[float] = Field(None, ge=0, le=100)


class PortfolioRiskPolicyItem(BaseModel):
    id: int
    min_cash_buffer_pct: float
    max_single_position_pct: float
    max_sector_pct: float
    max_high_risk_product_pct: float
    max_portfolio_drawdown_pct: float
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PortfolioRiskPolicyResponse(BaseModel):
    policy: Optional[PortfolioRiskPolicyItem] = None


class PortfolioAccountCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    broker: Optional[str] = Field(None, max_length=64)
    market: Literal["cn", "hk", "us", "jp", "kr", "tw"] = "cn"
    base_currency: str = Field("CNY", min_length=3, max_length=8)
    owner_id: Optional[str] = Field(None, max_length=64)


class PortfolioAccountUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    broker: Optional[str] = Field(None, max_length=64)
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    base_currency: Optional[str] = Field(None, min_length=3, max_length=8)
    owner_id: Optional[str] = Field(None, max_length=64)
    is_active: Optional[bool] = None


class PortfolioAccountItem(BaseModel):
    id: int
    owner_id: Optional[str] = None
    name: str
    broker: Optional[str] = None
    market: str
    base_currency: str
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PortfolioAccountListResponse(BaseModel):
    accounts: List[PortfolioAccountItem] = Field(default_factory=list)


class PortfolioTradeCreateRequest(BaseModel):
    account_id: int
    symbol: str = Field(..., min_length=1, max_length=16)
    trade_date: date
    side: Literal["buy", "sell"]
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    fee: float = Field(0.0, ge=0)
    tax: float = Field(0.0, ge=0)
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    trade_uid: Optional[str] = Field(None, max_length=128)
    note: Optional[str] = Field(None, max_length=255)


class PortfolioCashLedgerCreateRequest(BaseModel):
    account_id: int
    event_date: date
    direction: Literal["in", "out"]
    amount: float = Field(..., gt=0)
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    note: Optional[str] = Field(None, max_length=255)


class PortfolioCorporateActionCreateRequest(BaseModel):
    account_id: int
    symbol: str = Field(..., min_length=1, max_length=16)
    effective_date: date
    action_type: Literal["cash_dividend", "split_adjustment"]
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    cash_dividend_per_share: Optional[float] = Field(None, ge=0)
    split_ratio: Optional[float] = Field(None, gt=0)
    note: Optional[str] = Field(None, max_length=255)


class PortfolioEventCreatedResponse(BaseModel):
    id: int


class PortfolioDeleteResponse(BaseModel):
    deleted: int


class PortfolioTradeListItem(BaseModel):
    id: int
    account_id: int
    trade_uid: Optional[str] = None
    symbol: str
    market: str
    currency: str
    trade_date: str
    side: str
    quantity: float
    price: float
    fee: float
    tax: float
    note: Optional[str] = None
    created_at: Optional[str] = None


class PortfolioTradeListResponse(BaseModel):
    items: List[PortfolioTradeListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class PortfolioCashLedgerListItem(BaseModel):
    id: int
    account_id: int
    event_date: str
    direction: str
    amount: float
    currency: str
    note: Optional[str] = None
    created_at: Optional[str] = None


class PortfolioCashLedgerListResponse(BaseModel):
    items: List[PortfolioCashLedgerListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class PortfolioCorporateActionListItem(BaseModel):
    id: int
    account_id: int
    symbol: str
    market: str
    currency: str
    effective_date: str
    action_type: str
    cash_dividend_per_share: Optional[float] = None
    split_ratio: Optional[float] = None
    note: Optional[str] = None
    created_at: Optional[str] = None


class PortfolioCorporateActionListResponse(BaseModel):
    items: List[PortfolioCorporateActionListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class PortfolioPositionItem(BaseModel):
    symbol: str
    market: str
    currency: str
    quantity: float
    avg_cost: float
    total_cost: float
    last_price: float
    market_value_base: float
    unrealized_pnl_base: float
    unrealized_pnl_pct: Optional[float] = None
    valuation_currency: str
    price_source: str = "unknown"
    price_provider: Optional[str] = None
    price_date: Optional[str] = None
    price_stale: bool = False
    price_available: bool = True
    data_quality: str = "ok"
    limitations: List[str] = Field(default_factory=list)


class PortfolioPositionAnalysisRequest(BaseModel):
    account_id: Optional[int] = Field(None, description="Optional account id; required when a symbol is held in multiple accounts")
    analysis_phase: Literal["auto", "premarket", "intraday", "postmarket"] = "auto"
    force: bool = Field(False, description="Force refresh analysis inputs without bypassing duplicate in-flight tasks")
    research_snapshot_hash: Optional[str] = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Expected hash of the preflight portfolio research snapshot",
    )
    research_cutoff: Optional[datetime] = Field(
        None,
        description="Timezone-aware cutoff of the preflight portfolio research snapshot",
    )

    @model_validator(mode="after")
    def validate_research_snapshot_binding(self) -> "PortfolioPositionAnalysisRequest":
        if (self.research_snapshot_hash is None) != (self.research_cutoff is None):
            raise PydanticCustomError(
                "research_snapshot_binding_incomplete",
                "research_snapshot_hash and research_cutoff must be provided together",
            )
        if self.research_cutoff is not None:
            if self.research_cutoff.tzinfo is None or self.research_cutoff.utcoffset() is None:
                raise PydanticCustomError(
                    "research_cutoff_timezone_missing",
                    "research_cutoff must include an explicit timezone offset",
                )
        return self


class PortfolioAccountSnapshot(BaseModel):
    account_id: int
    account_name: str
    owner_id: Optional[str] = None
    broker: Optional[str] = None
    market: str
    base_currency: str
    as_of: str
    cost_method: str
    total_cash: float
    total_market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fee_total: float
    tax_total: float
    fx_stale: bool
    data_quality: str = "ok"
    limitations: List[str] = Field(default_factory=list)
    positions: List[PortfolioPositionItem] = Field(default_factory=list)


class PortfolioSnapshotResponse(BaseModel):
    as_of: str
    cost_method: str
    currency: str
    account_count: int
    total_cash: float
    total_market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fee_total: float
    tax_total: float
    fx_stale: bool
    data_quality: str = "ok"
    limitations: List[str] = Field(default_factory=list)
    accounts: List[PortfolioAccountSnapshot] = Field(default_factory=list)


class PortfolioAnalysisRuntimeProof(BaseModel):
    architecture: Literal["single", "multi"]
    automatic_multi_agent: bool


class PortfolioResearchSnapshotResponse(BaseModel):
    schema_version: str
    cutoff: str
    timezone: str
    cost_method: str
    analysis_runtime: PortfolioAnalysisRuntimeProof
    universe_hash: str
    snapshot_hash: str
    accounts: List[Dict[str, Any]] = Field(default_factory=list)
    positions: List[Dict[str, Any]] = Field(default_factory=list)
    instruments: List[Dict[str, Any]] = Field(default_factory=list)
    benchmarks: List[Dict[str, Any]] = Field(default_factory=list)
    risk_policy: Optional[Dict[str, Any]] = None
    risk_budget: Optional[Dict[str, Any]] = None
    hard_blockers: List[Dict[str, Any]] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    completeness: str


class PortfolioResearchBaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_snapshot_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    research_cutoff: datetime

    @model_validator(mode="after")
    def validate_research_cutoff(self) -> "PortfolioResearchBaselineRequest":
        if self.research_cutoff.tzinfo is None or self.research_cutoff.utcoffset() is None:
            raise PydanticCustomError(
                "research_cutoff_timezone_missing",
                "research_cutoff must include an explicit timezone offset",
            )
        return self


class PortfolioResearchBaselineItem(BaseModel):
    account_id: int
    account_name: Optional[str] = None
    market: str
    symbol: str
    name: Optional[str] = None
    display_label: str
    selection_key: str
    currency: Optional[str] = None
    quantity: Optional[float] = None
    instrument_type: str = "unknown"
    quote: Dict[str, Any] = Field(default_factory=dict)
    history: Dict[str, Any] = Field(default_factory=dict)
    trend: Optional[Dict[str, Any]] = None
    current_signal_id: Optional[int] = None
    position_action: Literal["hold", "reduce", "exit"]
    incremental_action: Literal["add_in_batches", "wait", "no_add"]
    core_reason: Optional[str] = None
    hard_blockers: List[str] = Field(default_factory=list)
    risk_flags: List[Dict[str, Any]] = Field(default_factory=list)
    exception_reasons: List[str] = Field(default_factory=list)
    evidence_status: str
    research_level: Literal["baseline"] = "baseline"
    detail_recommended: bool = False
    sizing_allowed: bool = False


class PortfolioResearchBaselineCandidate(BaseModel):
    selection_key: str
    display_label: str
    market: str
    symbol: str
    account_ids: List[int] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    priority: int
    recommended: bool


class PortfolioResearchBaselineResponse(BaseModel):
    schema_version: Literal["portfolio-research-baseline-v1"]
    snapshot_hash: str
    cutoff: str
    market_data_cutoff: str
    ledger_position_count: int
    baseline_row_count: int
    coverage_reconciled: bool
    portfolio_risk_flags: List[Dict[str, Any]] = Field(default_factory=list)
    items: List[PortfolioResearchBaselineItem] = Field(default_factory=list)
    suggested_deep_analysis: List[PortfolioResearchBaselineCandidate] = Field(default_factory=list)
    deep_analysis_started: bool = False


class PortfolioImportTradeItem(BaseModel):
    trade_date: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    fee: float
    tax: float
    trade_uid: Optional[str] = None
    dedup_hash: str
    currency: Optional[str] = None


class PortfolioImportParseResponse(BaseModel):
    broker: str
    record_count: int
    skipped_count: int
    error_count: int
    records: List[PortfolioImportTradeItem] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class PortfolioImportCommitResponse(BaseModel):
    account_id: int
    record_count: int
    inserted_count: int
    duplicate_count: int
    failed_count: int
    dry_run: bool
    errors: List[str] = Field(default_factory=list)


class PortfolioImportBrokerItem(BaseModel):
    broker: str
    aliases: List[str] = Field(default_factory=list)
    display_name: Optional[str] = None


class PortfolioImportBrokerListResponse(BaseModel):
    brokers: List[PortfolioImportBrokerItem] = Field(default_factory=list)


class PortfolioFxRefreshResponse(BaseModel):
    as_of: str
    account_count: int
    refresh_enabled: bool
    disabled_reason: Optional[str] = None
    pair_count: int
    updated_count: int
    stale_count: int
    error_count: int


class PortfolioDecisionSignalRiskItem(BaseModel):
    account_id: Optional[int] = None
    symbol: str
    market: str
    signal: Dict[str, Any] = Field(default_factory=dict)


class PortfolioDecisionSignalRiskBlock(BaseModel):
    available: bool = True
    total: int = 0
    actions: Dict[str, int] = Field(default_factory=dict)
    items: List[PortfolioDecisionSignalRiskItem] = Field(default_factory=list)


class PortfolioRiskResponse(BaseModel):
    as_of: str
    account_id: Optional[int] = None
    cost_method: str
    currency: str
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    concentration: Dict[str, Any] = Field(default_factory=dict)
    sector_concentration: Dict[str, Any] = Field(default_factory=dict)
    drawdown: Dict[str, Any] = Field(default_factory=dict)
    stop_loss: Dict[str, Any] = Field(default_factory=dict)
    decision_signal_risk: PortfolioDecisionSignalRiskBlock = Field(default_factory=PortfolioDecisionSignalRiskBlock)
