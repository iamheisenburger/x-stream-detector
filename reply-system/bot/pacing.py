"""Pacing = the behavioural anti-suspension layer.

Pure functions over (config, state, now) so they are unit-testable without a
live account. Every decision returns (ok, reason) so the daemon can log WHY it
held back.
"""
import random
import time
from datetime import datetime, timezone

from .config import Config
from .state import State


def in_active_hours(cfg: Config, now: float) -> bool:
    hour = datetime.fromtimestamp(now, timezone.utc).hour
    start, end = cfg.pacing.active_hours_utc
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end          # window wraps past midnight


def active_window_bounds(cfg: Config, now: float) -> tuple[float, float] | None:
    """Return the UTC bounds of the active window containing ``now``."""
    dt = datetime.fromtimestamp(now, timezone.utc)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start_hour, end_hour = cfg.pacing.active_hours_utc
    start = midnight + start_hour * 3600
    end = midnight + end_hour * 3600

    if start_hour < end_hour:
        return (start, end) if start <= now < end else None
    if start_hour > end_hour:
        if now >= start:
            return start, end + 86400
        if now < end:
            return start - 86400, end
        return None
    return midnight, midnight + 86400


def daily_cap(cfg: Config, now: float) -> int:
    """Ramped cap: start low, step up every ramp_day_length days since launch."""
    ramp = cfg.pacing.daily_cap_ramp
    if not cfg.started_at_iso:
        return ramp[0]
    try:
        started = datetime.fromisoformat(cfg.started_at_iso).timestamp()
    except ValueError:
        return ramp[0]
    days = max(0, int((now - started) // 86400))
    step = min(days // max(1, cfg.pacing.ramp_day_length), len(ramp) - 1)
    return ramp[step]


def approval_daily_cap(cfg: Config, now: float = None) -> int:
    """Today's ceiling for reply drafts sent for a person to post."""
    approval = cfg.human_approval
    if not approval.ramp_with_pacing:
        return approval.daily_candidate_cap
    return min(approval.daily_candidate_cap, daily_cap(cfg, now or time.time()))


def can_reply(cfg: Config, state: State, author: str, now: float = None):
    now = now or time.time()

    if not in_active_hours(cfg, now):
        return False, "outside active hours"

    cap = daily_cap(cfg, now)
    if state.count_today(now=now) >= cap:
        return False, f"daily cap reached ({cap})"

    gap = now - state.last_action_at()
    if gap < cfg.pacing.min_gap_seconds:
        return False, f"min gap not elapsed ({int(gap)}s < {cfg.pacing.min_gap_seconds}s)"

    since_author = now - state.last_reply_to_author_at(author)
    if since_author < cfg.pacing.per_account_cooldown_seconds:
        return False, f"author cooldown ({author})"

    return True, "clear"


def can_notify_reply(cfg: Config, state: State, author: str, now: float = None):
    """Pace human approval requests independently from public X actions."""
    now = now or time.time()

    approval = cfg.human_approval
    if approval.require_operator_active and not state.operator_is_active(now):
        return False, "operator unavailable"
    if not approval.require_operator_active and not in_active_hours(cfg, now):
        return False, "outside active hours"

    cap = approval_daily_cap(cfg, now)
    if state.reply_draft_count_today(now) >= cap:
        return False, f"daily approval cap reached ({cap})"

    if state.active_reply_draft_count(now) >= approval.max_pending_candidates:
        return False, "too many unexpired approval requests"

    gap = now - state.last_reply_draft_at()
    if gap < approval.min_candidate_gap_seconds:
        return False, (
            "approval gap not elapsed "
            f"({int(gap)}s < {approval.min_candidate_gap_seconds}s)"
        )

    since_author = now - state.last_reply_draft_to_author_at(author)
    if since_author < approval.per_account_cooldown_seconds:
        return False, f"approval author cooldown ({author})"

    return True, "clear"


def selectivity_pass(cfg: Config, author: str, rng: random.Random = random) -> bool:
    """Reach-weighted veto: high-reach accounts pass often, low-reach rarely, so
    the limited daily budget concentrates on the highest-impression opportunities."""
    return rng.random() < cfg.weight_for(author)


def next_gap_seconds(cfg: Config, rng: random.Random = random) -> float:
    return cfg.pacing.min_gap_seconds + rng.random() * cfg.pacing.max_gap_jitter_seconds


def should_standalone_post(
    cfg: Config,
    state: State,
    now: float = None,
    kind: str = "post",
) -> bool:
    """``kind`` is the ledger entry one slot produces: "post" for an original
    published through the API, "post_draft" for one handed to a person."""
    now = now or time.time()
    bounds = active_window_bounds(cfg, now)
    if bounds is None:
        return False
    quota = max(0, int(cfg.pacing.standalone_posts_per_day))
    if quota == 0:
        return False

    start, end = bounds
    posted = state.count_since(kind, start)
    if posted >= quota:
        return False
    if now - state.last_kind_at(kind) < cfg.pacing.standalone_min_gap_seconds:
        return False

    # The first slot opens with the active window; remaining slots are evenly
    # spaced. A restart can catch up at most one post per minimum-gap interval.
    slot_seconds = (end - start) / quota
    due_slots = min(quota, 1 + int((now - start) // slot_seconds))
    return posted < due_slots


def standalone_slot_key(cfg: Config, now: float = None) -> str | None:
    """Stable key for the currently due standalone-post slot."""
    now = now or time.time()
    bounds = active_window_bounds(cfg, now)
    quota = max(0, int(cfg.pacing.standalone_posts_per_day))
    if bounds is None or quota == 0:
        return None
    start, end = bounds
    slot_seconds = (end - start) / quota
    slot_index = min(quota - 1, int((now - start) // slot_seconds))
    return f"{int(start)}:{slot_index}"
