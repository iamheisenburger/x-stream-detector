"""Meter and stop TwitterAPI.io delivery spend before it can run away."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .config import DetectionBudget
from .skipgate import normalize_tweet, tweet_age_seconds
from .state import State


class CostCircuitOpen(RuntimeError):
    """A provider-spend or delivery-integrity limit was breached."""


@dataclass(frozen=True)
class DeliverySnapshot:
    delivered: int
    classification: str
    bootstrap_stale: int
    connection_replay: int
    reconnects: int
    duplicates: int
    non_originals: int
    stale: int
    unknown_authors: int
    credits_today: int
    credits_month: int


class DeliveryGuard:
    """Persistent spend meter plus fast in-memory anomaly circuit breakers."""

    def __init__(
        self,
        limits: DetectionBudget,
        state: State,
        target_handles: list[str],
        *,
        starting_balance_credits: int,
        started_at: float | None = None,
    ):
        self.limits = limits
        self.state = state
        self.targets = {handle.lstrip("@").lower() for handle in target_handles}
        self.starting_balance_credits = int(starting_balance_credits)
        self.started_at = time.time() if started_at is None else float(started_at)
        self.session_deliveries = 0
        self.bootstrap_stale = 0
        self.connection_replay = 0
        self.reconnects = 0
        self.duplicates = 0
        self.non_originals = 0
        self.stale = 0
        self.unknown_authors = 0
        self._seen: set[str] = set()
        self._minute: deque[float] = deque()
        self._reconnect_times: deque[float] = deque()
        self._connection_started_at = self.started_at
        self._connection_replay = 0
        self._steady_balance: int | None = None
        self._steady_deliveries = 0

    def _trip(self, reason: str) -> None:
        raise CostCircuitOpen(reason)

    def begin_connection(self, *, now: float | None = None) -> None:
        """Start one websocket reconnect without buying another rule lifecycle."""
        now = time.time() if now is None else float(now)
        cutoff = now - 3600
        while self._reconnect_times and self._reconnect_times[0] < cutoff:
            self._reconnect_times.popleft()
        self._reconnect_times.append(now)
        if len(self._reconnect_times) > self.limits.max_reconnects_per_hour:
            self._trip(
                "websocket reconnect cap exceeded: "
                f"{len(self._reconnect_times)} > "
                f"{self.limits.max_reconnects_per_hour} in one hour"
            )
        self.reconnects += 1
        self._connection_started_at = now
        self._connection_replay = 0

    def observe(self, event: dict, *, now: float | None = None) -> DeliverySnapshot:
        now = time.time() if now is None else float(now)
        tweet = normalize_tweet(event)
        tweet_id = tweet["id"]
        author = tweet["author"].lower()
        prior_count = self.state.provider_delivery_count(tweet_id) if tweet_id else 0
        duplicate = bool(tweet_id and (tweet_id in self._seen or prior_count > 0))
        non_original = tweet["is_reply"] or tweet["is_retweet"]
        age = tweet_age_seconds(tweet["created"], now)
        stale = age is not None and age > self.limits.max_delivery_age_seconds
        session_age = now - self.started_at
        bootstrap_stale = (
            stale
            and 0 <= session_age <= self.limits.bootstrap_grace_seconds
        )
        unknown_author = not author or author not in self.targets
        connection_age = now - self._connection_started_at
        reconnect_replay = (
            self.reconnects > 0
            and 0 <= connection_age <= self.limits.reconnect_grace_seconds
            and (duplicate or stale)
        )

        classification = "ok"
        if unknown_author:
            classification = "unknown_author"
        elif non_original:
            classification = "non_original"
        elif reconnect_replay:
            classification = "connection_replay"
        elif bootstrap_stale:
            classification = "bootstrap_replay" if duplicate else "bootstrap_stale"
        elif duplicate:
            classification = "duplicate"
        elif stale:
            classification = "stale"

        self.state.record_provider_delivery(
            tweet_id=tweet_id,
            rule_id=str(event.get("_twitterapi_rule_id") or ""),
            author=author,
            classification=classification,
            credits=self.limits.credits_per_delivery,
            now=now,
        )
        self.session_deliveries += 1
        if tweet_id:
            self._seen.add(tweet_id)
        if classification in {"bootstrap_stale", "bootstrap_replay"}:
            self.bootstrap_stale += 1
        elif classification == "connection_replay":
            self.connection_replay += 1
            self._connection_replay += 1
        elif classification == "duplicate":
            self.duplicates += 1
        if classification == "non_original":
            self.non_originals += 1
        if classification == "stale":
            self.stale += 1
        if classification == "unknown_author":
            self.unknown_authors += 1

        # TwitterAPI.io sends one bounded historical snapshot when a fresh
        # stream attaches. It is paid and metered, but it is not live traffic
        # and must not poison the steady-state rate detector.
        if classification not in {
            "bootstrap_stale",
            "bootstrap_replay",
            "connection_replay",
        }:
            self._minute.append(now)
        cutoff = now - 60
        while self._minute and self._minute[0] < cutoff:
            self._minute.popleft()

        credits_today = self.state.provider_credits_today(now)
        credits_month = self.state.provider_credits_month(now)
        snapshot = DeliverySnapshot(
            delivered=self.session_deliveries,
            classification=classification,
            bootstrap_stale=self.bootstrap_stale,
            connection_replay=self.connection_replay,
            reconnects=self.reconnects,
            duplicates=self.duplicates,
            non_originals=self.non_originals,
            stale=self.stale,
            unknown_authors=self.unknown_authors,
            credits_today=credits_today,
            credits_month=credits_month,
        )

        if credits_today > self.limits.daily_credit_cap:
            self._trip(
                f"daily detection cap exceeded: {credits_today} > "
                f"{self.limits.daily_credit_cap} credits"
            )
        if credits_month > self.limits.monthly_credit_cap:
            self._trip(
                f"monthly detection cap exceeded: {credits_month} > "
                f"{self.limits.monthly_credit_cap} credits"
            )
        if len(self._minute) > self.limits.max_deliveries_per_minute:
            self._trip(
                f"delivery-rate anomaly: {len(self._minute)} events in 60s"
            )
        if self.bootstrap_stale > self.limits.max_bootstrap_stale_deliveries:
            self._trip(
                "startup backfill exceeded bound: "
                f"{self.bootstrap_stale} > "
                f"{self.limits.max_bootstrap_stale_deliveries} deliveries"
            )
        if (
            self._connection_replay
            > self.limits.max_reconnect_replay_deliveries
        ):
            self._trip(
                "reconnect replay exceeded bound: "
                f"{self._connection_replay} > "
                f"{self.limits.max_reconnect_replay_deliveries} deliveries"
            )
        if self.duplicates > self.limits.max_duplicate_deliveries:
            self._trip(f"duplicate replay anomaly: {self.duplicates} duplicate deliveries")
        if self.non_originals > self.limits.max_non_original_deliveries:
            self._trip(
                f"source filter failure: {self.non_originals} replies/retweets delivered"
            )
        if self.stale > self.limits.max_stale_deliveries:
            self._trip(f"backfill anomaly: {self.stale} stale deliveries")
        if self.unknown_authors > self.limits.max_unknown_author_deliveries:
            self._trip(
                f"rule-scope anomaly: {self.unknown_authors} unknown authors delivered"
            )
        if (
            self.session_deliveries >= self.limits.duplicate_ratio_min_deliveries
            and self.duplicates / self.session_deliveries > self.limits.max_duplicate_ratio
        ):
            self._trip(
                f"duplicate ratio anomaly: {self.duplicates}/{self.session_deliveries}"
            )
        return snapshot

    def verify_balance(self, current_balance_credits: int) -> None:
        current = int(current_balance_credits)
        actual = self.state.record_provider_balance(current)
        if current < self.limits.minimum_balance_credits:
            self._trip(
                f"provider balance below reserve: {current} < "
                f"{self.limits.minimum_balance_credits} credits"
            )
        if actual["debits_today"] > self.limits.daily_credit_cap:
            self._trip(
                "authoritative daily provider debit cap exceeded: "
                f"{actual['debits_today']} > {self.limits.daily_credit_cap} credits"
            )
        if actual["debits_month"] > self.limits.monthly_credit_cap:
            self._trip(
                "authoritative monthly provider debit cap exceeded: "
                f"{actual['debits_month']} > {self.limits.monthly_credit_cap} credits"
            )

        session_age = time.time() - self.started_at
        if session_age <= self.limits.startup_settlement_seconds:
            startup_drop = max(0, self.starting_balance_credits - current)
            if startup_drop > self.limits.max_startup_session_debit_credits:
                self._trip(
                    "startup provider debit exceeded bound: "
                    f"{startup_drop} > "
                    f"{self.limits.max_startup_session_debit_credits} credits"
                )
            return

        # Provider setup/backfill charges settle late and variably. Establish one
        # steady-state baseline after that bounded phase, then reconcile strictly.
        if self._steady_balance is None or current > self._steady_balance:
            self._steady_balance = current
            self._steady_deliveries = self.session_deliveries
            return
        actual_drop = max(0, self._steady_balance - current)
        expected_drop = (
            (self.session_deliveries - self._steady_deliveries)
            * self.limits.credits_per_delivery
        )
        unexplained = actual_drop - expected_drop
        if unexplained > self.limits.max_unexplained_credit_drop:
            self._trip(
                f"unexplained provider burn: actual={actual_drop}, "
                f"observed={expected_drop}, unexplained={unexplained} credits"
            )
