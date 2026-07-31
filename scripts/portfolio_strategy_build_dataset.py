#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a deterministic historical strategy dataset from frozen local JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.portfolio_strategy_historical_sample_service import (
    PortfolioStrategyHistoricalSampleService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a local historical strategy dataset")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = json.loads(args.source.read_text(encoding="utf-8"))
    dataset = PortfolioStrategyHistoricalSampleService().build(source=source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    reason_counts = Counter(item["reason_code"] for item in dataset["excluded_events"])
    print(f"候选记录: {dataset['candidate_count']}")
    print(f"可用样本: {len(dataset['eligible_events'])}")
    print(f"排除记录: {len(dataset['excluded_events'])}")
    if reason_counts:
        print("排除原因: " + ", ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items())))
    if dataset["synthetic"]:
        print("测试数据，不代表真实成绩")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
