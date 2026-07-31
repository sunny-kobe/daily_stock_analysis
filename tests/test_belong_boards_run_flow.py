# -*- coding: utf-8 -*-
"""Regression tests for belong-board run-flow diagnostics."""

import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import patch

import pandas as pd
import pytest

from data_provider.efinance_fetcher import EfinanceFetcher, _ef_call_with_timeout
from src.services.run_diagnostics import (
    activate_run_diagnostic_context,
    current_diagnostic_snapshot,
    reset_run_diagnostic_context,
)
from data_provider.base import DataFetcherManager


class _BoardFetcher:
    def __init__(self, name: str, result):
        self.name = name
        self.priority = 0
        self._result = result
        self.calls = 0

    def get_belong_board(self, _stock_code: str):
        self.calls += 1
        return self._result


class _FailingBoardFetcher(_BoardFetcher):
    def __init__(self, name: str, error: Exception):
        super().__init__(name, [])
        self._error = error

    def get_belong_board(self, _stock_code: str):
        self.calls += 1
        raise self._error


class _TimeoutAwareBoardFetcher(_BoardFetcher):
    def __init__(self, name: str, result):
        super().__init__(name, result)
        self.timeout = None

    def get_belong_board(self, _stock_code: str, *, timeout=None):
        self.calls += 1
        self.timeout = timeout
        return self._result


def _capture_belong_board_run(manager: DataFetcherManager):
    flow_events = []
    token = activate_run_diagnostic_context(
        trace_id="trace-boards",
        task_id="task-boards",
        query_id="query-boards",
        stock_code="600519",
        trigger_source="api",
        event_sink=flow_events.append,
    )
    try:
        boards = manager.get_belong_boards("600519")
        diagnostics = current_diagnostic_snapshot()
    finally:
        reset_run_diagnostic_context(token)
    return boards, diagnostics, flow_events


def test_get_belong_boards_records_successful_provider_run():
    manager = DataFetcherManager(
        fetchers=[
            _BoardFetcher(
                "BoardFetcher",
                [{"name": "白酒", "type": "行业"}],
            )
        ]
    )

    boards, diagnostics, flow_events = _capture_belong_board_run(manager)

    assert boards
    assert diagnostics is not None
    provider_runs = diagnostics["provider_runs"]
    assert len(provider_runs) == 1
    assert provider_runs[0]["data_type"] == "belong_boards"
    assert provider_runs[0]["provider"] == "BoardFetcher"
    assert provider_runs[0]["operation"] == "get_belong_board"
    assert provider_runs[0]["success"] is True
    assert provider_runs[0]["record_count"] == len(boards)
    assert [event["type"] for event in flow_events] == ["provider_run_started", "provider_run"]
    assert flow_events[0]["node_id"] == flow_events[1]["node_id"]
    assert flow_events[0]["node_id"] == "provider_belong_boards_boardfetcher_1"


def test_get_belong_boards_records_empty_attempt_and_fallback():
    manager = DataFetcherManager(
        fetchers=[
            _BoardFetcher("EmptyBoardFetcher", []),
            _BoardFetcher("FallbackBoardFetcher", [{"name": "电力设备", "type": "行业"}]),
        ]
    )

    boards, diagnostics, flow_events = _capture_belong_board_run(manager)

    assert boards
    assert diagnostics is not None
    provider_runs = diagnostics["provider_runs"]
    assert [run["provider"] for run in provider_runs] == ["EmptyBoardFetcher", "FallbackBoardFetcher"]
    assert [run["success"] for run in provider_runs] == [False, True]
    assert provider_runs[0]["error_type"] == "empty"
    assert provider_runs[0]["fallback_to"] == "FallbackBoardFetcher"
    assert len(flow_events) == 4


def test_get_belong_boards_records_exception_attempt_and_fallback():
    manager = DataFetcherManager(
        fetchers=[
            _FailingBoardFetcher("FailingBoardFetcher", RuntimeError("board source down")),
            _BoardFetcher("FallbackBoardFetcher", [{"name": "电力设备", "type": "行业"}]),
        ]
    )

    boards, diagnostics, flow_events = _capture_belong_board_run(manager)

    assert boards
    assert diagnostics is not None
    provider_runs = diagnostics["provider_runs"]
    assert [run["provider"] for run in provider_runs] == ["FailingBoardFetcher", "FallbackBoardFetcher"]
    assert provider_runs[0]["success"] is False
    assert provider_runs[0]["error_type"] == "RuntimeError"
    assert provider_runs[0]["fallback_to"] == "FallbackBoardFetcher"
    assert provider_runs[1]["success"] is True
    assert len(flow_events) == 4


def test_get_belong_boards_propagates_request_timeout_to_provider():
    fetcher = _TimeoutAwareBoardFetcher(
        "TimeoutAwareBoardFetcher",
        [{"name": "白酒", "type": "行业"}],
    )
    manager = DataFetcherManager(fetchers=[fetcher])

    boards = manager.get_belong_boards("600519", timeout_seconds=0.25)

    assert boards
    assert fetcher.timeout == 0.25


def test_efinance_belong_board_forwards_timeout_to_guarded_call():
    fetcher = EfinanceFetcher(sleep_min=0, sleep_max=0)

    with (
        patch.object(fetcher, "_set_random_user_agent"),
        patch.object(fetcher, "_enforce_rate_limit"),
        patch(
            "data_provider.efinance_fetcher._ef_call_with_timeout",
            return_value=pd.DataFrame(),
        ) as guarded_call,
    ):
        assert fetcher.get_belong_board("600519", timeout=0.25) is None

    assert guarded_call.call_args.kwargs["timeout"] == 0.25


def test_efinance_timeout_worker_does_not_block_process_exit():
    release = threading.Event()
    before = {thread.ident for thread in threading.enumerate()}

    try:
        with pytest.raises(FuturesTimeoutError):
            _ef_call_with_timeout(lambda: release.wait(1.0), timeout=0.01)

        spawned = [
            thread
            for thread in threading.enumerate()
            if thread.ident not in before and thread.is_alive()
        ]
        assert spawned
        assert all(thread.daemon for thread in spawned)
    finally:
        release.set()
