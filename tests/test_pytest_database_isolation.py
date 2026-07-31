# -*- coding: utf-8 -*-
"""Regression coverage for the pytest database isolation guard."""

from __future__ import annotations

import os
from pathlib import Path


def test_01_simulate_test_teardown_removing_database_isolation() -> None:
    os.environ.pop("DATABASE_PATH", None)
    os.environ.pop("ENV_FILE", None)


def test_02_pytest_restores_database_isolation_before_next_test() -> None:
    assert "DATABASE_PATH" in os.environ
    assert "ENV_FILE" in os.environ
    database_path = Path(os.environ["DATABASE_PATH"]).resolve()
    env_file = Path(os.environ["ENV_FILE"]).resolve()
    assert database_path != (Path.cwd() / "data" / "stock_analysis.db").resolve()
    assert f"DATABASE_PATH={database_path}\n" == env_file.read_text(encoding="utf-8")
