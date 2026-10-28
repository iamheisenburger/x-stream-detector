"""A local read ledger that mirrors X's billing and closes the stream at the caps."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from .settings import Budget


class BudgetClosed(RuntimeError):
    """The stream must stay disconnected: a cap is reached or the price is unchecked."""


@dataclass(frozen=True)
class ReadSnapshot:
    charged: int
    today_microusd: int
    month_microusd: int
    exhausted: bool


class ReadLedger:
    """X bills each post once per UTC day, so a post is booked once per day.
    Every delivered post is booked before anything else happens to it."""

    def __init__(self, path: str, budget: Budget):
        self.budget = budget
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS reads(
                post_id TEXT NOT NULL,
                day TEXT NOT NULL,
                month TEXT NOT NULL,
                cost_microusd INTEGER NOT NULL,
                at REAL NOT NULL,
                PRIMARY KEY(post_id, day)
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_reads_month ON reads(month)")
        self.db.commit()

    @staticmethod
    def _day(now: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(now))

    def today_microusd(self, now: float | None = None) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd), 0) FROM reads WHERE day=?", (self._day(now or time.time()),)
        ).fetchone()
        return int(row[0])

    def month_microusd(self, now: float | None = None) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd), 0) FROM reads WHERE month=?",
            (self._day(now or time.time())[:7],),
        ).fetchone()
        return int(row[0])

    def reads_today(self, now: float | None = None) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM reads WHERE day=?", (self._day(now or time.time()),)).fetchone()
        return int(row[0])

    def _check_pricing(self, now: float) -> None:
        today = datetime.fromtimestamp(now, timezone.utc).date()
        if today > date.fromisoformat(self.budget.pricing_valid_through):
            raise BudgetClosed(
                "the X read price was last checked for "
                f"{self.budget.pricing_valid_through}; check X's rate card and update budget.pricing_valid_through"
            )

    def _snapshot(self, now: float, charged: int) -> ReadSnapshot:
        today, month = self.today_microusd(now), self.month_microusd(now)
        return ReadSnapshot(
            charged=charged,
            today_microusd=today,
            month_microusd=month,
            exhausted=today >= self.budget.daily_cap_microusd or month >= self.budget.monthly_cap_microusd,
        )

    def assert_can_connect(self, now: float | None = None) -> ReadSnapshot:
        now = now or time.time()
        self._check_pricing(now)
        snap = self._snapshot(now, 0)
        price = self.budget.post_read_microusd
        if snap.today_microusd + price > self.budget.daily_cap_microusd:
            raise BudgetClosed("the daily read cap has no room for another post")
        if snap.month_microusd + price > self.budget.monthly_cap_microusd:
            raise BudgetClosed("the monthly read cap has no room for another post")
        return snap

    def record(self, post_ids: Iterable[str], now: float | None = None) -> ReadSnapshot:
        now = now or time.time()
        day = self._day(now)
        charged = 0
        for post_id in dict.fromkeys(str(p) for p in post_ids if str(p)):
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO reads(post_id, day, month, cost_microusd, at) VALUES (?,?,?,?,?)",
                (post_id, day, day[:7], self.budget.post_read_microusd, now),
            )
            charged += cursor.rowcount
        self.db.commit()
        return self._snapshot(now, charged)
