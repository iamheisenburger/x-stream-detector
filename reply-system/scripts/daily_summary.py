#!/usr/bin/env python3
"""Morning Telegram summary of the last 24 hours (sent at 00:55 UTC / 06:25 IST)."""
from __future__ import annotations

import asyncio
import calendar
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def draft_rate_line(
    c: sqlite3.Connection,
    now: float,
    one_off_reads_by_day: dict[str, int] | None = None,
    days: int = 3,
) -> str:
    """Drafts per post the stream delivered, for each of the last full UTC
    days (the R2 exit: at least 40% over 3 days). Measurement reads and a
    day's one-off audit reads are not posts delivered for drafting."""
    parts = []
    for back in range(days, 0, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(now - back * 86_400))
        reads = c.execute(
            "SELECT COUNT(*) FROM x_stream_reads WHERE day=? AND author NOT LIKE 'measure:%'", (day,)
        ).fetchone()[0]
        reads = max(0, reads - int((one_off_reads_by_day or {}).get(day, 0)))
        drafts = c.execute(
            "SELECT COUNT(*) FROM reply_drafts WHERE day=? AND status IN ('notified','observed')", (day,)
        ).fetchone()[0]
        label = time.strftime("%b %d", time.gmtime(now - back * 86_400))
        parts.append(f"{label} {round(100 * drafts / reads)}% ({drafts}/{reads})" if reads else f"{label} no posts")
    return "Draft rate (target 40%): " + ", ".join(parts)


def targets_line(report_path: str) -> str | None:
    """Vision checklist A5: how much of the daily read budget the targets use."""
    import json

    try:
        report = json.loads(Path(report_path).read_text())
    except (OSError, ValueError):
        return None
    target = float(report.get("dailyPaidReadTarget") or 0)
    expected = float(report.get("expectedPaidReads") or 0)
    ai = float((report.get("categoryReads") or {}).get("AI and automation") or 0)
    if not target:
        return None
    return (f"Targets: {report.get('activeTargetCount')} accounts, {expected:.0f} of {target:.0f} daily reads "
            f"({round(100 * expected / target)}%), AI {round(100 * ai / target)}% (pass: 85%+ used, AI 25% or less)")


def standing_line(db_path: str, now: float) -> str | None:
    """Vision checklist D6 (Oct 23 2026): X limited the account on Oct 22
    (Sorsa: search ban; replies fell from a median 67 views to 5). Reply reach
    is the recovery signal, and the speed is how long after the original post
    each reply went up."""
    c = sqlite3.connect(db_path)
    try:
        try:
            rows = c.execute(
                """SELECT r.impressions, r.posted_at - d.source_created_at FROM reply_results r
                   LEFT JOIN reply_drafts d ON d.id = r.draft_id
                   WHERE r.posted_at >= ? AND r.impressions IS NOT NULL""",
                (now - 86_400,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        # Oct 23 2026: Arshad paused replies while the account is limited; the
        # newest posts' views are then the recovery signal (24 before the limit).
        try:
            posts = [] if rows else c.execute(
                """SELECT posted_at, impressions FROM post_results
                   WHERE posted_at >= ? AND impressions IS NOT NULL ORDER BY posted_at DESC LIMIT 2""",
                (now - 3 * 86_400,),
            ).fetchall()
        except sqlite3.OperationalError:
            posts = []
    finally:
        c.close()
    if not rows and posts:
        shown = ", ".join(f"{time.strftime('%b %d %H:%M', time.gmtime(at))} {int(views)}" for at, views in reversed(posts))
        return (f"Standing (D6): no replies posted in 24h; newest posts {shown} views "
                "(limited since Oct 22, recovered at 20+)")
    if not rows:
        return None
    views = sorted(int(r[0]) for r in rows)
    speeds = sorted(float(r[1]) for r in rows if r[1] is not None and r[1] >= 0)
    line = (f"Standing (D6): replies median {views[len(views) // 2]} views ({len(views)} in 24h; "
            "limited since Oct 22, recovered at 50+)")
    if speeds:
        line += f"; posted a median {speeds[len(speeds) // 2]:.0f} s after the original"
    return line


def build_summary(
    db_path: str,
    now: float,
    month_cap_usd: float,
    one_off_read_microusd: int = 0,
    read_price_microusd: int = 5000,
    one_off_reads_by_day: dict[str, int] | None = None,
    targets_report: str | None = None,
) -> str:
    """one_off_read_microusd: a one-off measurement booked inside the window
    (x_detection.incident_daily_baseline_*), reported apart from the stream.
    one_off_reads_by_day: the same, as post counts per UTC day, for the
    draft-rate trend."""
    since = now - 86_400
    c = sqlite3.connect(db_path)
    drafts = c.execute(
        "SELECT status, created_at-source_created_at, author FROM reply_drafts WHERE created_at>=?",
        (since,),
    ).fetchall()
    outcomes = Counter(
        r[0] for r in c.execute(
            "SELECT outcome FROM generation_outcomes WHERE kind='reply' AND at>=?", (since,)
        )
    )
    skip_reasons = Counter(
        str(r[0])[:60] for r in c.execute(
            """SELECT reason FROM generation_outcomes WHERE kind='reply' AND at>=?
               AND outcome IN ('quality_rejected','deterministic_rejected')""",
            (since,),
        )
    )
    reads, read_cost = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_microusd),0) FROM x_stream_reads WHERE at>=?", (since,)
    ).fetchone()
    llm_cost = c.execute(
        "SELECT COALESCE(SUM(cost_microusd),0) FROM llm_usage WHERE at>=?", (since,)
    ).fetchone()[0]
    month = time.strftime("%Y-%m", time.gmtime(now))
    month_spend = (
        c.execute("SELECT COALESCE(SUM(cost_microusd),0) FROM x_stream_reads WHERE substr(day,1,7)=?", (month,)).fetchone()[0]
        + c.execute("SELECT COALESCE(SUM(cost_microusd),0) FROM llm_usage WHERE substr(day,1,7)=?", (month,)).fetchone()[0]
    ) / 1e6
    latencies = sorted(float(d[1]) for d in drafts if d[1] is not None)
    sent = sum(d[0] == "notified" for d in drafts)
    observed = sum(d[0] == "observed" for d in drafts)
    lines = [
        "Pentra replies: last 24 hours",
        f"Drafts: {sent} sent" + (f", {observed} observed (not sent)" if observed else ""),
        f"Posts caught: {sum(outcomes.values())} ({', '.join(f'{k.replace(chr(95), chr(32))} {v}' for k, v in outcomes.most_common())})" if outcomes else "Posts caught: 0",
        draft_rate_line(c, now, one_off_reads_by_day),
    ]
    if latencies:
        lines.append(f"Speed post → draft: median {latencies[len(latencies)//2]:.0f}s, slowest {latencies[-1]:.0f}s")
    if skip_reasons:
        lines.append("Top skip reasons: " + "; ".join(f"{r} ({n})" for r, n in skip_reasons.most_common(3)))
    top = Counter(d[2] for d in drafts).most_common(5)
    if top:
        lines.append("Drafted under: " + ", ".join(f"@{a}" for a, _ in top))
    from scripts.track_posted_replies import posted_summary, posts_summary
    posted_line = posted_summary(db_path, since, now)
    if posted_line:
        lines.append(posted_line)
    posts_line = posts_summary(db_path, now)
    if posts_line:
        lines.append(posts_line)
    standing = standing_line(db_path, now)
    if standing:
        lines.append(standing)
    targets = targets_line(targets_report) if targets_report else None
    if targets:
        lines.append(targets)
    # Vision checklist B1: both daily slots publish on their own.
    published = c.execute("SELECT COUNT(*) FROM actions WHERE kind='post' AND at>=?", (since,)).fetchone()[0]
    # News posts (Oct 24 2026) are extra to the two slots.
    try:
        news = c.execute("SELECT COUNT(*) FROM news_items WHERE status='published' AND updated_at>=?",
                         (since,)).fetchone()[0]
    except sqlite3.OperationalError:
        news = 0
    lines.append(f"Original posts published (24h): {published - news} of 2 slots"
                 + (f", plus {news} news post{'' if news == 1 else 's'}" if news else ""))
    one_off = max(0, min(int(one_off_read_microusd or 0), int(read_cost)))
    one_off_reads = one_off // max(1, int(read_price_microusd)) if one_off else 0
    stream_reads = max(0, reads - one_off_reads)
    one_off_note = (
        f" + one-off measurement ${one_off/1e6:.2f} ({one_off_reads})" if one_off_reads else ""
    )
    lines.append(
        f"Spend: X reads ${(read_cost - one_off)/1e6:.2f} ({stream_reads}){one_off_note}, "
        f"Claude ${llm_cost/1e6:.2f}; month so far ${month_spend:.2f} of ${month_cap_usd:.0f}"
    )
    return "\n".join(lines)


def record_followers(db_path: str, followers: int, now: float) -> str:
    """Store today's follower count; the line says the change since the last
    day's count (Oct 20 2026: we measured impressions and guessed at follows)."""
    c = sqlite3.connect(db_path)
    try:
        c.execute(
            "CREATE TABLE IF NOT EXISTS follower_counts(day TEXT PRIMARY KEY, followers INTEGER NOT NULL, at REAL NOT NULL)"
        )
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        before = c.execute(
            "SELECT followers FROM follower_counts WHERE day<? ORDER BY day DESC LIMIT 1", (day,)
        ).fetchone()
        c.execute(
            "INSERT OR REPLACE INTO follower_counts(day,followers,at) VALUES(?,?,?)", (day, int(followers), now)
        )
        c.commit()
    finally:
        c.close()
    if before is None:
        return f"Followers: {followers}"
    return f"Followers: {followers} ({followers - int(before[0]):+d} since the last summary)"


async def fetch_follower_count(cfg, username: str) -> int:
    from bot.x_stream import X_API_ROOT, OfficialXStream

    async with OfficialXStream(cfg) as api:
        body = await api._json(
            "GET", f"{X_API_ROOT}/2/users/by/username/{username}", params={"user.fields": "public_metrics"}
        )
    return int(body["data"]["public_metrics"]["followers_count"])


def directories_line(state) -> str | None:
    """Grok's latest directory digest in one line (Oct 21 2026)."""
    digest = state.latest_grok_digest()
    if not digest:
        return None

    def names(items) -> list[str]:
        return [str(item.get("directory") if isinstance(item, dict) else item) for item in items or []]

    live, submitted, rejected = names(digest.get("live")), names(digest.get("submitted")), names(digest.get("rejected"))
    line = (f"Directories (Grok, {digest.get('date') or 'undated'}): {len(live)} live, "
            f"{len(submitted)} submitted, {len(rejected)} rejected")
    return line + (f"; live: {', '.join(live[:6])}" if live else "")


# One user lookup a day, booked as a measurement read at twice a post read
# (user reads are priced separately; this never under-counts).
FOLLOWER_LOOKUP_MICROUSD = 10_000


async def follower_line(cfg, state, db_path: str, now: float, fetch=fetch_follower_count) -> str:
    username = str(cfg.publishing.expected_username or "").lstrip("@")
    try:
        followers = await fetch(cfg, username)
    except Exception as exc:
        return f"Followers: unavailable ({type(exc).__name__}: {str(exc)[:80]})"
    state.record_x_stream_read(
        post_id=f"user:{username}", author=f"measure:{username}", cost_microusd=FOLLOWER_LOOKUP_MICROUSD, now=now
    )
    return record_followers(db_path, followers, now)


def price_pin_warning(valid_through: dict[str, str], today: date, days: int = 7) -> str | None:
    """Each paid lane stops the day after its pricing_valid_through (Oct 19
    2026: all three pins ran out on Oct 31 with nothing to say so). Warn a
    week ahead, and every day after an expiry."""
    left = {lane: (date.fromisoformat(day) - today).days for lane, day in valid_through.items()}
    due = sorted(lane for lane, n in left.items() if n <= days)
    if not due:
        return None
    soonest = min(left[lane] for lane in due)
    day = min(valid_through[lane] for lane in due)
    if soonest < 0:
        return (f"PRICE PINS EXPIRED {day} ({', '.join(due)}): those lanes are stopped. "
                "Re-check X and Claude prices, then renew pricing_valid_through in config.json.")
    return (f"Price pins expire {day} (in {soonest} day{'' if soonest == 1 else 's'}; {', '.join(due)}): "
            "re-check X and Claude prices and renew pricing_valid_through in config.json, or those lanes stop.")


async def main() -> int:
    from bot.config import load
    from bot.telegram_approval import TelegramApprovalClient

    cfg = load()
    now = time.time()
    x = cfg.x_detection
    one_off = 0
    one_off_by_day = {}
    if x.incident_daily_baseline_day:
        day_start = calendar.timegm(time.strptime(x.incident_daily_baseline_day, "%Y-%m-%d"))
        if now - 86_400 <= day_start + 86_400 and day_start <= now:
            one_off = int(x.incident_daily_baseline_microusd or 0)
        one_off_by_day[x.incident_daily_baseline_day] = (
            int(x.incident_daily_baseline_microusd or 0) // max(1, int(x.post_read_microusd or 5000))
        )
    text = build_summary(
        "state.db", now, cfg.monthly_ceiling_microusd / 1e6 or 35,
        one_off_read_microusd=one_off, read_price_microusd=int(x.post_read_microusd or 5000),
        one_off_reads_by_day=one_off_by_day,
        targets_report="research/output/active-x-reply-targets.json",
    )
    warning = price_pin_warning({
        "x_detection": x.pricing_valid_through,
        "generation": cfg.generation.pricing_valid_through,
        "publishing": cfg.publishing.pricing_valid_through,
    }, date.today())
    if warning:
        text = f"{text}\n{warning}"
    from bot.state import State
    text = f"{text}\n" + await follower_line(cfg, State("state.db"), "state.db", now)
    directories = directories_line(State("state.db"))
    if directories:
        text = f"{text}\n{directories}"
    await TelegramApprovalClient(cfg).send_message(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
