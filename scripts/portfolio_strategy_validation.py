#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual, file-backed portfolio strategy validation CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.repositories.decision_quality_repo import DecisionQualityRepository
from src.services.portfolio_strategy_replay_service import PortfolioStrategyReplayService
from src.services.portfolio_walk_forward_service import PortfolioWalkForwardService
from src.storage import DatabaseManager


def _read(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _contexts(cutoff_from: str, cutoff_to: str) -> list[Any]:
    database_path = os.getenv("DATABASE_PATH")
    if not database_path:
        return []
    db = DatabaseManager(db_url=f"sqlite:///{Path(database_path).expanduser().resolve()}")
    since = datetime.fromisoformat(cutoff_from)
    until = datetime.fromisoformat(cutoff_to)
    return DecisionQualityRepository(db).list_contexts_for_weekly_review(
        since=since,
        until=until,
        limit=1000,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--as-of", required=True)

    build = sub.add_parser("build-dataset")
    build.add_argument("--from", dest="cutoff_from", required=True)
    build.add_argument("--to", dest="cutoff_to", required=True)
    build.add_argument("--output", required=True)

    replay = sub.add_parser("replay")
    replay.add_argument("--dataset", required=True)
    replay.add_argument("--strategy", required=True)
    replay.add_argument("--output", required=True)

    report = sub.add_parser("report")
    report.add_argument("--run", required=True)

    walk_forward = sub.add_parser("walk-forward")
    walk_forward.add_argument("--champion", required=True)
    walk_forward.add_argument("--challenger", required=True)
    walk_forward.add_argument("--hold", required=True)
    walk_forward.add_argument("--eligible-events", required=True)
    walk_forward.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    service = PortfolioStrategyReplayService()
    if args.command == "preflight":
        payload = {
            "status": "READY" if os.getenv("DATABASE_PATH") else "NOT_READY",
            "as_of": args.as_of,
            "database_identity": "configured" if os.getenv("DATABASE_PATH") else "missing",
            "network_calls": 0,
            "automatic_multi_agent": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "build-dataset":
        contexts = _contexts(args.cutoff_from, args.cutoff_to)
        payload = service.build_dataset(
            contexts=contexts,
            cutoff_from=args.cutoff_from,
            cutoff_to=args.cutoff_to,
        )
        if not os.getenv("DATABASE_PATH"):
            payload["blockers"] = ["database_path_required"]
        _write(args.output, payload)
        return 0
    if args.command == "replay":
        result = service.replay(
            dataset=_read(args.dataset),
            strategy=_read(args.strategy),
        )
        _write(args.output, result)
        print(json.dumps({"status": result["status"], "run_id": result["run_id"]}))
        return 0
    if args.command == "walk-forward":
        eligible_payload = _read(args.eligible_events)
        eligible = eligible_payload.get("event_ids") or []
        result = PortfolioWalkForwardService().compare_paired_runs(
            champion=_read(args.champion),
            challenger=_read(args.challenger),
            hold=_read(args.hold),
            eligible_event_ids=eligible,
        )
        _write(args.output, result)
        print(json.dumps({"paired_event_count": result["paired_event_count"]}))
        return 0
    run = _read(args.run)
    report = {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "event_count": run.get("event_count", 0),
        "network_calls": run.get("network_calls", 0),
        "portfolio_metrics": "unable"
        if any(event.get("portfolio_return_status") == "unable" for event in run.get("events", []))
        else "eligible",
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
