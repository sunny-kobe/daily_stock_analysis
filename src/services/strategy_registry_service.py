# -*- coding: utf-8 -*-
"""Version and manually advance portfolio strategies without execution side effects."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.repositories.strategy_validation_repo import StrategyValidationRepository
from src.schemas.strategy_validation import (
    StrategyTransitionRequest,
    StrategyValidationRun,
    StrategyVersionManifest,
)
from src.storage import DatabaseManager


_ALLOWED_TRANSITIONS = {
    "draft": ("backtest_running",),
    "backtest_running": ("backtest_failed", "simulation"),
    "backtest_failed": (),
    "simulation": ("small_capital",),
    "small_capital": ("active",),
    "active": ("retired",),
    "retired": (),
}

_STRATEGY_STATUS_LABELS = {
    "draft": "待回测",
    "backtest_running": "回测中",
    "backtest_failed": "回测未通过",
    "simulation": "模拟观察",
    "small_capital": "小额验证",
    "active": "正式使用",
    "retired": "已停用",
}

_RUN_STATUS_LABELS = {
    "completed": "已完成",
    "failed": "未通过",
    "unable": "资料不足",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class StrategyRegistryService:
    def __init__(self, db_manager: DatabaseManager | None = None):
        self.repo = StrategyValidationRepository(db_manager)

    def create_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        manifest = StrategyVersionManifest.model_validate(payload)
        manifest_payload = manifest.model_dump(mode="json")
        manifest_json = canonical_json(manifest_payload)
        row, _ = self.repo.create_strategy_version(
            {
                "strategy_key": manifest.strategy_key,
                "version": manifest.version,
                "name": manifest.name,
                "initial_status": manifest.status,
                "manifest_json": manifest_json,
                "manifest_hash": hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
            }
        )
        return self._serialize_version(row)

    def get_version(self, strategy_key: str, version: str) -> dict[str, Any]:
        row = self.repo.get_strategy_version(strategy_key=strategy_key, version=version)
        if row is None:
            raise ValueError("strategy_version_not_found")
        return self._serialize_version(row)

    def list_versions(self) -> list[dict[str, Any]]:
        return [self._serialize_version(row) for row in self.repo.list_strategy_versions()]

    def record_validation_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = StrategyValidationRun.model_validate(payload)
        if (
            run.qualifying
            and run.validation_kind == "historical_backtest"
            and run.result.get("historical_status") != "complete"
        ):
            raise ValueError("only_complete_historical_backtests_can_qualify")
        if self.repo.get_strategy_version(
            strategy_key=run.strategy_key,
            version=run.strategy_version,
        ) is None:
            raise ValueError("strategy_version_not_found")
        run_payload = run.model_dump(mode="json")
        run_hash = sha256_json(run_payload)
        row, _ = self.repo.create_validation_run(
            {
                "run_id": f"svr-{run_hash[:24]}",
                "strategy_key": run.strategy_key,
                "strategy_version": run.strategy_version,
                "validation_kind": run.validation_kind,
                "protocol_json": canonical_json(run.protocol),
                "dataset_hash": run.dataset_hash,
                "engine_version": run.engine_version,
                "status": run.status,
                "qualifying": run.qualifying,
                "result_json": canonical_json(run.result),
                "run_hash": run_hash,
            }
        )
        return self._serialize_run(row)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get_validation_run(run_id=run_id)
        if row is None:
            raise ValueError("validation_run_not_found")
        return self._serialize_run(row)

    def transition(
        self,
        *,
        strategy_key: str,
        version: str,
        to_status: str,
        human_reason: str,
    ) -> dict[str, Any]:
        if not str(human_reason or "").strip():
            raise ValueError("human_reason_required")
        request = StrategyTransitionRequest(
            to_status=to_status,
            human_reason=human_reason,
        )
        row = self.repo.get_strategy_version(strategy_key=strategy_key, version=version)
        if row is None:
            raise ValueError("strategy_version_not_found")
        current_status = self._current_status(row)
        manifest = json.loads(row.manifest_json)
        evaluation_mode = manifest["evaluation_mode"]
        allowed = (
            ["simulation"]
            if current_status == "draft" and evaluation_mode == "forward_only"
            else list(_ALLOWED_TRANSITIONS[current_status])
        )
        if request.to_status not in allowed:
            raise ValueError("invalid_strategy_transition")
        if (
            request.to_status == "simulation"
            and evaluation_mode != "forward_only"
            and not self.repo.has_qualifying_run(
                strategy_key=strategy_key,
                strategy_version=version,
                validation_kind="historical_backtest",
            )
        ):
            raise ValueError("qualifying_backtest_required")
        if request.to_status == "small_capital" and not self.repo.has_qualifying_run(
            strategy_key=strategy_key,
            strategy_version=version,
            validation_kind="forward_observation",
        ):
            raise ValueError("forward_observation_required")
        transition = self.repo.append_transition(
            strategy_key=strategy_key,
            strategy_version=version,
            from_status=current_status,
            to_status=request.to_status,
            human_reason=request.human_reason,
        )
        return {
            "strategy_key": strategy_key,
            "version": version,
            "from_status": transition.from_status,
            "status": transition.to_status,
            "status_label": _STRATEGY_STATUS_LABELS[transition.to_status],
            "human_reason": transition.human_reason,
            "transition_id": transition.id,
        }

    def _current_status(self, row) -> str:
        transitions = self.repo.list_transitions(
            strategy_key=row.strategy_key,
            strategy_version=row.version,
        )
        return transitions[-1].to_status if transitions else row.initial_status

    def _serialize_version(self, row) -> dict[str, Any]:
        manifest = json.loads(row.manifest_json)
        status = self._current_status(row)
        runs = self.repo.list_validation_runs(
            strategy_key=row.strategy_key,
            strategy_version=row.version,
        )
        return {
            **manifest,
            "status": status,
            "status_label": _STRATEGY_STATUS_LABELS[status],
            "allowed_transitions": self._allowed_transitions(
                strategy_key=row.strategy_key,
                version=row.version,
                status=status,
                evaluation_mode=manifest["evaluation_mode"],
            ),
            "latest_run": self._serialize_run(runs[0]) if runs else None,
            "manifest_hash": row.manifest_hash,
            "created_at": row.created_at.isoformat(),
        }

    def _allowed_transitions(
        self,
        *,
        strategy_key: str,
        version: str,
        status: str,
        evaluation_mode: str,
    ) -> list[str]:
        allowed = (
            ["simulation"]
            if status == "draft" and evaluation_mode == "forward_only"
            else list(_ALLOWED_TRANSITIONS[status])
        )
        if (
            status == "backtest_running"
            and evaluation_mode != "forward_only"
            and not self.repo.has_qualifying_run(
                strategy_key=strategy_key,
                strategy_version=version,
                validation_kind="historical_backtest",
            )
        ):
            allowed.remove("simulation")
        if status == "simulation" and not self.repo.has_qualifying_run(
            strategy_key=strategy_key,
            strategy_version=version,
            validation_kind="forward_observation",
        ):
            allowed.remove("small_capital")
        return allowed

    @staticmethod
    def _serialize_run(row) -> dict[str, Any]:
        return {
            "run_id": row.run_id,
            "strategy_key": row.strategy_key,
            "strategy_version": row.strategy_version,
            "validation_kind": row.validation_kind,
            "protocol": json.loads(row.protocol_json),
            "dataset_hash": row.dataset_hash,
            "engine_version": row.engine_version,
            "status": row.status,
            "status_label": _RUN_STATUS_LABELS[row.status],
            "qualifying": row.qualifying,
            "result": json.loads(row.result_json),
            "run_hash": row.run_hash,
            "created_at": row.created_at.isoformat(),
        }
