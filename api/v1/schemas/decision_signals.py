# -*- coding: utf-8 -*-
"""DecisionSignal API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from api.v1.schemas.market_phase import MarketPhaseValue
from src.schemas.decision_action import DecisionAction
from src.schemas.decision_profile import DecisionProfile


DecisionSignalSourceType = Literal["analysis", "agent", "alert", "market_review", "manual"]
DecisionSignalStatus = Literal["active", "expired", "invalidated", "closed", "archived"]
DecisionSignalPlanQuality = Literal["complete", "partial", "minimal", "unknown"]
DecisionSignalHorizon = Literal["intraday", "1d", "3d", "5d", "10d", "20d", "swing", "long"]
DecisionSignalMarket = Literal["cn", "hk", "us", "jp", "kr", "tw"]
DecisionSignalOutcomeStatus = Literal["completed", "unable"]
DecisionSignalOutcomeValue = Literal["hit", "miss", "neutral"]
DecisionSignalFeedbackValue = Literal["useful", "not_useful"]
DecisionSignalFeedbackSource = Literal["web", "api"]
DecisionSignalHumanDecision = Literal["accept", "veto", "modify", "no_action"]
DecisionSignalManualAction = Literal["buy", "add", "hold", "reduce", "sell", "no_action"]
DecisionSignalPositionAction = Literal["hold", "reduce", "exit"]
DecisionSignalIncrementalAction = Literal["add_in_batches", "wait", "no_add"]


class DecisionSignalCreateRequest(BaseModel):
    stock_code: str = Field(..., min_length=1, max_length=32)
    stock_name: Optional[str] = Field(None, json_schema_extra={"maxLength": 64})
    market: DecisionSignalMarket
    source_type: DecisionSignalSourceType
    source_agent: Optional[str] = Field(None, json_schema_extra={"maxLength": 64})
    source_report_id: Optional[int] = None
    trace_id: Optional[str] = Field(None, json_schema_extra={"maxLength": 64})
    decision_profile: DecisionProfile = Field(
        default=None,
        description="Optional decision profile. Omit to use server-side default/fallback; explicit null is rejected.",
    )
    market_phase: Optional[MarketPhaseValue] = None
    trigger_source: str = Field(..., min_length=1, json_schema_extra={"maxLength": 64})
    action: DecisionAction
    action_label: Optional[str] = Field(None, json_schema_extra={"maxLength": 32})
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    score: Optional[int] = Field(None, ge=0, le=100)
    horizon: Optional[DecisionSignalHorizon] = None
    entry_low: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    entry_high: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    target_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    invalidation: Optional[Any] = None
    watch_conditions: Optional[Any] = None
    reason: Optional[Any] = None
    risk_summary: Optional[Any] = None
    catalyst_summary: Optional[Any] = None
    evidence: Optional[Any] = None
    data_quality_summary: Optional[Any] = None
    plan_quality: Optional[DecisionSignalPlanQuality] = None
    status: Optional[DecisionSignalStatus] = None
    expires_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional metadata object. Omitted or null values are treated as absent.",
    )
    report_language: Optional[Literal["zh", "en", "ko"]] = None


class DecisionSignalReassessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_report_id: int = Field(..., gt=0)
    decision_profile: DecisionProfile
    persist: bool = False


class DecisionSignalWarning(BaseModel):
    code: str
    message: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class DecisionSignalGuardrailResult(BaseModel):
    raw_action: str
    final_action: str
    passed: bool
    violations: List[str] = Field(default_factory=list)
    adjustments: List[str] = Field(default_factory=list)
    adjusted: bool


class DecisionSignalPreview(BaseModel):
    action: str
    score: Optional[int] = None
    confidence: Optional[float] = None
    horizon: Optional[str] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    invalidation: Optional[str] = None
    reason: Optional[str] = None
    risk_summary: Optional[str] = None
    watch_conditions: Optional[str] = None
    metadata: Dict[str, Any]


class DecisionSignalStatusUpdateRequest(BaseModel):
    status: DecisionSignalStatus
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional replacement metadata. Omit to preserve the stored value; "
            "null clears it; an object replaces it while preserving the formal "
            "decision_profile identity."
        ),
    )


class DecisionSignalOutcomeRunRequest(BaseModel):
    signal_id: Optional[int] = Field(None, gt=0)
    horizons: Optional[List[DecisionSignalHorizon]] = None
    force: bool = False
    market: Optional[DecisionSignalMarket] = None
    stock_code: Optional[str] = Field(None, json_schema_extra={"maxLength": 32})
    action: Optional[DecisionAction] = None
    source_type: Optional[DecisionSignalSourceType] = None
    status: Optional[DecisionSignalStatus] = None
    limit: int = Field(100, ge=1, le=500)


class DecisionSignalOutcomeItem(BaseModel):
    id: int
    signal_id: int
    horizon: str
    engine_version: str
    eval_status: str
    outcome: Optional[str] = None
    direction_expected: Optional[str] = None
    direction_correct: Optional[bool] = None
    unable_reason: Optional[str] = None
    anchor_date: Optional[str] = None
    eval_window_days: Optional[int] = None
    start_price: Optional[float] = None
    end_close: Optional[float] = None
    max_high: Optional[float] = None
    min_low: Optional[float] = None
    stock_return_pct: Optional[float] = None
    action: Optional[str] = None
    market: Optional[str] = None
    market_phase: Optional[str] = None
    source_type: Optional[str] = None
    source_agent: Optional[str] = None
    plan_quality: Optional[str] = None
    data_quality_level: Optional[str] = None
    holding_state: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DecisionSignalOutcomeRunResponse(BaseModel):
    items: List[DecisionSignalOutcomeItem] = Field(default_factory=list)
    evaluated: int
    created: int
    updated: int
    skipped: int
    engine_version: str


class DecisionSignalOutcomeListResponse(BaseModel):
    items: List[DecisionSignalOutcomeItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class DecisionSignalOutcomeStatsBucket(BaseModel):
    dimension: str
    value: str
    total: int
    completed: int
    unable: int
    hit: int
    miss: int
    neutral: int
    hit_rate_pct: Optional[float] = None
    avg_stock_return_pct: Optional[float] = None
    unable_reasons: Dict[str, int] = Field(default_factory=dict)


class DecisionSignalProfileCalibrationBucket(BaseModel):
    dimensions: Dict[str, str] = Field(default_factory=dict)
    total: int
    completed: int
    unable: int
    hit: int
    miss: int
    neutral: int
    sample_sufficient: bool
    hit_rate_pct: Optional[float] = None
    avg_stock_return_pct: Optional[float] = None
    miss_rate_pct: Optional[float] = None
    unable_rate_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None


class DecisionSignalProfileCalibrationBreakdowns(BaseModel):
    decision_profile: List[DecisionSignalProfileCalibrationBucket] = Field(default_factory=list)
    decision_profile_action: List[DecisionSignalProfileCalibrationBucket] = Field(default_factory=list)
    decision_profile_horizon: List[DecisionSignalProfileCalibrationBucket] = Field(default_factory=list)
    decision_profile_market_phase: List[DecisionSignalProfileCalibrationBucket] = Field(default_factory=list)
    decision_profile_data_quality_level: List[DecisionSignalProfileCalibrationBucket] = Field(default_factory=list)
    profile_source: List[DecisionSignalProfileCalibrationBucket] = Field(default_factory=list)


class DecisionSignalProfileCalibration(BaseModel):
    minimum_completed_sample_size: int = Field(..., ge=1)
    breakdowns: DecisionSignalProfileCalibrationBreakdowns


class DecisionSignalOutcomeStatsResponse(BaseModel):
    engine_version: str
    horizons: Optional[List[str]] = None
    statuses: List[str] = Field(default_factory=list)
    total: int
    completed: int
    unable: int
    hit: int
    miss: int
    neutral: int
    hit_rate_pct: Optional[float] = None
    avg_stock_return_pct: Optional[float] = None
    unable_reasons: Dict[str, int] = Field(default_factory=dict)
    breakdowns: Dict[str, List[DecisionSignalOutcomeStatsBucket]] = Field(default_factory=dict)
    profile_calibration: DecisionSignalProfileCalibration


class DecisionSignalFeedbackRequest(BaseModel):
    feedback_value: DecisionSignalFeedbackValue
    reason_code: Optional[str] = Field(None, json_schema_extra={"maxLength": 64})
    note: Optional[str] = Field(None, json_schema_extra={"maxLength": 1000})
    source: DecisionSignalFeedbackSource = "api"


class DecisionSignalFeedbackItem(BaseModel):
    signal_id: int
    feedback_value: Optional[DecisionSignalFeedbackValue] = None
    reason_code: Optional[str] = None
    note: Optional[str] = None
    source: Optional[DecisionSignalFeedbackSource] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DecisionSignalShadowFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback_value: Optional[DecisionSignalFeedbackValue] = None
    frozen_snapshot_hash: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F]{64}$")
    evidence_sources: Optional[List[str]] = Field(None, min_length=1, max_length=50)
    human_decision: DecisionSignalHumanDecision
    human_position_action: Optional[DecisionSignalPositionAction] = None
    human_incremental_action: Optional[DecisionSignalIncrementalAction] = None
    actual_position_action: Optional[DecisionSignalPositionAction] = None
    actual_incremental_action: Optional[DecisionSignalIncrementalAction] = None
    decision_reason_code: Optional[str] = Field(None, max_length=64)
    note: Optional[str] = Field(None, max_length=1000)
    actual_manual_action: Optional[DecisionSignalManualAction] = None
    correction_minutes: Optional[int] = Field(None, ge=0, le=1440)
    latency_ms: Optional[int] = Field(None, ge=0)
    model_tokens: Optional[int] = Field(None, ge=0)
    source: DecisionSignalFeedbackSource = "api"


class DecisionSignalShadowFeedbackItem(BaseModel):
    signal_id: int
    feedback_value: Optional[DecisionSignalFeedbackValue] = None
    source: Optional[DecisionSignalFeedbackSource] = None
    frozen_snapshot_hash: Optional[str] = None
    evidence_sources: List[str] = Field(default_factory=list)
    gated_recommendation: Optional[str] = None
    human_decision: Optional[DecisionSignalHumanDecision] = None
    human_position_action: Optional[DecisionSignalPositionAction] = None
    human_incremental_action: Optional[DecisionSignalIncrementalAction] = None
    actual_position_action: Optional[DecisionSignalPositionAction] = None
    actual_incremental_action: Optional[DecisionSignalIncrementalAction] = None
    decision_reason_code: Optional[str] = None
    note: Optional[str] = None
    actual_manual_action: Optional[DecisionSignalManualAction] = None
    correction_minutes: Optional[int] = None
    recommendation_created_at: Optional[str] = None
    evidence_expires_at: Optional[str] = None
    latency_ms: Optional[int] = None
    model_tokens: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DecisionQualityOutcomeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: Optional[int] = Field(None, gt=0)
    horizons: Optional[List[Literal["5d", "20d", "60d"]]] = None


class DecisionQualityOutcomeRunResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    evaluated: int
    engine_version: str


class DecisionQualityDetailResponse(BaseModel):
    context: Dict[str, Any]
    outcomes: List[Dict[str, Any]] = Field(default_factory=list)
    attributions: List[Dict[str, Any]] = Field(default_factory=list)


class DecisionQualityStatsResponse(BaseModel):
    sample_size: int
    horizon: Literal["5d", "20d", "60d"]
    empty_state: bool
    performance: Optional[Dict[str, Any]] = None
    instrument_concentration: List[Dict[str, Any]] = Field(default_factory=list)
    engine_version: str


class DecisionQualityAttributionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "fact_error",
        "evidence_error",
        "thesis_error",
        "valuation_error",
        "timing_error",
        "risk_error",
        "execution_error",
        "unattributed",
    ]
    status: Literal["proposed", "confirmed", "rejected"]
    summary: str = Field(..., min_length=1, max_length=1000)
    evidence: List[Any] = Field(default_factory=list, max_length=100)
    counterexamples: List[Any] = Field(default_factory=list, max_length=100)
    user_note: Optional[str] = Field(None, max_length=1000)


class DecisionQualityAttributionItem(BaseModel):
    signal_id: int
    horizon: Literal["5d", "20d", "60d"]
    engine_version: str
    category: str
    status: str
    summary: str
    evidence: List[Any] = Field(default_factory=list)
    counterexamples: List[Any] = Field(default_factory=list)
    user_note: Optional[str] = None


class DecisionQualityWeeklyReviewResponse(BaseModel):
    window_start: str
    window_end: str
    material_decision_count: int
    decisions: List[Dict[str, Any]] = Field(default_factory=list)
    ai_human_disagreements: List[Dict[str, Any]] = Field(default_factory=list)
    confirmed_attribution_counts: Dict[str, int] = Field(default_factory=dict)
    triggered_conditions: List[Dict[str, Any]] = Field(default_factory=list)
    expired_conditions: List[Dict[str, Any]] = Field(default_factory=list)
    candidate_patterns: List[Dict[str, Any]] = Field(default_factory=list)
    automatic_rules_activated: bool = False


class DecisionSignalItem(BaseModel):
    id: int
    stock_code: str
    stock_name: Optional[str] = None
    market: str
    source_type: str
    source_agent: Optional[str] = None
    source_report_id: Optional[int] = None
    trace_id: Optional[str] = None
    decision_profile: Optional[DecisionProfile] = None
    market_phase: Optional[str] = None
    trigger_source: str
    action: str
    action_label: Optional[str] = None
    confidence: Optional[float] = None
    score: Optional[int] = None
    horizon: Optional[str] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    invalidation: Optional[str] = None
    watch_conditions: Optional[str] = None
    reason: Optional[str] = None
    risk_summary: Optional[str] = None
    catalyst_summary: Optional[str] = None
    evidence: Optional[Any] = None
    data_quality_summary: Optional[Any] = None
    plan_quality: str
    status: str
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: Optional[Any] = None


class DecisionSignalMutationResponse(BaseModel):
    item: DecisionSignalItem
    created: bool


class DecisionSignalReassessResponse(BaseModel):
    preview: Optional[DecisionSignalPreview] = None
    item: Optional[DecisionSignalItem] = None
    created: bool = False
    persist_status: Optional[Literal["created", "existing", "refreshed"]] = None
    warnings: List[DecisionSignalWarning] = Field(default_factory=list)
    blocked_reason: Optional[str] = None


class DecisionSignalReassessErrorResponse(BaseModel):
    error: Literal[
        "unsupported_report_type",
        "unsupported_report_snapshot",
        "guardrail_blocked",
    ]
    message: str
    blocked_reason: Optional[str] = None
    warnings: List[DecisionSignalWarning] = Field(default_factory=list)


class DecisionSignalListResponse(BaseModel):
    items: List[DecisionSignalItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int
