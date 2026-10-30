#!/usr/bin/env python3
"""Daily: which reply drafts were actually posted, and how each reply did.

Reads the account's own replies from the last 50 hours through the official
recent-search endpoint (app bearer token, one read per post, booked in the
same read ledger as the stream), matches each reply to the draft written for
the same source post, and stores the reply's public metrics (impressions,
likes, replies, reposts, quotes, bookmarks) in reply_results. A reply is seen
on two consecutive daily runs, so its numbers settle at about 1-2 days old.
Nothing is posted. Capped by --max-posts and by the day's remaining read cap.

Oct 2026 baseline (30 days): 15 posted replies, median 92 impressions, 2 likes
in total, under source posts with a median of about 75K impressions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOOKBACK_SECONDS = 50 * 3600
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

SCHEMA = """CREATE TABLE IF NOT EXISTS reply_results(
    reply_id TEXT PRIMARY KEY,
    source_tweet_id TEXT NOT NULL,
    draft_id INTEGER,
    posted_at REAL NOT NULL,
    impressions INTEGER,
    likes INTEGER,
    replies INTEGER,
    reposts INTEGER,
    quotes INTEGER,
    bookmarks INTEGER,
    text TEXT,
    measured_at REAL NOT NULL
)"""


# Original posts (vision checklist B4, Oct 22 2026): each is measured on three
# daily runs, so its numbers settle; at most 10 reads a run.
POSTS_LOOKBACK_SECONDS = 3 * 86400
POSTS_MAX_RESULTS = 10

POSTS_SCHEMA = """CREATE TABLE IF NOT EXISTS post_results(
    post_id TEXT PRIMARY KEY,
    posted_at REAL NOT NULL,
    impressions INTEGER,
    likes INTEGER,
    replies INTEGER,
    reposts INTEGER,
    quotes INTEGER,
    bookmarks INTEGER,
    text TEXT,
    measured_at REAL NOT NULL
)"""


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def replies_from_search(payload: dict) -> list[dict]:
    """The account's own replies in one recent-search page, with metrics."""
    rows = []
    for post in payload.get("data") or []:
        source = next(
            (ref.get("id") for ref in post.get("referenced_tweets") or [] if ref.get("type") == "replied_to"),
            None,
        )
        if not source or not post.get("created_at"):
            continue
        metrics = post.get("public_metrics") or {}
        rows.append({
            "reply_id": str(post["id"]),
            "source_tweet_id": str(source),
            "posted_at": _ts(post["created_at"]),
            "impressions": metrics.get("impression_count"),
            "likes": metrics.get("like_count"),
            "replies": metrics.get("reply_count"),
            "reposts": metrics.get("retweet_count"),
            "quotes": metrics.get("quote_count"),
            "bookmarks": metrics.get("bookmark_count"),
            "text": " ".join(str(post.get("text") or "").split())[:400],
        })
    return rows


def originals_from_search(payload: dict) -> list[dict]:
    """The account's own original posts in one recent-search page, with metrics."""
    rows = []
    for post in payload.get("data") or []:
        if any(ref.get("type") in ("replied_to", "retweeted") for ref in post.get("referenced_tweets") or []):
            continue
        if not post.get("created_at"):
            continue
        metrics = post.get("public_metrics") or {}
        rows.append({
            "post_id": str(post["id"]),
            "posted_at": _ts(post["created_at"]),
            "impressions": metrics.get("impression_count"),
            "likes": metrics.get("like_count"),
            "replies": metrics.get("reply_count"),
            "reposts": metrics.get("retweet_count"),
            "quotes": metrics.get("quote_count"),
            "bookmarks": metrics.get("bookmark_count"),
            "text": " ".join(str(post.get("text") or "").split())[:400],
        })
    return rows


def store_post_results(db: sqlite3.Connection, rows: list[dict], now: float) -> int:
    db.execute(POSTS_SCHEMA)
    for row in rows:
        db.execute(
            """INSERT INTO post_results(post_id,posted_at,impressions,likes,replies,reposts,quotes,bookmarks,text,measured_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(post_id) DO UPDATE SET impressions=excluded.impressions, likes=excluded.likes,
                   replies=excluded.replies, reposts=excluded.reposts, quotes=excluded.quotes,
                   bookmarks=excluded.bookmarks, measured_at=excluded.measured_at""",
            (row["post_id"], row["posted_at"], row["impressions"], row["likes"], row["replies"], row["reposts"],
             row["quotes"], row["bookmarks"], row["text"], now),
        )
    db.commit()
    return len(rows)


def posts_summary(db_path: str, now: float, days: int = 7) -> str | None:
    """Vision checklist B4-B6: the last 7 days of original posts in one line."""
    db = sqlite3.connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_results'").fetchone():
            return None
        rows = db.execute(
            "SELECT impressions, likes, replies, bookmarks FROM post_results WHERE posted_at>=?",
            (now - days * 86_400,),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return "Posts (7 days): none measured"
    views = [int(r[0] or 0) for r in rows]
    kept = sum(1 for r in rows if int(r[3] or 0) > 0 or int(r[2] or 0) > 0)
    return (f"Posts (7 days): {len(rows)} measured, median {statistics.median(views):.0f} views, "
            f"{sum(int(r[1] or 0) for r in rows)} likes, {sum(int(r[3] or 0) for r in rows)} bookmarks, "
            f"{sum(int(r[2] or 0) for r in rows)} replies; saved or answered: {kept} of {len(rows)}")


def store_results(db: sqlite3.Connection, rows: list[dict], now: float) -> int:
    """Upsert metrics; link each reply to its draft. Returns rows matched to a draft."""
    db.execute(SCHEMA)
    matched = 0
    for row in rows:
        draft = db.execute(
            "SELECT id FROM reply_drafts WHERE tweet_id=?", (row["source_tweet_id"],)
        ).fetchone()
        matched += bool(draft)
        db.execute(
            """INSERT INTO reply_results(reply_id,source_tweet_id,draft_id,posted_at,impressions,likes,
                   replies,reposts,quotes,bookmarks,text,measured_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(reply_id) DO UPDATE SET impressions=excluded.impressions, likes=excluded.likes,
                   replies=excluded.replies, reposts=excluded.reposts, quotes=excluded.quotes,
                   bookmarks=excluded.bookmarks, draft_id=COALESCE(excluded.draft_id, reply_results.draft_id),
                   measured_at=excluded.measured_at""",
            (row["reply_id"], row["source_tweet_id"], draft[0] if draft else None, row["posted_at"],
             row["impressions"], row["likes"], row["replies"], row["reposts"], row["quotes"],
             row["bookmarks"], row["text"], now),
        )
    db.commit()
    return matched


def posted_summary(db_path: str, since: float, until: float) -> str | None:
    """One line for the daily summary: drafts posted and how replies are doing."""
    db = sqlite3.connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_results'").fetchone():
            return None
        drafts = db.execute("SELECT COUNT(*) FROM reply_drafts WHERE created_at>=? AND created_at<?", (since, until)).fetchone()[0]
        posted = db.execute(
            "SELECT COUNT(*) FROM reply_results WHERE draft_id IN (SELECT id FROM reply_drafts WHERE created_at>=? AND created_at<?)",
            (since, until),
        ).fetchone()[0]
        settled = db.execute(
            "SELECT impressions, likes, replies FROM reply_results WHERE posted_at>=? AND posted_at<? AND impressions IS NOT NULL",
            (until - 4 * 86_400, until - 86_400),
        ).fetchall()
    finally:
        db.close()
    line = f"Posted: {posted} of {drafts} drafts from the last 24h"
    if settled:
        impressions = [int(r[0]) for r in settled]
        liked = sum(1 for r in settled if int(r[1] or 0) > 0)
        line += (f"; replies 1-4 days old: median {statistics.median(impressions):.0f} impressions, "
                 f"{sum(int(r[1] or 0) for r in settled)} likes, {sum(int(r[2] or 0) for r in settled)} replies "
                 f"({len(settled)} replies; {liked} of {len(settled)} liked, {round(100 * liked / len(settled))}%)")
    return line


async def run(max_posts: int) -> dict:
    from bot.config import load
    from bot.state import State
    from bot.x_read_budget import XReadBudget
    from bot.x_stream import OfficialXStream

    cfg = load()
    state = State()
    now = time.time()
    price = int(cfg.x_detection.post_read_microusd)
    # Its own lane: tracking never takes the reply stream's daily reads.
    snapshot = XReadBudget(cfg.x_detection, state, lane="measurement").snapshot(now)
    room = max(0, min(snapshot.daily_cap_microusd - snapshot.today_microusd,
                      snapshot.monthly_cap_microusd - snapshot.month_microusd) // max(1, price))
    budget = min(max_posts, room)
    username = str(cfg.publishing.expected_username or "").lstrip("@")
    if not username or budget < 10:
        return {"skipped": "no username or no read budget", "room": room}
    start = datetime.fromtimestamp(now - LOOKBACK_SECONDS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = quote(f"from:{username} is:reply")
    rows, booked, next_token = [], 0, None
    async with OfficialXStream(cfg) as api:
        while booked < budget:
            url = (f"{SEARCH_URL}?query={query}&start_time={start}&max_results={max(10, min(100, budget - booked))}"
                   "&tweet.fields=created_at,public_metrics,referenced_tweets"
                   + (f"&next_token={next_token}" if next_token else ""))
            response = await api.client.get(url, headers=api.headers)
            if response.status_code != 200:
                return {"error": f"recent search HTTP {response.status_code}", "booked": booked}
            payload = response.json()
            for post in payload.get("data") or []:
                if state.record_x_stream_read(post_id=str(post["id"]), author=f"measure:{username}",
                                              cost_microusd=price, now=now):
                    booked += 1
            rows.extend(replies_from_search(payload))
            next_token = (payload.get("meta") or {}).get("next_token")
            if not next_token:
                break
        originals: list[dict] = []
        if budget - booked >= POSTS_MAX_RESULTS:
            since = datetime.fromtimestamp(now - POSTS_LOOKBACK_SECONDS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (f"{SEARCH_URL}?query={quote(f'from:{username} -is:reply -is:retweet')}&start_time={since}"
                   f"&max_results={POSTS_MAX_RESULTS}&tweet.fields=created_at,public_metrics,referenced_tweets")
            response = await api.client.get(url, headers=api.headers)
            if response.status_code == 200:
                payload = response.json()
                for post in payload.get("data") or []:
                    if state.record_x_stream_read(post_id=str(post["id"]), author=f"measure:{username}",
                                                  cost_microusd=price, now=now):
                        booked += 1
                originals = originals_from_search(payload)
    db = sqlite3.connect("state.db", timeout=30)
    try:
        matched = store_results(db, rows, now)
        store_post_results(db, originals, now)
    finally:
        db.close()
    return {"replies_seen": len(rows), "matched_to_drafts": matched, "posts_seen": len(originals),
            "reads_booked": booked,
            "cost_usd": round(booked * price / 1e6, 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-posts", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.max_posts))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
