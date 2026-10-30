#!/usr/bin/env python3
"""Bounded reach/content audit using the official X recent-search endpoint.

The audit never opens a websocket and never changes the production roster or
stream rules. Every returned Post is written to the same durable read ledger as
the production stream before the result is used, so discovery cannot bypass the
daily or monthly X-read ceilings. Paid audits are disabled by default because
even correctly metered search results consume the same daily allowance needed
for time-sensitive production replies. Use the free public-profile audit for
normal roster work.

Pentra bootstrap: before the reply stream exists there is no live allowance to
protect, so ``--per-handle --bootstrap-daily-cap-usd`` may measure the seed
candidates in one sitting. It is refused while ``x_detection.enabled`` is true,
every read still lands in the same durable ledger, and the monthly cap still
applies. Per-handle queries (``max_results`` 10, the API minimum) give every
candidate a fair sample instead of letting prolific posters fill a shared page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bot.config import load
from bot.state import State
from bot.x_read_budget import XReadBudget, XReadBudgetOpen
from bot.x_stream import OfficialXStream


SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
MAX_QUERY_CHARS = 512
URL_RE = re.compile(r"https?://\S+", re.I)
TOPICAL_RE = re.compile(
    r"\b(?:seo|search|google|keywords?|rank(?:s|ing|ings)?|organic|traffic|"
    r"content|blog(?:s|ging)?|website|web ?site|landing page|copy(?:writing)?|"
    r"newsletter|audience|distribution|indie ?hacker|bootstrap(?:ped|ping)?|"
    r"ai|agi|model|llm|agent|automation|software|saas|product|startup|"
    r"founder|company|business|customer|user|developer|engineering|code|"
    r"coding|cloud|api|growth|marketing|sales|revenue|conversion|design|"
    r"launch|ship|build|team|hiring|enterprise|open[- ]source)\b",
    re.I,
)
FORBIDDEN_RE = re.compile(
    r"\b(?:election|president|senator|congress|republican|democrat|trump|"
    r"biden|modi|war|military|iran|israel|ukraine|gaza|football|soccer|nba|"
    r"nfl|cricket|movie|music|celebrity|crypto|bitcoin|ethereum|token|stock|"
    r"shares?|trading|portfolio|market rally|breaking news|minister|government|"
    r"parliament|immigration|immigrants|bjp|labour party|tory|tories|protest|"
    r"citizens|patriots?|senate|governor|prime minister|nfl|premier league)\b",
    re.I,
)


def require_paid_audit_opt_in(enabled: bool) -> None:
    if not enabled:
        raise SystemExit(
            "paid official-X reach audit refused before any request: use "
            "scripts/audit_x_target_reach_public.py for normal roster work; "
            "--allow-production-read-debit is an explicit emergency override"
        )


def _handles(values: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            value.lstrip("@").strip()
            for value in values
            if value.lstrip("@").strip()
        )
    )


def build_query(handles: list[str]) -> str:
    normalized = _handles(handles)
    if not normalized:
        raise ValueError("at least one handle is required")
    query = (
        f"({' OR '.join(f'from:{handle}' for handle in normalized)}) "
        "-is:reply -is:retweet lang:en"
    )
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(
            f"query is {len(query)} characters; split it below {MAX_QUERY_CHARS}"
        )
    return query


def build_handle_query(handle: str) -> str:
    return f"from:{handle} -is:reply -is:retweet lang:en"


def audit_window(
    now: datetime,
    minimum_post_age_hours: float,
) -> tuple[datetime, datetime]:
    """Return the [start, end] window every audited handle is measured over.

    Recent search only reaches seven days back; the start stays an hour inside
    that edge so the request is never rejected for an out-of-range start_time.
    """
    # X rejects an end_time closer than ten seconds to the request.
    end = now - timedelta(hours=max(minimum_post_age_hours, 1 / 60))
    start = now - timedelta(days=7) + timedelta(hours=1)
    if end <= start:
        raise ValueError("minimum post age leaves no measurable window")
    return start, end


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def bootstrap_budget_cfg(cfg: Any, daily_cap_usd: float) -> Any:
    """Allow a one-time pre-launch measurement above the stream's daily cap.

    Refused while the reply stream is enabled, because then the daily cap is
    protecting time-sensitive production reads. The monthly cap is untouched.
    """
    if cfg.x_detection.enabled:
        raise SystemExit(
            "bootstrap audit refused: x_detection is enabled, so the daily "
            "read cap belongs to the live reply stream"
        )
    daily = int(round(daily_cap_usd * 1_000_000))
    if daily <= 0:
        raise SystemExit("--bootstrap-daily-cap-usd must be positive")
    return replace(
        cfg.x_detection,
        daily_microusd_cap=min(daily, cfg.x_detection.monthly_microusd_cap),
    )


def _merge_bodies(bodies: list[dict[str, Any]]) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    users: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for body in bodies:
        for post in body.get("data") or []:
            post_id = str(post.get("id") or "")
            if post_id and post_id in seen:
                continue
            seen.add(post_id)
            data.append(post)
        for user in (body.get("includes") or {}).get("users", []):
            if isinstance(user, dict):
                users[str(user.get("id"))] = user
    return {"data": data, "includes": {"users": list(users.values())}}


def _rows(body: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    users = {
        str(user.get("id")): str(user.get("username") or "")
        for user in (body.get("includes") or {}).get("users", [])
        if isinstance(user, dict)
    }
    by_handle: dict[str, list[dict[str, Any]]] = {
        handle.casefold(): [] for handle in requested
    }
    canonical: dict[str, str] = {
        handle.casefold(): handle for handle in requested
    }
    for post in body.get("data") or []:
        if not isinstance(post, dict):
            continue
        handle = users.get(str(post.get("author_id")), "")
        key = handle.casefold()
        if key not in by_handle:
            continue
        metrics = post.get("public_metrics") or {}
        note = post.get("note_tweet") or {}
        text = str(
            (note.get("text") if isinstance(note, dict) else "")
            or post.get("text")
            or ""
        ).strip()
        without_urls = URL_RE.sub("", text).strip()
        by_handle[key].append({
            "id": str(post.get("id") or ""),
            "created_at": str(post.get("created_at") or ""),
            "views": int(metrics.get("impression_count") or 0),
            "replies": int(metrics.get("reply_count") or 0),
            "text": text,
            "topical_signal": bool(TOPICAL_RE.search(without_urls)),
            "forbidden_signal": bool(FORBIDDEN_RE.search(without_urls)),
            "link_only": not bool(without_urls),
        })

    rows: list[dict[str, Any]] = []
    for key, posts in by_handle.items():
        posts.sort(key=lambda post: post["created_at"], reverse=True)
        views = [int(post["views"]) for post in posts]
        relevant = [
            post for post in posts
            if post["topical_signal"]
            and not post["forbidden_signal"]
            and not post["link_only"]
        ]
        rows.append({
            "handle": canonical[key],
            "sample_posts": len(posts),
            "median_views": statistics.median(views) if views else 0,
            "median_replies": statistics.median(
                [int(post["replies"]) for post in posts]
            ) if posts else 0,
            "topical_posts": sum(post["topical_signal"] for post in posts),
            "forbidden_posts": sum(post["forbidden_signal"] for post in posts),
            "link_only_posts": sum(post["link_only"] for post in posts),
            "relevant_original_posts": len(relevant),
            "relevant_original_ratio": round(len(relevant) / len(posts), 4)
            if posts else 0,
            "verified_100k_median": (
                len(posts) >= 3 and statistics.median(views) >= 100_000
            ),
            "posts": posts,
        })
    rows.sort(
        key=lambda row: (
            not row["verified_100k_median"],
            -row["relevant_original_ratio"],
            -row["median_views"],
            row["handle"].casefold(),
        )
    )
    return rows


def operator_blocks_audit(cfg: Any, state: State, now: float | None = None) -> bool:
    """A paid audit waits while the reply stream may be reading. /always only
    reads inside human_approval.shift_hours_utc, so outside that window the
    stream's reads are not at stake (a timed /on shift still blocks)."""
    if cfg.human_approval._window() is not None:
        state.shift_window = cfg.human_approval.in_shift_window
    return state.operator_is_active(now)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    require_paid_audit_opt_in(args.allow_production_read_debit)
    cfg = load()
    state = State(args.state_db)
    if operator_blocks_audit(cfg, state):
        raise SystemExit(
            "paid official-X reach audit refused while the Telegram operator "
            "window is active"
        )
    budget_cfg = cfg.x_detection
    if args.bootstrap_daily_cap_usd is not None:
        bootstrapped = bootstrap_budget_cfg(cfg, args.bootstrap_daily_cap_usd)
        budget_cfg = replace(bootstrapped, measurement_daily_microusd_cap=bootstrapped.daily_microusd_cap)
    # A reach audit is measurement: its own daily lane, booked as
    # measure:<handle>, so it never closes the reply stream for the day.
    budget = XReadBudget(budget_cfg, state, lane="measurement")
    requested = _handles(args.handles)
    now = datetime.now(timezone.utc)
    window_start, window_end = audit_window(now, args.minimum_post_age_hours)
    if args.per_handle:
        queries = [(handle, build_handle_query(handle)) for handle in requested]
    else:
        queries = [(None, build_query(requested))]

    def available_reads() -> int:
        snap = budget.assert_can_connect()
        return min(
            (snap.daily_cap_microusd - snap.today_microusd)
            // budget_cfg.post_read_microusd,
            (snap.monthly_cap_microusd - snap.month_microusd)
            // budget_cfg.post_read_microusd,
        )

    bodies: list[dict[str, Any]] = []
    audited: list[str] = []
    unaudited: list[str] = []
    charged = 0
    stop_reason = ""
    async with OfficialXStream(cfg) as api:
        for handle, query in queries:
            if stop_reason:
                unaudited.extend([handle] if handle else requested)
                continue
            try:
                room = available_reads()
            except XReadBudgetOpen as exc:
                room, stop_reason = 0, str(exc)
            if room < args.max_results:
                stop_reason = stop_reason or (
                    f"only {room} reads remain inside the hard ledger caps; "
                    f"a query may return {args.max_results}"
                )
                if not args.per_handle:
                    raise XReadBudgetOpen(stop_reason)
                unaudited.extend([handle] if handle else requested)
                continue
            body = await api._json(
                "GET",
                SEARCH_URL,
                params={
                    "query": query,
                    "max_results": args.max_results,
                    "start_time": _iso(window_start),
                    "end_time": _iso(window_end),
                    "tweet.fields": (
                        "created_at,author_id,public_metrics,referenced_tweets,"
                        "note_tweet"
                    ),
                    "expansions": "author_id",
                    "user.fields": "username",
                },
            )
            users = {
                str(user.get("id")): str(user.get("username") or "")
                for user in (body.get("includes") or {}).get("users", [])
                if isinstance(user, dict)
            }
            for post in body.get("data") or []:
                snapshot = budget.record(
                    post_id=str(post.get("id") or ""),
                    author=users.get(str(post.get("author_id")), ""),
                )
                charged += int(snapshot.charged)
            bodies.append(body)
            audited.extend([handle] if handle else requested)

    merged = _merge_bodies(bodies)
    rows = _rows(merged, audited) if audited else []
    result = {
        "generated_at": now.isoformat(),
        "source": "official X recent search; replies and retweets excluded",
        "mode": "per_handle" if args.per_handle else "combined",
        "max_results": args.max_results,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "requested_accounts": len(requested),
        "audited_accounts": len(audited),
        "unaudited_handles": unaudited,
        "stop_reason": stop_reason,
        "returned_posts": len(merged["data"]),
        "newly_charged_reads": charged,
        "verified_100k_count": sum(row["verified_100k_median"] for row in rows),
        "rows": rows,
    }
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("handles", nargs="*")
    parser.add_argument(
        "--handles-file",
        type=Path,
        help="Newline-separated handles (for example from build_reply_pool.py handles).",
    )
    parser.add_argument(
        "--per-handle",
        action="store_true",
        help="One query per handle so every candidate gets its own sample.",
    )
    parser.add_argument(
        "--bootstrap-daily-cap-usd",
        type=float,
        default=None,
        help=(
            "Pre-launch only (refused while x_detection.enabled): daily read "
            "ceiling for this audit. The monthly cap still applies."
        ),
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--state-db", default="state.db")
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Posts per query: default 10 with --per-handle, else 50.",
    )
    parser.add_argument(
        "--allow-production-read-debit",
        action="store_true",
        help=(
            "Emergency-only acknowledgement that returned search Posts debit "
            "the same daily ledger used by the live reply stream."
        ),
    )
    parser.add_argument(
        "--minimum-post-age-hours",
        type=float,
        default=24,
        help="Exclude very fresh posts whose impression totals are still immature.",
    )
    args = parser.parse_args()
    if args.handles_file:
        args.handles = list(args.handles) + [
            line.strip()
            for line in args.handles_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not args.handles:
        parser.error("give handles or --handles-file")
    if args.max_results is None:
        args.max_results = 10 if args.per_handle else 50
    if not 10 <= args.max_results <= 100:
        parser.error("--max-results must be between 10 and 100")
    if args.minimum_post_age_hours < 0:
        parser.error("--minimum-post-age-hours must be non-negative")
    return args


def main() -> int:
    result = asyncio.run(run(parse_args()))
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
