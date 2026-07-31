#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run deterministic portfolio strategy validation from frozen JSON files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.portfolio_strategy_backtest_service import PortfolioStrategyBacktestService
from src.services.strategy_registry_service import canonical_json, sha256_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a frozen, no-network portfolio strategy test")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--strategy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--real-performance", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    strategy = json.loads(args.strategy.read_text(encoding="utf-8"))
    if args.real_performance:
        hash_payload = {key: value for key, value in dataset.items() if key != "dataset_hash"}
        if dataset.get("synthetic") is not False or dataset.get("dataset_hash") != sha256_json(hash_payload):
            print("真实成绩必须使用完整校验的非测试数据", file=sys.stderr)
            return 2
    report = PortfolioStrategyBacktestService().run(
        strategy_manifest=strategy,
        dataset=dataset,
        persist=False,
    )
    if dataset.get("synthetic"):
        report["display_status"] = "测试数据，不代表真实成绩"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical_json(report) + "\n", encoding="utf-8")
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
