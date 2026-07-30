from src.services.portfolio_research_routing import PortfolioResearchRoutingService


def _snapshot(instrument_type="equity", blockers=None):
    return {
        "snapshot_hash": "frozen-hash",
        "cutoff": "2026-07-22T09:00:00",
        "instruments": [{
            "symbol": "AAPL",
            "market": "us",
            "instrument_type": instrument_type,
            "verification_status": "verified",
        }],
        "hard_blockers": blockers or [],
    }


def test_routine_holding_stops_after_dsa() -> None:
    result = PortfolioResearchRoutingService().route(
        research_snapshot=_snapshot(), symbol="AAPL", market="us",
        question_type="routine", question="是否需要调整？",
    )

    assert result["status"] == "not_required"
    assert result["capability"] is None
    assert result["frozen_input_hash"] == "frozen-hash"


def test_each_exception_selects_at_most_one_specific_capability() -> None:
    service = PortfolioResearchRoutingService()
    cases = [
        ("timing", "deterministic_timing"),
        ("product_terms", "vibe_product_evidence"),
        ("thesis", "tradingagents_thesis"),
        ("high_impact_risk", "tradingagents_risk_review"),
    ]
    for question_type, capability in cases:
        result = service.route(
            research_snapshot=_snapshot("qdii" if question_type == "product_terms" else "equity"),
            symbol="AAPL", market="us", question_type=question_type,
            question=f"exact question for {question_type}",
        )
        assert result["status"] == "offered_pending_confirmation"
        assert result["capability"] == capability
        assert isinstance(result["capability"], str)


def test_required_control_plane_evidence_blocks_worker_offer() -> None:
    result = PortfolioResearchRoutingService().route(
        research_snapshot=_snapshot(blockers=[{
            "code": "instrument_identity_unverified", "scope": "instrument",
            "symbol": "AAPL", "market": "us",
        }]),
        symbol="AAPL", market="us", question_type="thesis",
        question="公司逻辑是否变化？",
    )

    assert result["status"] == "blocked"
    assert result["capability"] is None
    assert result["blockers"] == ["instrument_identity_unverified"]
