# -*- coding: utf-8 -*-
"""Human-governed strategy lifecycle without runtime activation side effects."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.repositories.portfolio_strategy_validation_repo import (
    PortfolioStrategyValidationRepository,
)


_STRATEGY_TRANSITIONS = {
    "draft": {"shadow"},
    "shadow": {"champion", "retired"},
    "champion": {"retired"},
    "retired": set(),
}
_RULE_TRANSITIONS = {
    "observed": {"proposed"},
    "proposed": {"approved", "rejected"},
    "approved": set(),
    "rejected": set(),
}
_EVIDENCE_FIELDS = {
    "maturity_decision",
    "historical_oos",
    "prospective_shadow",
    "hard_gate_failures",
    "sample_concentration",
    "cost_delta_pct",
    "drawdown_delta_pct",
    "mature_horizons",
}


class PortfolioStrategyVersionService:
    def __init__(
        self,
        repo: PortfolioStrategyValidationRepository | None = None,
        db_manager: Any = None,
    ):
        self.repo = repo or PortfolioStrategyValidationRepository(db_manager)

    def register_strategy(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        return self._serialize_strategy(self.repo.create_strategy_version(manifest))

    def transition_strategy(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        target_status: str,
        human_confirmed: bool,
        reason: str,
        approved_by: str | None = None,
        automatic: bool = False,
    ) -> dict[str, Any]:
        if automatic:
            raise ValueError("automatic_promotion_forbidden")
        if human_confirmed is not True:
            raise ValueError("human_confirmation_required")
        if not str(reason or "").strip():
            raise ValueError("governance_reason_required")
        row = self.repo.get_strategy_version(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
        )
        if row is None:
            raise ValueError("strategy_version_not_found")
        if target_status not in _STRATEGY_TRANSITIONS.get(row.status, set()):
            raise ValueError(f"invalid_strategy_transition:{row.status}->{target_status}")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        updated = self.repo.update_strategy_status(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            status=target_status,
            reason=reason.strip(),
            approved_by=approved_by if target_status == "champion" else None,
            approved_at=now if target_status == "champion" else None,
        )
        return self._serialize_strategy(updated)

    def transition_rule_candidate(
        self,
        *,
        candidate_id: str,
        target_status: str,
        human_confirmed: bool,
        reason: str,
        approved_by: str | None = None,
        automatic: bool = False,
    ) -> dict[str, Any]:
        if automatic:
            raise ValueError("automatic_promotion_forbidden")
        if human_confirmed is not True:
            raise ValueError("human_confirmation_required")
        row = self.repo.get_rule_candidate(candidate_id=candidate_id)
        if row is None:
            raise ValueError("rule_candidate_not_found")
        if target_status not in _RULE_TRANSITIONS.get(row.status, set()):
            raise ValueError(f"invalid_rule_transition:{row.status}->{target_status}")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        updated = self.repo.update_rule_status(
            candidate_id=candidate_id,
            status=target_status,
            reason=reason.strip(),
            approved_by=approved_by if target_status == "approved" else None,
            approved_at=now if target_status == "approved" else None,
        )
        return {
            "candidate_id": updated.candidate_id,
            "status": updated.status,
            "rule_hash": updated.rule_hash,
            "governance_reason": updated.governance_reason,
        }

    def review_strategy(
        self,
        *,
        decision: str,
        strategy_id: str,
        strategy_version: str,
        evidence_summary: Mapping[str, Any],
        reason: str,
        rollback_strategy_version: str,
        approved_by: str,
        human_confirmed: bool,
    ) -> dict[str, Any]:
        self._validate_review_request(
            evidence_summary=evidence_summary,
            reason=reason,
            rollback_strategy_version=rollback_strategy_version,
            approved_by=approved_by,
            human_confirmed=human_confirmed,
        )
        if decision not in {"approve", "reject", "retire"}:
            raise ValueError("invalid_review_decision")
        target = self.repo.get_strategy_version(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
        )
        rollback = self.repo.get_strategy_version(
            strategy_id=strategy_id,
            strategy_version=rollback_strategy_version,
        )
        if target is None or rollback is None:
            raise ValueError("strategy_version_not_found")
        if decision == "approve":
            if evidence_summary.get("maturity_decision") != "ELIGIBLE_FOR_HUMAN_REVIEW":
                raise ValueError("strategy_not_eligible_for_review")
            if evidence_summary.get("hard_gate_failures"):
                raise ValueError("hard_gate_failures_present")
            if "20d" not in evidence_summary.get("mature_horizons", []):
                raise ValueError("mature_20d_evidence_required")
            transitioned = self.transition_strategy(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                target_status="champion",
                human_confirmed=True,
                reason=reason,
                approved_by=approved_by,
            )
        else:
            transitioned = self.transition_strategy(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                target_status="retired",
                human_confirmed=True,
                reason=reason,
                approved_by=approved_by,
            )
        event_id = self._governance_event_id(
            decision=decision,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            rollback_strategy_version=rollback_strategy_version,
            evidence_summary=evidence_summary,
            reason=reason,
            approved_by=approved_by,
        )
        self.repo.append_governance_event(
            event_id=event_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            decision=decision,
            rollback_strategy_version=rollback_strategy_version,
            evidence_summary=evidence_summary,
            reason=reason,
            approved_by=approved_by,
        )
        if decision == "approve":
            self.repo.set_selected_strategy(
                strategy_id=strategy_id,
                selected_strategy_version=strategy_version,
                rollback_strategy_version=rollback_strategy_version,
                governance_event_id=event_id,
            )
        return {
            **transitioned,
            "review_decision": decision,
            "rollback_strategy_version": rollback_strategy_version,
            "mature_horizons": list(evidence_summary["mature_horizons"]),
            "long_term_improvement_status": (
                "MATURE" if "60d" in evidence_summary["mature_horizons"] else "PROVISIONAL"
            ),
            "runtime_activated": False,
        }

    def rollback_strategy(
        self,
        *,
        strategy_id: str,
        current_strategy_version: str,
        rollback_strategy_version: str,
        evidence_summary: Mapping[str, Any],
        reason: str,
        approved_by: str,
        human_confirmed: bool,
    ) -> dict[str, Any]:
        self._validate_review_request(
            evidence_summary=evidence_summary,
            reason=reason,
            rollback_strategy_version=rollback_strategy_version,
            approved_by=approved_by,
            human_confirmed=human_confirmed,
        )
        current = self.repo.get_strategy_version(
            strategy_id=strategy_id,
            strategy_version=current_strategy_version,
        )
        rollback = self.repo.get_strategy_version(
            strategy_id=strategy_id,
            strategy_version=rollback_strategy_version,
        )
        if current is None or rollback is None:
            raise ValueError("strategy_version_not_found")
        event_id = self._governance_event_id(
            decision="rollback",
            strategy_id=strategy_id,
            strategy_version=current_strategy_version,
            rollback_strategy_version=rollback_strategy_version,
            evidence_summary=evidence_summary,
            reason=reason,
            approved_by=approved_by,
        )
        self.repo.append_governance_event(
            event_id=event_id,
            strategy_id=strategy_id,
            strategy_version=current_strategy_version,
            decision="rollback",
            rollback_strategy_version=rollback_strategy_version,
            evidence_summary=evidence_summary,
            reason=reason,
            approved_by=approved_by,
        )
        selected = self.repo.set_selected_strategy(
            strategy_id=strategy_id,
            selected_strategy_version=rollback_strategy_version,
            rollback_strategy_version=current_strategy_version,
            governance_event_id=event_id,
        )
        return {
            "strategy_id": strategy_id,
            "selected_strategy_version": selected.selected_strategy_version,
            "rollback_strategy_version": selected.rollback_strategy_version,
            "strategy_version_count": self.repo.count_strategy_versions(strategy_id=strategy_id),
            "governance_event_count": self.repo.count_governance_events(strategy_id=strategy_id),
            "runtime_activated": False,
        }

    def get_review_summary(
        self,
        *,
        strategy_id: str,
        protocol_id: str | None = None,
    ) -> dict[str, Any]:
        from src.services.portfolio_shadow_validation_service import (
            PortfolioShadowValidationService,
        )

        weekly = PortfolioShadowValidationService(repo=self.repo).weekly_review(
            protocol_id=protocol_id
        )
        versions = self.repo.list_strategy_versions(strategy_id=strategy_id)
        selected = self.repo.get_selected_strategy(strategy_id=strategy_id)
        latest_event = self.repo.get_latest_governance_event(strategy_id=strategy_id)
        evidence = json.loads(latest_event.evidence_json) if latest_event is not None else None

        champion_version = selected.selected_strategy_version if selected is not None else None
        if champion_version is None:
            champion_row = next((row for row in reversed(versions) if row.status == "champion"), None)
            champion_version = champion_row.strategy_version if champion_row is not None else None
        challenger_version = None
        if weekly["comparisons"]:
            challenger_version = weekly["comparisons"][-1]["challenger_decision"][
                "strategy_version"
            ]
            if champion_version is None:
                champion_version = weekly["comparisons"][-1]["champion_decision"][
                    "strategy_version"
                ]

        unable_reasons: list[str] = []
        if evidence is None:
            unable_reasons.append("validation_evidence_not_recorded")
            historical_oos: dict[str, Any] = {
                "status": "unable",
                "unable_reasons": ["validation_evidence_not_recorded"],
            }
            prospective_shadow: dict[str, Any] = {
                "status": "collecting" if weekly["comparison_count"] else "unable",
                "comparison_count": weekly["comparison_count"],
                "paired_disagreement_count": len(weekly["paired_disagreements"]),
                "abstention_count": len(weekly["abstentions"]),
                "unable_reasons": ["mature_shadow_evidence_not_recorded"],
            }
            hard_gate_failures = weekly["hard_gate_failures"]
            sample_concentration = {
                "status": "unable",
                "unable_reasons": ["sample_concentration_not_computed"],
            }
            cost_delta_pct = None
            drawdown_delta_pct = None
            mature_horizons: list[str] = []
            maturity_decision = "INSUFFICIENT_EVIDENCE"
        else:
            historical_oos = dict(evidence["historical_oos"])
            prospective_shadow = dict(evidence["prospective_shadow"])
            hard_gate_failures = list(evidence["hard_gate_failures"])
            sample_concentration = dict(evidence["sample_concentration"])
            cost_delta_pct = evidence["cost_delta_pct"]
            drawdown_delta_pct = evidence["drawdown_delta_pct"]
            mature_horizons = list(evidence["mature_horizons"])
            maturity_decision = str(evidence["maturity_decision"])

        return {
            "strategy_id": strategy_id,
            "protocol_id": weekly["protocol_id"],
            "champion": {"strategy_version": champion_version},
            "challenger": {"strategy_version": challenger_version},
            "historical_oos": historical_oos,
            "prospective_shadow": prospective_shadow,
            "hard_gate_failures": hard_gate_failures,
            "sample_concentration": sample_concentration,
            "cost_delta_pct": cost_delta_pct,
            "drawdown_delta_pct": drawdown_delta_pct,
            "unable_reasons": unable_reasons,
            "mature_horizons": mature_horizons,
            "maturity_decision": maturity_decision,
            "rollback_target": (
                selected.rollback_strategy_version if selected is not None else None
            ),
            "long_term_improvement_status": (
                "MATURE" if "60d" in mature_horizons else "PROVISIONAL"
            ),
            "automatic_promotion": False,
            "runtime_activated": False,
        }

    @staticmethod
    def _validate_review_request(
        *,
        evidence_summary: Mapping[str, Any],
        reason: str,
        rollback_strategy_version: str,
        approved_by: str,
        human_confirmed: bool,
    ) -> None:
        if human_confirmed is not True:
            raise ValueError("human_confirmation_required")
        if not isinstance(evidence_summary, Mapping) or not _EVIDENCE_FIELDS <= set(evidence_summary):
            raise ValueError("evidence_summary_incomplete")
        if not str(reason or "").strip():
            raise ValueError("governance_reason_required")
        if not str(rollback_strategy_version or "").strip():
            raise ValueError("rollback_target_required")
        if not str(approved_by or "").strip():
            raise ValueError("approved_by_required")

    @staticmethod
    def _governance_event_id(**payload: Any) -> str:
        from src.schemas.portfolio_strategy_validation import strategy_manifest_hash

        return strategy_manifest_hash(payload)

    @staticmethod
    def _serialize_strategy(row: Any) -> dict[str, Any]:
        return {
            "strategy_id": row.strategy_id,
            "strategy_version": row.strategy_version,
            "status": row.status,
            "manifest_hash": row.manifest_hash,
            "governance_reason": row.governance_reason,
            "approved_by": row.approved_by,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "runtime_activated": False,
        }
