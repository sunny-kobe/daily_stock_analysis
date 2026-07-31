#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export persisted DSA decisions as an evidence-gap-preserving source JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.portfolio_strategy_source_export_service import (
    PortfolioStrategySourceExportService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export real DSA candidates without evidence backfill")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--frozen-at", required=True)
    parser.add_argument("--reporting-currency", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = PortfolioStrategySourceExportService(args.database).export(
        frozen_at=args.frozen_at,
        reporting_currency=args.reporting_currency,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"真实候选: {source['candidate_count']}")
    print("可直接回测: 0")
    gap_counts = source["source_audit"]["gap_counts"]
    if gap_counts:
        print("证据缺口: " + ", ".join(f"{key}={value}" for key, value in gap_counts.items()))
    print("资料不足，不代表真实历史成绩")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
