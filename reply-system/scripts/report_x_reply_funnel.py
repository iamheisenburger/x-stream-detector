#!/usr/bin/env python3
"""Report the source-to-Telegram funnel without exposing environment secrets."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def _source_type(evidence_id: str) -> str:
    _, separator, value = str(evidence_id or "").rpartition("|")
    return value if separator and value in {"original", "reply"} else "unknown"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 2)


def run(args: argparse.Namespace) -> dict:
    config = json.loads(args.config.read_text())
    connection = sqlite3.connect(args.state_db)
    outcomes = connection.execute(
        """SELECT outcome,evidence_id FROM generation_outcomes
           WHERE kind='reply' AND day=?""",
        (args.day,),
    ).fetchall()
    outcome_counts = Counter(str(row[0]) for row in outcomes)
    source_outcomes: dict[str, Counter] = {
        "original": Counter(),
        "reply": Counter(),
        "unknown": Counter(),
    }
    for outcome, evidence_id in outcomes:
        source_outcomes[_source_type(evidence_id)][str(outcome)] += 1

    drafts = connection.execute(
        """SELECT status,created_at-source_created_at
           FROM reply_drafts WHERE day=?""",
        (args.day,),
    ).fetchall()
    latencies = [float(row[1]) for row in drafts if row[1] is not None]
    reads, read_cost = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(cost_microusd),0)
           FROM x_stream_reads WHERE day=?""",
        (args.day,),
    ).fetchone()
    llm_calls, llm_cost = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(cost_microusd),0)
           FROM llm_usage WHERE day=? AND lane='reply'""",
        (args.day,),
    ).fetchone()

    x_detection = config.get("x_detection") or {}
    generation = config.get("generation") or {}
    approval = config.get("human_approval") or {}
    return {
        "day_utc": args.day,
        "reads": int(reads),
        "read_cost_usd": round(int(read_cost) / 1_000_000, 6),
        "read_daily_cap_usd": round(
            int(x_detection.get("daily_microusd_cap") or 0) / 1_000_000,
            6,
        ),
        "generation_calls": int(llm_calls),
        "generation_cost_usd": round(int(llm_cost) / 1_000_000, 6),
        "reply_generation_daily_cap_usd": round(
            int(generation.get("reply_daily_reserve_microusd") or 0)
            / 1_000_000,
            6,
        ),
        "drafts": len(drafts),
        "draft_daily_cap": int(approval.get("daily_candidate_cap") or 0),
        "draft_statuses": dict(Counter(str(row[0]) for row in drafts)),
        "outcomes": dict(outcome_counts),
        "outcomes_by_source_type": {
            source: dict(counts) for source, counts in source_outcomes.items()
        },
        "source_to_notification_latency_seconds": {
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, default=Path("state.db"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument(
        "--day",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    return parser.parse_args()


def main() -> int:
    print(json.dumps(run(parse_args()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
