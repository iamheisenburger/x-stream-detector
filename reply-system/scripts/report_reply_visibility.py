#!/usr/bin/env python3
"""R3: how each posted reply did, next to its timing and its source post's
early numbers (the free rise checks at about 90 s and 60 min).

Reads state.db read-only; spends nothing. The R3 exit is a median of at least
5 times the baseline impressions per posted reply over the next 30 replies.

Baseline, re-measured Oct 19 2026 on the 15 replies posted Sep 30-Oct 4:
median 129 impressions (92 was first reported), 2 likes in total, 0 author
replies; our reply got a median 0.2% of the source post's impressions.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASELINE_MEDIAN_IMPRESSIONS = 129


def visibility_report(db_path: str, since: float = 0.0) -> dict:
    db = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        rows = db.execute(
            """SELECT d.author, r.reply_id, r.posted_at - d.source_created_at, r.impressions, r.likes, r.replies,
                      s90.impressions, s90.replies, s60.impressions, s60.replies, r.text
               FROM reply_results r
               JOIN reply_drafts d ON d.id = r.draft_id
               LEFT JOIN source_snapshots s90 ON s90.post_id = d.tweet_id AND s90.kind = 'rise_90s'
               LEFT JOIN source_snapshots s60 ON s60.post_id = d.tweet_id AND s60.kind = 'rise_60m'
               WHERE r.posted_at >= ?
               ORDER BY r.posted_at""",
            (float(since),),
        ).fetchall()
    finally:
        db.close()
    keys = ("author", "reply_id", "posted_after_s", "impressions", "likes", "replies",
            "views_90s", "replies_90s", "views_60m", "replies_60m", "text")
    report_rows = [dict(zip(keys, row)) for row in rows]
    for row in report_rows:
        row["posted_after_s"] = round(row["posted_after_s"]) if row["posted_after_s"] is not None else None
    impressions = [row["impressions"] for row in report_rows if row["impressions"] is not None]
    delays = [row["posted_after_s"] for row in report_rows if row["posted_after_s"] is not None]
    return {
        "posted": len(report_rows),
        "median_impressions": statistics.median(impressions) if impressions else None,
        "likes": sum(int(row["likes"] or 0) for row in report_rows),
        "median_posted_after_s": statistics.median(delays) if delays else None,
        "target_median": 5 * BASELINE_MEDIAN_IMPRESSIONS,
        "rows": report_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="state.db")
    parser.add_argument("--since", type=float, default=0.0, help="epoch seconds")
    args = parser.parse_args()
    report = visibility_report(args.db, args.since)
    for row in report["rows"]:
        print(f"@{row['author']:<16} posted +{row['posted_after_s']}s  {row['impressions']} impressions, "
              f"{row['likes']} likes | source at 90s: {row['views_90s']} views, {row['replies_90s']} replies; "
              f"at 60m: {row['views_60m']} views, {row['replies_60m']} replies")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
