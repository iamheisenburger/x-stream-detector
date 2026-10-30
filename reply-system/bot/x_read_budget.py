"""Hard cost circuit for official X filtered-stream reads."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .config import XDetectionBudget
from .state import MEASUREMENT_AUTHOR_PREFIX, State


class XReadBudgetOpen(RuntimeError):
    """The official X read stream must remain disconnected."""


def read_pace_wait_seconds(*, now: float, reads_used: int, daily_reads: int,
                           window: tuple[int, int] | None, burst: int) -> float:
    """Seconds the stream should pause so the day's reads spread over the
    shift window (0 = read now). Oct 2026: on healthy days the 90-read cap ran
    out by ~19:00 UTC and the evening of a 12:00-23:00 window had no drafts.
    Up to `burst` reads may run ahead of an even pace; outside the window, or
    without one, nothing is paced."""
    if window is None or daily_reads <= 0:
        return 0.0
    start_hour, end_hour = window
    current = datetime.fromtimestamp(now, timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if start_hour < end_hour:
        start, end = midnight + start_hour * 3600, midnight + end_hour * 3600
    elif current.hour >= start_hour:
        start, end = midnight + start_hour * 3600, midnight + 86_400 + end_hour * 3600
    else:
        start, end = midnight - 86_400 + start_hour * 3600, midnight + end_hour * 3600
    if not start <= now < end:
        return 0.0
    rate = daily_reads / (end - start)
    if reads_used < burst + rate * (now - start):
        return 0.0
    return max(60.0, (reads_used - burst + 1) / rate - (now - start))


@dataclass(frozen=True)
class XReadSnapshot:
    charged: bool
    today_microusd: int
    month_microusd: int
    daily_cap_microusd: int
    monthly_cap_microusd: int

    @property
    def exhausted(self) -> bool:
        return (
            self.today_microusd >= self.daily_cap_microusd
            or self.month_microusd >= self.monthly_cap_microusd
        )


class XReadBudget:
    """lane "stream": the reply stream's reads against daily_microusd_cap.
    lane "measurement": measurement tasks, booked as measure:<handle>, against
    measurement_daily_microusd_cap. Both count toward the one monthly cap."""

    def __init__(self, cfg: XDetectionBudget, state: State, lane: str = "stream"):
        if lane not in ("stream", "measurement"):
            raise ValueError(f"unknown X read lane {lane!r}")
        self.cfg = cfg
        self.state = state
        self.lane = lane

    @property
    def daily_cap_microusd(self) -> int:
        return self.cfg.daily_microusd_cap if self.lane == "stream" else self.cfg.measurement_daily_microusd_cap

    def _verify_pricing(self, now: float | None = None) -> None:
        now = now or time.time()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        try:
            valid_through = date.fromisoformat(self.cfg.pricing_valid_through)
        except ValueError as exc:
            raise XReadBudgetOpen("official X read pricing date is invalid") from exc
        if today > valid_through:
            raise XReadBudgetOpen(
                "official X read pricing assumption expired; refresh it before reconnecting"
            )

    def snapshot(self, now: float | None = None) -> XReadSnapshot:
        now = now or time.time()
        raw_today = self.state.x_stream_cost_microusd_today(now, lane=self.lane)
        utc_day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        baseline = (
            max(0, int(self.cfg.incident_daily_baseline_microusd or 0))
            if self.lane == "stream" and self.cfg.incident_daily_baseline_day == utc_day
            else 0
        )
        return XReadSnapshot(
            charged=False,
            today_microusd=max(0, raw_today - baseline),
            month_microusd=self.state.x_stream_cost_microusd_month(now),
            daily_cap_microusd=self.daily_cap_microusd,
            monthly_cap_microusd=self.cfg.monthly_microusd_cap,
        )

    def assert_can_connect(self, now: float | None = None) -> XReadSnapshot:
        now = now or time.time()
        self._verify_pricing(now)
        snapshot = self.snapshot(now)
        next_cost = self.cfg.post_read_microusd
        if snapshot.today_microusd + next_cost > snapshot.daily_cap_microusd:
            raise XReadBudgetOpen(f"official X daily read cap ({self.lane}) has no room for another post")
        if snapshot.month_microusd + next_cost > self.cfg.monthly_microusd_cap:
            raise XReadBudgetOpen("official X monthly read cap has no room for another post")
        return snapshot

    def record(
        self,
        *,
        post_id: str,
        author: str,
        now: float | None = None,
    ) -> XReadSnapshot:
        """Record a delivered post before any generation or filtering work."""
        now = now or time.time()
        self._verify_pricing(now)
        if self.lane == "measurement" and not author.lower().startswith(MEASUREMENT_AUTHOR_PREFIX):
            author = f"{MEASUREMENT_AUTHOR_PREFIX}{author.lstrip('@')}"
        charged = self.state.record_x_stream_read(
            post_id=post_id,
            author=author,
            cost_microusd=self.cfg.post_read_microusd,
            now=now,
        )
        snapshot = self.snapshot(now)
        snapshot = XReadSnapshot(
            charged=charged,
            today_microusd=snapshot.today_microusd,
            month_microusd=snapshot.month_microusd,
            daily_cap_microusd=snapshot.daily_cap_microusd,
            monthly_cap_microusd=snapshot.monthly_cap_microusd,
        )
        if snapshot.today_microusd > snapshot.daily_cap_microusd:
            raise XReadBudgetOpen(f"official X daily read cap ({self.lane}) was exceeded")
        if snapshot.month_microusd > self.cfg.monthly_microusd_cap:
            raise XReadBudgetOpen("official X monthly read cap was exceeded")
        return snapshot
