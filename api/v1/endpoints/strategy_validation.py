# -*- coding: utf-8 -*-
"""Versioned strategy validation with explicit human-only stage changes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.v1.errors import api_error
from api.v1.schemas.strategy_validation import (
    StrategyCreateRequest,
    StrategyRunCreateRequest,
    StrategyTransitionBody,
    StrategyTransitionResponse,
    StrategyValidationRunItem,
    StrategyVersionItem,
    StrategyVersionListResponse,
)
from src.services.strategy_registry_service import StrategyRegistryService


logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/strategies", response_model=StrategyVersionListResponse)
def list_strategies() -> StrategyVersionListResponse:
    try:
        return StrategyVersionListResponse(items=StrategyRegistryService().list_versions())
    except Exception as exc:
        raise _internal_error("List strategy versions failed", exc)


@router.get(
    "/strategies/{strategy_key}/versions/{version}",
    response_model=StrategyVersionItem,
)
def get_strategy(strategy_key: str, version: str) -> StrategyVersionItem:
    try:
        return StrategyVersionItem(
            **StrategyRegistryService().get_version(strategy_key, version)
        )
    except ValueError as exc:
        raise _service_error(exc)
    except Exception as exc:
        raise _internal_error("Get strategy version failed", exc)


@router.get("/runs/{run_id}", response_model=StrategyValidationRunItem)
def get_run(run_id: str) -> StrategyValidationRunItem:
    try:
        return StrategyValidationRunItem(**StrategyRegistryService().get_run(run_id))
    except ValueError as exc:
        raise _service_error(exc)
    except Exception as exc:
        raise _internal_error("Get strategy validation run failed", exc)


@router.post("/strategies", response_model=StrategyVersionItem)
def create_strategy(request: StrategyCreateRequest) -> StrategyVersionItem:
    try:
        return StrategyVersionItem(
            **StrategyRegistryService().create_version(request.model_dump(mode="json"))
        )
    except ValueError as exc:
        raise _service_error(exc)
    except Exception as exc:
        raise _internal_error("Create strategy version failed", exc)


@router.post("/runs", response_model=StrategyValidationRunItem)
def create_run(request: StrategyRunCreateRequest) -> StrategyValidationRunItem:
    try:
        return StrategyValidationRunItem(
            **StrategyRegistryService().record_validation_run(
                request.model_dump(mode="json")
            )
        )
    except ValueError as exc:
        raise _service_error(exc)
    except Exception as exc:
        raise _internal_error("Create strategy validation run failed", exc)


@router.post(
    "/strategies/{strategy_key}/versions/{version}/transition",
    response_model=StrategyTransitionResponse,
)
def transition_strategy(
    strategy_key: str,
    version: str,
    request: StrategyTransitionBody,
) -> StrategyTransitionResponse:
    try:
        return StrategyTransitionResponse(
            **StrategyRegistryService().transition(
                strategy_key=strategy_key,
                version=version,
                to_status=request.to_status,
                human_reason=request.human_reason,
            )
        )
    except ValueError as exc:
        raise _service_error(exc)
    except Exception as exc:
        raise _internal_error("Transition strategy stage failed", exc)


def _service_error(exc: ValueError) -> HTTPException:
    error = str(exc)
    if error in {"strategy_version_not_found", "validation_run_not_found"}:
        return api_error(404, error, error.replace("_", " "))
    if error in {
        "strategy_version_immutable",
        "validation_run_immutable",
        "invalid_strategy_transition",
        "qualifying_backtest_required",
        "forward_observation_required",
    }:
        return api_error(409, error, error.replace("_", " "))
    return api_error(400, "validation_error", error)


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error("%s: %s", message, exc, exc_info=True)
    return api_error(500, "internal_error", message)
