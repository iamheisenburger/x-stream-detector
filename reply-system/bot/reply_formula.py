"""Pentra's reply-guy formula: which accounts to reply under, and in what order.

Speed comes first: the filtered stream detects a post within seconds, so a
Pentra reply lands before the crowd arrives. A post's final reply count
therefore says nothing about how early we are and is NOT used. What decides
the value of being first is the impressions the post gets, which is driven by
the creator's audience:

    opportunity(a)       = median_views_per_original_post(a)
    daily_opportunity(a) = on_topic_posts_per_day(a) * opportunity(a)

Every account must first pass the gates (active, on Pentra's topics, clean
feed, measured on >= 3 recent originals). Among those, the reach floor is the
HIGHEST floor at which the pool still supplies enough on-topic posts for the
daily draft target:

    required_posts_per_day = daily_draft_target / assumed_draft_rate
    reach_floor = max{ f in reach_floors :
                       sum(on_topic_posts_per_day(a) for a with median_views(a) >= f)
                       >= required_posts_per_day }

So the pool is as big-account as the volume target allows, and the daily read
budget is spent on the highest-opportunity accounts first.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any, Iterable


# Owner rule: the target pool's reach floor never goes below 40K median views
# per original post, whatever volume a lower step would supply.
MIN_REACH_FLOOR = 40_000


@dataclass(frozen=True)
class ReplyFormula:
    daily_draft_target: int = 35
    assumed_draft_rate: float = 0.40
    reach_floors: tuple = field(default_factory=lambda: (
        100_000, 75_000, 50_000, 40_000,
    ))
    min_posts_per_day: float = 0.5
    min_on_topic_share: float = 0.30
    max_off_lane_share: float = 0.20
    min_sample_posts: int = 3

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "ReplyFormula":
        raw = raw or {}
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}
        if "reach_floors" in values:
            floors = sorted({max(float(v), MIN_REACH_FLOOR) for v in values["reach_floors"]}, reverse=True)
            values["reach_floors"] = tuple(floors or [MIN_REACH_FLOOR])
        return cls(**values)

    @property
    def required_posts_per_day(self) -> float:
        return self.daily_draft_target / max(self.assumed_draft_rate, 1e-9)

    @property
    def lowest_floor(self) -> float:
        return float(min(self.reach_floors))


def opportunity(median_views: float, _median_replies: float = 0.0, _formula: "ReplyFormula | None" = None) -> float:
    """Impressions per post. Reply counts are ignored: we reply first."""
    return float(median_views)


def opportunity_component(value: float) -> float:
    """1K expected impressions per reply scores 0, 1M scores 1 (log scale)."""
    return max(0.0, min(1.0, math.log10(max(value, 1.0) / 1_000) / 3))


def gate_failure(
    *,
    sample_posts: int,
    posts_per_day: float,
    on_topic_share: float,
    off_lane_share: float,
    formula: ReplyFormula,
) -> str:
    if sample_posts < formula.min_sample_posts:
        return f"only {sample_posts} original posts measured"
    if posts_per_day < formula.min_posts_per_day:
        return f"inactive: {posts_per_day:.2f} original posts/day"
    if on_topic_share < formula.min_on_topic_share:
        return f"off-topic: {on_topic_share:.2f} of posts on Pentra topics"
    if off_lane_share > formula.max_off_lane_share:
        return f"off-lane share {off_lane_share:.2f}"
    return ""


def choose_reach_floor(
    accounts: Iterable[tuple[float, float]],
    formula: ReplyFormula,
) -> dict[str, Any]:
    """accounts: (median_views, on_topic_posts_per_day) for gate-passing accounts."""
    accounts = list(accounts)
    required = formula.required_posts_per_day
    supply = 0.0
    for floor in sorted(formula.reach_floors, reverse=True):
        supply = sum(ppd for views, ppd in accounts if views >= floor)
        if supply >= required:
            return {"floor": float(floor), "supply": round(supply, 2),
                    "required": round(required, 2), "meetsVolume": True}
    return {"floor": formula.lowest_floor, "supply": round(supply, 2),
            "required": round(required, 2), "meetsVolume": False}
