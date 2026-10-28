"""The always-on loop: active hours, read caps, back-off and X's 429s."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from .budget import BudgetClosed, ReadLedger
from .client import CreditsDepleted, RateLimited, StreamError, XStreamClient
from .rules import canonical_rule_specs
from .settings import Settings

log = logging.getLogger("xstream")

OnPost = Callable[[dict], Awaitable[None]]

# On the third connection-limit 429 in a row (about 90 seconds of waiting),
# ask X which connections it still counts for this app and end them.
STALE_CONNECTION_CHECK_STREAK = 2
CREDITS_RETRY_SECONDS = 300.0


def rate_limit_delay(exc: RateLimited, *, now: float, streak: int) -> float:
    """A genuine rate limit waits for X's reset time. The connection-limit 429
    only means X has not released the connection that just dropped, so retry
    after 30s, 60s, 120s... (never later than the reset) instead of going
    blind for up to fifteen minutes."""
    reset_delay = max(60.0, exc.retry_at - now + 5.0)
    if not exc.connection_limit:
        return reset_delay
    return min(reset_delay, 30.0 * 2 ** min(max(0, streak), 4))


def interruption_delay(exc: BaseException, backoff: float) -> float:
    """A dropped transport is still held open on X's side for a few seconds;
    reconnecting after one second is refused as a second connection."""
    if "transport interrupted" in str(exc):
        return max(float(backoff), 10.0)
    return float(backoff)


def seconds_until_open(now: float, window: tuple[int, int] | None) -> float:
    """0 inside the active hours (or with none), else seconds until they start."""
    if window is None:
        return 0.0
    start, end = window[0] * 3600, window[1] * 3600
    clock = now % 86_400
    if start < end:
        return 0.0 if start <= clock < end else (start - clock) % 86_400
    return 0.0 if clock >= start or clock < end else start - clock


def seconds_until_close(now: float, window: tuple[int, int] | None) -> float | None:
    if window is None:
        return None
    return (window[1] * 3600 - now % 86_400) % 86_400 or 86_400.0


def seconds_until_utc_reset(now: float) -> float:
    return 86_400 - now % 86_400 + 5.0


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Detector:
    def __init__(
        self,
        settings: Settings,
        ledger: ReadLedger,
        *,
        client_factory: Callable[[Settings], XStreamClient] = XStreamClient,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        self.ledger = ledger
        self.client_factory = client_factory
        self.clock = clock
        self.sleep = sleep
        self.specs = canonical_rule_specs(
            settings.handles,
            settings.rule_namespace,
            keywords=settings.keywords,
            extra_rules=settings.extra_rules,
            lang=settings.lang,
            include_replies=settings.include_replies,
        )
        self._reconnects: deque[float] = deque()
        self._deliveries: set[asyncio.Task] = set()

    def _reconnect_wait(self) -> float:
        """Seconds to hold off so reconnects stay under the hourly ceiling."""
        now = self.clock()
        while self._reconnects and self._reconnects[0] < now - 3600:
            self._reconnects.popleft()
        self._reconnects.append(now)
        if len(self._reconnects) <= self.settings.max_reconnects_per_hour:
            return 0.0
        wait = self._reconnects[0] + 3600 - now
        log.error("%d reconnects in the last hour; holding off %s", len(self._reconnects), _duration(wait))
        return wait

    async def _deliver(self, on_post: OnPost, event: dict) -> None:
        try:
            await on_post(event)
        except Exception:
            log.exception("handler failed for post %s", event.get("id"))

    async def _consume(self, api: XStreamClient, on_post: OnPost) -> str:
        async for event in api.events():
            # Book the read before anything else, so a crash can never hide a cost.
            snap = self.ledger.record([event["id"], *event.get("included_post_ids", [])], now=self.clock())
            created = event.get("created_at_epoch")
            lag = f"{self.clock() - created:.1f}s after posting" if created else "post time unknown"
            log.info(
                "post @%s %s | %s | reads today $%.3f",
                event["author"]["username"] or "?",
                event["id"],
                lag,
                snap.today_microusd / 1_000_000,
            )
            # The stream reader never waits on the handler.
            task = asyncio.create_task(self._deliver(on_post, event))
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)
            if snap.exhausted:
                log.warning("read cap reached; disconnecting until the UTC reset")
                return "budget"
        return "stream-ended"

    async def session(self, on_post: OnPost) -> str:
        """One connection, from rule sync to disconnect. Returns why it ended."""
        self.ledger.assert_can_connect(self.clock())
        async with self.client_factory(self.settings) as api:
            await api.sync_rules(self.specs)
            log.info("stream connected | %d rule(s) | %d handle(s)", len(self.specs), len(self.settings.handles))
            consume = asyncio.create_task(self._consume(api, on_post))
            close_in = seconds_until_close(self.clock(), self.settings.active_hours_utc)
            timer = asyncio.create_task(asyncio.sleep(close_in)) if close_in is not None else None
            waiting = {consume} | ({timer} if timer else set())
            try:
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiting:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            if consume not in done:
                return "window-closed"
            return consume.result()

    async def _end_stale_connections(self) -> int:
        try:
            async with self.client_factory(self.settings) as api:
                return await api.end_stale_connections()
        except Exception as exc:  # the next reconnect simply retries
            log.warning("could not check for stale connections: %s", exc)
            return 0

    async def run(self, on_post: OnPost) -> None:
        backoff = 1.0
        streak = 0
        while True:
            closed_for = seconds_until_open(self.clock(), self.settings.active_hours_utc)
            if closed_for > 0:
                log.info("outside active hours; stream off for %s", _duration(closed_for))
                await self.sleep(closed_for)
                continue
            try:
                reason = await self.session(on_post)
            except BudgetClosed as exc:
                wait = seconds_until_utc_reset(self.clock())
                log.warning("%s; stream off for %s", exc, _duration(wait))
                await self.sleep(wait)
                continue
            except CreditsDepleted as exc:
                log.warning("%s; retrying in %s", exc, _duration(CREDITS_RETRY_SECONDS))
                await self.sleep(CREDITS_RETRY_SECONDS)
                continue
            except RateLimited as exc:
                delay = rate_limit_delay(exc, now=self.clock(), streak=streak)
                if exc.connection_limit and streak == STALE_CONNECTION_CHECK_STREAK:
                    ended = await self._end_stale_connections()
                    if ended:
                        log.info("ended %d stale connection(s) X still counted for this app", ended)
                        delay = min(delay, 10.0)
                if exc.connection_limit:
                    streak += 1
                log.warning("%s; retrying in %s", exc, _duration(delay))
                await self.sleep(delay)
                continue
            except (StreamError, OSError, asyncio.TimeoutError) as exc:
                streak = 0
                delay = max(interruption_delay(exc, backoff), self._reconnect_wait())
                log.warning("stream interrupted: %s; retrying in %s", exc, _duration(delay))
                await self.sleep(delay)
                backoff = min(backoff * 2, self.settings.reconnect_max_seconds)
                continue
            backoff, streak = 1.0, 0
            if reason == "budget":
                await self.sleep(seconds_until_utc_reset(self.clock()))
            elif reason == "window-closed":
                log.info("active hours ended; stream disconnected")
            else:
                delay = max(1.0, self._reconnect_wait())
                log.info("stream ended; reconnecting in %s", _duration(delay))
                await self.sleep(delay)
