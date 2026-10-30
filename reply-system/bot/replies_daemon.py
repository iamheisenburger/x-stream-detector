"""Production official-X detection and Telegram reply-approval service."""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .daemon import Daemon, daemon_lock, log
from .dynamic_discovery import DynamicAuthorVerifier
from .llm_budget import GenerationBudgetOpen, ProviderCreditExhausted

CREDIT_RECHECK_SECONDS = 3600.0
from .pacing import approval_daily_cap
from .skipgate import normalize_tweet, should_skip
from .telegram_approval import TelegramApprovalError, parse_artifact_message, parse_shift_command
from .x_read_budget import XReadBudget, XReadBudgetOpen, read_pace_wait_seconds
from .x_stream import (
    X_API_ROOT,
    OfficialXStream,
    XCreditsDepleted,
    XRateLimitError,
    XStreamError,
)

# R3 (Oct 19 2026): our replies get about 0.2% of the source post's
# impressions, so the post's size decides most of a reply's reach. Each sent
# draft's post is read again, free on the UTC day the stream delivered it, at
# these ages (kind, min, max seconds). The first puts the post's live numbers
# on the draft, so the fastest risers get posted first; both are stored in
# source_snapshots.
RISE_CHECKS = (("rise_90s", 90.0, 600.0), ("rise_60m", 3600.0, 4500.0))
RISE_CHECK_POLL_SECONDS = 20.0


def _compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def rise_line(age_seconds: float, metrics: dict) -> str:
    """'Now (1m 32s): 2.4K views · 31 likes · 9 replies'."""
    seconds = int(age_seconds)
    if seconds >= 3600:
        age = f"{seconds // 3600}h {seconds % 3600 // 60}m"
    elif seconds >= 60:
        age = f"{seconds // 60}m {seconds % 60}s"
    else:
        age = f"{seconds}s"
    views = int(metrics.get("impression_count") or 0)
    likes = int(metrics.get("like_count") or 0)
    replies = int(metrics.get("reply_count") or 0)
    return (f"Now ({age}): {_compact(views)} views · {likes} like{'' if likes == 1 else 's'} · "
            f"{replies} repl{'y' if replies == 1 else 'ies'}")


def stream_rate_limit_delay(
    exc: XRateLimitError,
    *,
    now: float,
    connection_limit_streak: int,
) -> float:
    """Seconds to wait after X answered a stream connect with HTTP 429.

    A genuine rate limit waits for X's reset time. The connection-limit 429
    only means X has not yet released the connection that just dropped, so
    Pentra retries after 30s, 60s, 120s... (never later than the reset)
    instead of going blind for up to fifteen minutes."""
    reset_delay = max(60.0, exc.retry_at - now + 5.0)
    if not exc.connection_limit:
        return reset_delay
    return min(reset_delay, 30.0 * 2 ** min(max(0, connection_limit_streak), 4))


# A connection that just dropped is released by X within about a minute. On
# the third connection-limit 429 in a row (about 90 seconds of waiting) the
# daemon asks X which connections it still counts and ends them.
STALE_CONNECTION_CHECK_STREAK = 2


def reconnect_delay_after_interruption(exc: BaseException, backoff: float) -> float:
    """A dropped transport is still held open on X's side for a few seconds;
    reconnecting after one second is refused as a second connection."""
    if "transport interrupted" in str(exc):
        return max(float(backoff), 10.0)
    return float(backoff)


class OfficialReplyRunner:
    async def _notice(self, text: str, *, silent: bool = False) -> None:
        """Unsolicited Telegram notice; not sent in observe-only mode.
        silent: shown in the chat without a phone notification."""
        if self.cfg.human_approval.observing():
            log(f"observe-only notice (not sent): {text[:120]!r}")
            return
        if silent:
            await self.daemon.approval.send_message(text, silent=True)
            return
        await self.daemon.approval.send_message(text)

    async def _announce_ready(self) -> None:
        """Every restart says so in the chat. With /always on nothing needs
        doing, so it no longer buzzes the phone (Oct 19 2026: six deploys,
        six pushes in one evening)."""
        await self._notice(
            "Pentra reply controls are ready.\n\n"
            "Send /always for durable monitoring, or /on, /on 1h, /on 3h, "
            "/on 5h, /on 8h, or /on 24h for a timed window. Plain /on uses 24 hours. "
            "Send /off to stop paid reads immediately.\n\n"
            + self._status_text(),
            silent=self.daemon.state.operator_is_continuous(),
        )

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.cfg = daemon.cfg
        self.budget = XReadBudget(self.cfg.x_detection, daemon.state)
        self.targets = {
            handle.lstrip("@").lower()
            for handle in self.cfg.handles
        }
        self.reply_sources_configured = bool(
            self.cfg.x_detection.include_self_contained_replies
            and self.cfg.x_detection.reply_source_handles
        )
        self.dynamic_verifier = (
            DynamicAuthorVerifier(self.cfg, daemon.state)
            if self.cfg.x_detection.dynamic_discovery_enabled
            else None
        )
        self.dynamic_targets = (
            set(daemon.state.dynamic_qualified_author_handles())
            if self.cfg.x_detection.dynamic_discovery_enabled
            else set()
        )
        self.session_dynamic_targets = set(self.dynamic_targets)
        self.reconnects: deque[float] = deque()
        self.shift_changed = asyncio.Event()
        worker_count = max(
            1,
            int(self.cfg.human_approval.generation_worker_threads or 0),
        )
        self.candidate_semaphore = asyncio.Semaphore(worker_count)
        self.candidate_tasks: set[asyncio.Task] = set()
        self.processing_failed = asyncio.Event()
        self.processing_budget_error: GenerationBudgetOpen | None = None
        self.x_credit_notice_sent = False
        if self.cfg.human_approval._window() is not None:
            daemon.state.shift_window = self.cfg.human_approval.in_shift_window

    async def _process_candidate(self, event: dict) -> None:
        async with self.candidate_semaphore:
            try:
                await self.daemon.handle_tweet(event)
            except GenerationBudgetOpen as exc:
                self.processing_budget_error = exc
                if not self.daemon.state.operator_is_continuous():
                    self.daemon.state.set_operator_active_until(0)
                self.processing_failed.set()
                self.shift_changed.set()
                await self._notice(
                    "Reply monitoring paused because the Claude budget "
                    f"has no room for another decision: {exc}"
                )
            except Exception as exc:
                log(
                    "candidate processing error: "
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )

    async def _schedule_candidate(self, event: dict) -> None:
        # The 90-second freshness gate measures the post's age when X
        # delivered it, not after it waited for a free worker.
        event = {**event, "_received_at": event.get("_received_at") or time.time()}
        workers = int(
            self.cfg.human_approval.generation_worker_threads or 0
        )
        if workers <= 0:
            await self._process_candidate(event)
            return
        pending_limit = max(
            workers,
            int(self.cfg.human_approval.generation_pending_queue or workers),
        )
        while len(self.candidate_tasks) >= pending_limit:
            await asyncio.wait(
                tuple(self.candidate_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )
        task = asyncio.create_task(self._process_candidate(dict(event)))
        self.candidate_tasks.add(task)
        task.add_done_callback(self.candidate_tasks.discard)

    def _record_reconnect(self) -> None:
        now = time.time()
        while self.reconnects and self.reconnects[0] < now - 3600:
            self.reconnects.popleft()
        self.reconnects.append(now)
        if len(self.reconnects) > self.cfg.x_detection.max_reconnects_per_hour:
            raise XStreamError(
                "official X stream reconnect ceiling reached; refusing a hot loop"
            )

    async def _x_credits_empty_notice(self) -> None:
        """Oct 16-18 2026: X credits sat at $0 for two days with only a log
        line. Say it once per outage; a stream that runs again re-arms it."""
        if self.x_credit_notice_sent:
            return
        self.x_credit_notice_sent = True
        await self._notice(
            "X API credits are empty, so reply monitoring cannot read posts "
            "(X answers HTTP 402). Top up at console.x.com; monitoring "
            "retries every 5 minutes and resumes by itself."
        )

    def _window_text(self) -> str:
        hours = self.cfg.human_approval._window()
        return f"{hours[0]:02d}:00-{hours[1]:02d}:00 UTC" if hours else ""

    def _continuous_window_wait(self) -> float | None:
        """Seconds until the daily window opens or closes, while /always is on."""
        if not self.daemon.state.operator_is_continuous():
            return None
        return self.cfg.human_approval.seconds_until_window_change()

    async def _wait_for_shift_change(self, timeout: float | None) -> None:
        if timeout is None:
            await self.shift_changed.wait()
            return
        try:
            await asyncio.wait_for(self.shift_changed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    def _status_text(self, now: float | None = None) -> str:
        now = now or time.time()
        active_until = self.daemon.state.operator_active_until()
        continuous = self.daemon.state.operator_is_continuous()
        active = self.daemon.state.operator_is_active(now)
        remaining = max(0, math.ceil((active_until - now) / 60))
        capacity_closed = False
        if active:
            try:
                self.daemon.anthropic.preflight(self.cfg.reply_model)
                self.budget.assert_can_connect(now)
            except (GenerationBudgetOpen, XReadBudgetOpen):
                capacity_closed = True
        if continuous and not active:
            mode = f"WAITING (continuous inside {self._window_text()}; resumes automatically)"
        elif continuous and capacity_closed:
            mode = "PAUSED (continuous; spend cap resets automatically)"
        elif continuous:
            mode = "ACTIVE (continuous" + (f", {self._window_text()}" if self._window_text() else "") + ")"
        elif active and capacity_closed:
            mode = f"PAUSED ({remaining}m window; spend cap closed)"
        else:
            mode = f"ACTIVE ({remaining}m remaining)" if active else "OFF"
        drafts = self.daemon.state.reply_draft_count_today(now)
        reads = self.daemon.state.x_stream_read_count_today(now)
        raw_cost = self.daemon.state.x_stream_cost_microusd_today(now)
        # A one-off measurement booked today (x_detection.incident_daily_baseline_*)
        # is shown separately so the reply stream's own usage is readable.
        operating_cost = self.budget.snapshot(now).today_microusd
        price = max(1, self.cfg.x_detection.post_read_microusd)
        one_off_reads = max(0, (raw_cost - operating_cost) // price)
        reads = max(0, reads - one_off_reads)
        read_cost = operating_cost / 1_000_000
        one_off_note = (
            f" + {one_off_reads} one-off measurement reads" if one_off_reads else ""
        )
        generation_cost = (
            self.daemon.state.llm_cost_microusd_today(now) / 1_000_000
        )
        dynamic_targets = self.daemon.state.dynamic_qualified_author_count(now)
        read_capacity = (
            self.cfg.x_detection.daily_microusd_cap
            // self.cfg.x_detection.post_read_microusd
        )
        latency = self.daemon.state.reply_latency_stats_today(now)
        outcomes = self.daemon.state.reply_outcome_counts_today(now)
        speed_line = (
            f"Speed today: post → draft median {latency['median']}s, slowest {latency['max']}s\n"
            if latency["count"]
            else "Speed today: no drafts yet\n"
        )
        outcome_line = (
            "Posts today: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in outcomes.items()) + "\n"
            if outcomes
            else ""
        )
        if self.cfg.human_approval.observing():
            mode += " · OBSERVE-ONLY (drafts not sent)"
        return (
            f"Pentra replies: {mode}\n"
            f"Targets: {len(self.targets)} fixed + {dynamic_targets} freshly discovered\n"
            f"Drafts today: {drafts}/{approval_daily_cap(self.cfg)}\n"
            f"X reads today: {reads}/{read_capacity} (${read_cost:.3f}){one_off_note}\n"
            f"Claude today: ${generation_cost:.3f}\n"
            f"{speed_line}{outcome_line}\n"
            f"Use /always, /on, /on 1h, /on 3h, /on 5h, /on 8h, /on 24h, /off, or /status."
        )

    async def _apply_command(
        self,
        command: tuple[str, int | None],
    ) -> None:
        action, minutes = command
        if action == "status":
            await self.daemon.approval.send_message(self._status_text())
            return
        if action == "off":
            self.daemon.state.set_operator_continuous(False)
            self.daemon.state.set_operator_active_until(0)
            self.shift_changed.set()
            await self.daemon.approval.send_message(
                "Reply monitoring is OFF. The paid X stream is closing now."
            )
            return
        if action == "always":
            now = time.time()
            try:
                self.daemon.anthropic.preflight(self.cfg.reply_model)
                self.budget.assert_can_connect(now)
            except (GenerationBudgetOpen, XReadBudgetOpen) as exc:
                # Preserve the continuous intent even when today's circuit is
                # already closed. The runner will resume after the UTC reset.
                self.daemon.state.set_operator_continuous(True, now=now)
                self.daemon.state.set_operator_active_until(0, now=now)
                self.shift_changed.set()
                await self.daemon.approval.send_message(
                    "Continuous reply monitoring is enabled, but paused by "
                    f"the current spend cap: {exc}\n"
                    "It will retry automatically after the UTC daily reset."
                )
                return
            self.daemon.state.set_operator_continuous(True, now=now)
            self.daemon.state.set_operator_active_until(0, now=now)
            self.shift_changed.set()
            await self.daemon.approval.send_message(
                "Reply monitoring is ACTIVE continuously.\n"
                f"All {len(self.targets)} ranked targets are eligible. "
                "Daily and monthly spend caps remain enforced, and every "
                "public reply still requires your final tap in X."
            )
            return
        if action != "on" or minutes is None:
            return

        now = time.time()
        try:
            self.daemon.anthropic.preflight(self.cfg.reply_model)
            self.budget.assert_can_connect(now)
        except (GenerationBudgetOpen, XReadBudgetOpen) as exc:
            # A spend cap can pause delivery, but it must not silently erase
            # the operator's requested window. Keep the exact expiry they
            # selected; the runner will resume after reset only if time remains.
            self.daemon.state.set_operator_continuous(False, now=now)
            self.daemon.state.set_operator_active_until(
                now + minutes * 60,
                now=now,
            )
            self.shift_changed.set()
            await self.daemon.approval.send_message(
                f"Reply monitoring is PAUSED for the requested {minutes}-minute "
                f"window because a spend cap is closed: {exc}\n"
                "It will resume automatically after the UTC reset if that "
                "window is still active."
            )
            return
        self.daemon.state.set_operator_continuous(False, now=now)
        self.daemon.state.set_operator_active_until(now + minutes * 60, now=now)
        self.shift_changed.set()
        await self.daemon.approval.send_message(
            f"Reply monitoring is ACTIVE for {minutes} minutes.\n"
            f"All {len(self.targets)} ranked targets are eligible. "
            "Useful drafts will arrive here for your final tap in X."
        )

    async def _command_loop(self) -> None:
        offset = self.daemon.state.telegram_update_offset()
        while True:
            try:
                updates = await self.daemon.approval.get_updates(offset=offset)
            except TelegramApprovalError as exc:
                log(f"Telegram command polling interrupted: {exc}")
                await asyncio.sleep(5)
                continue

            for update in updates:
                update_id = int(update.get("update_id") or 0)
                offset = max(offset, update_id + 1)
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id") or "")
                if chat_id != self.cfg.telegram_chat_id:
                    continue
                if await self._store_artifact_message(message):
                    continue
                command = parse_shift_command(
                    str(message.get("text") or ""),
                    default_minutes=self.cfg.human_approval.default_shift_minutes,
                    max_minutes=self.cfg.human_approval.max_shift_minutes,
                )
                if command is not None:
                    try:
                        await self._apply_command(command)
                    except TelegramApprovalError as exc:
                        log(f"Telegram command response failed: {exc}")

            if updates:
                self.daemon.state.set_telegram_update_offset(offset)

    async def _rise_check_once(self, api) -> None:
        now = time.time()
        for kind, min_age, max_age in RISE_CHECKS:
            due = self.daemon.state.drafts_due_rise_check(
                kind=kind, min_age=min_age, max_age=max_age, now=now
            )[:100]
            if not due:
                continue
            body = await api._json(
                "GET",
                f"{X_API_ROOT}/2/tweets",
                params={"ids": ",".join(d["tweet_id"] for d in due), "tweet.fields": "public_metrics"},
            )
            posts = {str(post.get("id")): post for post in body.get("data") or []}
            for draft in due:
                post = posts.get(draft["tweet_id"])
                if post is not None:
                    # Read today by the stream: no new ledger row, no new charge.
                    self.budget.record(post_id=draft["tweet_id"], author=draft["author"], now=now)
                metrics = (post or {}).get("public_metrics") or {}
                age = now - float(draft["source_created_at"])
                self.daemon.state.record_source_snapshot(
                    post_id=draft["tweet_id"], kind=kind, age_seconds=age, metrics=metrics, now=now
                )
                if post is None:
                    continue
                line = rise_line(age, metrics)
                log(f"rise check {kind} @{draft['author']} ({draft['tweet_id']}): {line}")
                if kind != RISE_CHECKS[0][0]:
                    continue
                try:
                    await self.daemon.approval.edit_reply_draft(
                        message_id=int(draft["telegram_message_id"]),
                        live_line=line,
                        tweet_id=draft["tweet_id"],
                        author=draft["author"],
                        source_text=draft["source_text"],
                        reply_text=draft["reply_text"],
                        source_age_seconds=float(draft["created_at"]) - float(draft["source_created_at"]),
                        expires_in_seconds=max(1, int(float(draft["expires_at"]) - float(draft["created_at"]))),
                        author_reach=self.daemon._author_reach(draft["author"]),
                    )
                except TelegramApprovalError as exc:
                    log(f"rise check: draft edit failed: {str(exc)[:160]}")

    async def _rise_check_loop(self) -> None:
        """Beside the stream for the service's lifetime. A failure here is
        logged and never touches the stream."""
        while True:
            await asyncio.sleep(RISE_CHECK_POLL_SECONDS)
            try:
                if not any(
                    self.daemon.state.drafts_due_rise_check(kind=kind, min_age=low, max_age=high)
                    for kind, low, high in RISE_CHECKS
                ):
                    continue
                async with OfficialXStream(self.cfg) as api:
                    await self._rise_check_once(api)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"rise check skipped: {type(exc).__name__}: {str(exc)[:160]}")

    async def _store_artifact_message(self, message: dict) -> bool:
        """/artifact and /howto feed original posts (v3); the posts service
        drafts from them. This service is the one Telegram reader."""
        artifact = parse_artifact_message(message)
        if artifact is None:
            if str(message.get("text") or message.get("caption") or "").strip().lower().startswith("/artifact"):
                await self._reply_quietly(
                    "Send the facts as text after /artifact (with the screenshot if you have one): "
                    "every number in a post is checked against them."
                )
                return True
            return False
        self.daemon.state.add_post_artifact(**artifact)
        await self._reply_quietly(
            "Got it. Two drafts from this artifact in about a minute."
            if artifact["kind"] == "artifact" else "Got it. A checked how-to, two drafts in about a minute."
        )
        return True

    async def _reply_quietly(self, text: str) -> None:
        try:
            await self.daemon.approval.send_message(text)
        except TelegramApprovalError as exc:
            log(f"Telegram reply failed: {exc}")

    async def _end_stale_stream_connections(self) -> int:
        """Ask X to drop connections it still counts while none is open here."""
        try:
            async with OfficialXStream(self.cfg) as api:
                return await api.end_stale_stream_connections()
        except Exception as exc:  # the next reconnect simply retries
            log(
                "official X stale-connection check failed: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
            return 0

    async def _wait_until_operator_active(self) -> None:
        while not self.daemon.state.operator_is_active():
            self.shift_changed.clear()
            if self.daemon.state.operator_is_active():
                return
            # /always outside the daily window wakes itself when it opens.
            await self._wait_for_shift_change(self._continuous_window_wait())

    async def _wait_until_shift_stops(self) -> str:
        while True:
            if self.daemon.state.operator_is_continuous():
                self.shift_changed.clear()
                if not self.daemon.state.operator_is_active():
                    return "off"
                window_wait = self._continuous_window_wait()
                await self._wait_for_shift_change(window_wait)
                if not self.daemon.state.operator_is_active():
                    return "window" if window_wait is not None and self.daemon.state.operator_is_continuous() else "off"
                continue
            now = time.time()
            active_until = self.daemon.state.operator_active_until()
            remaining = active_until - now
            if remaining <= 0:
                return "expired"
            self.shift_changed.clear()
            if not self.daemon.state.operator_is_active():
                return "off"
            try:
                await asyncio.wait_for(
                    self.shift_changed.wait(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return "expired"
            if not self.daemon.state.operator_is_active():
                return "off"

    async def _consume_stream(self, api: OfficialXStream) -> str:
        session_day = self.daemon.state._day(time.time())
        async for event in api.events():
            if self.processing_failed.is_set():
                return "generation-budget"
            if not self.daemon.state.operator_is_active():
                return "off"

            author = str(
                (event.get("author") or {}).get("username") or ""
            ).lstrip("@").lower()
            post_id = str(event.get("id") or "")
            dynamic_audit_exhausted = False
            read = self.budget.record(
                post_id=post_id,
                author=author,
            )
            parent_read = None
            parent_id = str(event.get("parent_id") or "")
            if parent_id:
                parent_read = self.budget.record(
                    post_id=parent_id,
                    author=str(event.get("parent_author") or "").lstrip("@").lower(),
                )
            if read.charged:
                source_kind = "reply" if event.get("is_reply") else "original"
                log(
                    f"official X read {source_kind} "
                    f"@{author or 'unknown'} ({post_id}) "
                    f"| today=${read.today_microusd / 1_000_000:.4f}"
                )
            if parent_read is not None and parent_read.charged:
                log(
                    "official X read reply context "
                    f"({parent_id}) | today="
                    f"${parent_read.today_microusd / 1_000_000:.4f}"
                )
            eligible = author in self.targets or author in self.dynamic_targets
            if not eligible and self.dynamic_verifier is not None:
                if not event.get("dynamic_discovery_match"):
                    log(
                        f"skip @{author or 'unknown'} "
                        "[official stream author outside target set]"
                    )
                else:
                    # The X-edge topic and follower rule is only discovery,
                    # never admission. Avoid a paid historical audit when the
                    # current post already fails the local post-level gate.
                    normalized = normalize_tweet(event)
                    pre_skip, pre_reason = should_skip(
                        normalized,
                        self.cfg,
                        self.daemon.state,
                    )
                    if pre_skip:
                        self.daemon._record_reply_outcome(
                            normalized,
                            outcome="deterministic_rejected",
                            reason=pre_reason,
                        )
                        log(
                            f"skip dynamic @{author or 'unknown'} "
                            f"[current post: {pre_reason}]"
                        )
                    else:
                        verdict = await self.dynamic_verifier.verify_official(
                            author,
                            user_id=str(
                                (event.get("author") or {}).get("id") or ""
                            ),
                            stream_author=event.get("author") or {},
                            api=api,
                            record_read=self.budget.record,
                        )
                        dynamic_audit_exhausted = bool(
                            verdict.get("official_audit_exhausted")
                        )
                        eligible = verdict.get("status") == "qualified"
                        median = float(verdict.get("median_views") or 0)
                        if eligible:
                            self.dynamic_targets.add(author)
                            log(
                                f"dynamic author qualified @{author} "
                                f"| median={median:.0f} "
                                f"| originals={int(verdict.get('sample_posts') or 0)} "
                                f"| audit_reads={int(verdict.get('official_post_reads') or 0)}"
                            )
                        else:
                            log(
                                f"skip dynamic @{author or 'unknown'} "
                                f"[author audit: {str(verdict.get('reason') or '')[:120]}]"
                            )
            elif not eligible:
                log(
                    f"skip @{author or 'unknown'} "
                    "[official stream author outside target set]"
                )

            if eligible:
                await self._schedule_candidate(event)
                if self.processing_failed.is_set():
                    return "generation-budget"

            if len(
                self.dynamic_targets - self.session_dynamic_targets
            ) >= 5:
                log(
                    "dynamic qualified roster gained five authors; "
                    "refreshing canonical rules on the same stream slot"
                )
                return "dynamic-roster-refresh"

            if (
                event.get("is_reply")
                and self.cfg.x_detection.include_self_contained_replies
                and self.daemon.state.reply_source_outcome_count_today()
                >= self.cfg.x_detection.reply_source_daily_read_cap
            ):
                self.cfg.x_detection.include_self_contained_replies = False
                log(
                    "official X reply-source read reserve reached; "
                    "continuing with originals only"
                )
                return "reply-source-budget"

            if (
                self.reply_sources_configured
                and not self.cfg.x_detection.include_self_contained_replies
                and self.daemon.state._day(time.time()) != session_day
            ):
                return "reply-source-reset"

            if read.exhausted or dynamic_audit_exhausted or (
                parent_read is not None and parent_read.exhausted
            ):
                self.shift_changed.set()
                await self._notice(
                    "Reply monitoring paused at the official X daily/monthly "
                    "read cap.\n\n" + self._status_text()
                )
                return "read-budget"
            pace = self._read_pace_wait(read)
            if pace > 0:
                self.read_pace_wait = pace
                log(f"official X reads ahead of the shift's pace; pausing {pace / 60:.0f} min")
                return "read-pace"
        return "stream-ended"

    def _read_pace_wait(self, read) -> float:
        x = self.cfg.x_detection
        if not getattr(x, "read_pacing", False):
            return 0.0
        price = max(1, int(x.post_read_microusd))
        return read_pace_wait_seconds(now=time.time(), reads_used=int(read.today_microusd) // price,
                                      daily_reads=int(read.daily_cap_microusd) // price,
                                      window=self.cfg.human_approval._window(), burst=int(x.read_pace_burst))

    async def _stream_session(self) -> str:
        self.processing_failed.clear()
        self.processing_budget_error = None
        self.dynamic_targets = (
            set(self.daemon.state.dynamic_qualified_author_handles())
            if self.cfg.x_detection.dynamic_discovery_enabled
            else set()
        )
        self.session_dynamic_targets = set(self.dynamic_targets)
        dynamic_handles = sorted(self.dynamic_targets)
        if self.reply_sources_configured:
            reply_reads = self.daemon.state.reply_source_outcome_count_today()
            self.cfg.x_detection.include_self_contained_replies = (
                reply_reads < self.cfg.x_detection.reply_source_daily_read_cap
            )
        self.daemon.anthropic.preflight(self.cfg.reply_model)
        snapshot = self.budget.assert_can_connect()
        log(
            "official X stream connecting | "
            f"read_today=${snapshot.today_microusd / 1_000_000:.4f} "
            f"| read_month=${snapshot.month_microusd / 1_000_000:.4f}"
        )

        async with OfficialXStream(self.cfg) as api:
            rules = await api.ensure_canonical_rules(
                self.cfg.handles + dynamic_handles
            )
            log(
                f"official X canonical stream ready | rules={len(rules)} "
                f"| targets={len(self.targets)}+{len(dynamic_handles)}"
            )
            stream_task = asyncio.create_task(self._consume_stream(api))
            shift_task = asyncio.create_task(self._wait_until_shift_stops())
            failure_task = asyncio.create_task(self.processing_failed.wait())
            done, pending = await asyncio.wait(
                {stream_task, shift_task, failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if failure_task in done and self.processing_failed.is_set():
                return "generation-budget"
            completed = shift_task if shift_task in done else stream_task
            return await completed

    @staticmethod
    def _seconds_until_utc_reset(now: float | None = None) -> float:
        current = datetime.fromtimestamp(now or time.time(), timezone.utc)
        reset = (current + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=5,
            microsecond=0,
        )
        return max(1.0, reset.timestamp() - current.timestamp())

    async def _wait_for_budget_reset(self) -> None:
        delay = self._seconds_until_utc_reset()
        if not self.daemon.state.operator_is_continuous():
            remaining = self.daemon.state.operator_active_until() - time.time()
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        self.shift_changed.clear()
        try:
            await asyncio.wait_for(self.shift_changed.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    async def _wait_for_stream_capacity(self, delay: float) -> None:
        """Wait without burning connection attempts, but wake on operator input."""
        self.shift_changed.clear()
        try:
            await asyncio.wait_for(
                self.shift_changed.wait(),
                timeout=max(1.0, float(delay)),
            )
        except asyncio.TimeoutError:
            return

    async def run(self) -> None:
        if not self.cfg.x_detection.enabled:
            raise RuntimeError("official X reply detection is disabled")
        if self.cfg.detection.enabled:
            raise RuntimeError(
                "legacy TwitterAPI.io detection must remain disabled"
            )
        if self.cfg.publishing.enabled:
            raise RuntimeError(
                "reply-discovery service must never own public X publishing"
            )
        if not self.cfg.human_approval.enabled:
            raise RuntimeError("Telegram human approval is disabled")
        if not self.targets:
            raise RuntimeError("no reply targets are configured")

        verification = await self.daemon.approval.verify()
        if verification["chat_id"] != self.cfg.telegram_chat_id:
            raise RuntimeError("Telegram destination verification failed")
        log(
            "Telegram approval channel ready"
            + (
                f" (@{verification['bot_username']})"
                if verification["bot_username"]
                else ""
            )
        )

        command_task = asyncio.create_task(self._command_loop())
        rise_task = asyncio.create_task(self._rise_check_loop())
        await self._announce_ready()
        try:
            backoff = 1
            connection_limit_streak = 0
            while True:
                await self._wait_until_operator_active()
                try:
                    reason = await self._stream_session()
                    backoff = 1
                    self.x_credit_notice_sent = False
                    if reason == "window":
                        await self._notice(
                            f"Reply window closed ({self._window_text()}). Paid X monitoring is "
                            "off until it opens again; /on starts a timed shift now.\n\n"
                            + self._status_text()
                        )
                        continue
                    if reason == "generation-budget" and isinstance(self.processing_budget_error, ProviderCreditExhausted):
                        # Out of provider credit is fixed by a top-up, not by the
                        # daily reset: look again in an hour instead of at 00:00 UTC.
                        await self._notice("Reply monitoring is paused until the Anthropic credit is topped up; "
                                           "it checks again every hour.")
                        await self._wait_for_stream_capacity(CREDIT_RECHECK_SECONDS)
                        continue
                    if reason in {"generation-budget", "read-budget"}:
                        if self.daemon.state.operator_is_continuous():
                            await self._notice(
                                "Continuous monitoring will resume automatically "
                                "after the UTC daily budget reset."
                            )
                        else:
                            await self._notice(
                                "The requested timed window is preserved and "
                                "will resume after the UTC reset if time remains."
                            )
                        await self._wait_for_budget_reset()
                        continue
                    if reason == "read-pace":
                        # A planned pause, not a failure: no reconnect is counted.
                        await self._wait_for_stream_capacity(getattr(self, "read_pace_wait", 60.0))
                        continue
                    if reason in {
                        "reply-source-budget",
                        "reply-source-reset",
                        "dynamic-roster-refresh",
                    }:
                        # Intentional canonical-rule transition: reply rules
                        # pause at their reserve and return after UTC reset;
                        # originals continue without consuming reconnect quota.
                        continue
                    if reason == "expired":
                        await self._notice(
                            "Reply shift ended. Paid X monitoring is OFF.\n\n"
                            + self._status_text()
                        )
                    if not self.daemon.state.operator_is_active():
                        continue
                    self._record_reconnect()
                    log(
                        "official X stream ended; "
                        f"reconnecting in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(
                        backoff * 2,
                        self.cfg.x_detection.reconnect_max_seconds,
                    )
                except (GenerationBudgetOpen, XReadBudgetOpen) as exc:
                    log(f"official X reply shift held by budget: {exc}")
                    await self._notice(
                        "Reply monitoring is paused because a spend cap "
                        f"is closed: {exc}"
                    )
                    await self._wait_for_budget_reset()
                except XCreditsDepleted as exc:
                    if not self.daemon.state.operator_is_active():
                        continue
                    delay = 300.0
                    log(
                        "official X stream held by provider credits: "
                        f"{str(exc)[:180]}; retrying in {int(delay)}s"
                    )
                    await self._x_credits_empty_notice()
                    await self._wait_for_stream_capacity(delay)
                except XRateLimitError as exc:
                    if not self.daemon.state.operator_is_active():
                        continue
                    delay = stream_rate_limit_delay(
                        exc,
                        now=time.time(),
                        connection_limit_streak=connection_limit_streak,
                    )
                    if (
                        exc.connection_limit
                        and connection_limit_streak == STALE_CONNECTION_CHECK_STREAK
                    ):
                        ended = await self._end_stale_stream_connections()
                        if ended:
                            log(
                                f"official X stream: ended {ended} stale "
                                "connection(s) X still counted for this app"
                            )
                            delay = min(delay, 10.0)
                    if exc.connection_limit:
                        connection_limit_streak += 1
                    log(
                        "official X stream held by provider rate limit: "
                        f"{str(exc)[:180]}; retrying in {math.ceil(delay)}s"
                    )
                    await self._wait_for_stream_capacity(delay)
                except (XStreamError, OSError, asyncio.TimeoutError) as exc:
                    if not self.daemon.state.operator_is_active():
                        continue
                    self._record_reconnect()
                    connection_limit_streak = 0
                    delay = reconnect_delay_after_interruption(exc, backoff)
                    log(
                        f"official X stream interrupted: {type(exc).__name__}: "
                        f"{str(exc)[:180]}; "
                        f"retrying in {delay:g}s"
                    )
                    await asyncio.sleep(delay)
                    backoff = min(
                        backoff * 2,
                        self.cfg.x_detection.reconnect_max_seconds,
                    )
        finally:
            for task in (command_task, rise_task):
                task.cancel()
            for task in (command_task, rise_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def build_daemon() -> Daemon:
    daemon = Daemon()
    daemon.cfg.dry_run = False
    daemon.cfg.detection.enabled = False
    daemon.cfg.x_detection.enabled = True
    daemon.cfg.publishing.enabled = False
    daemon.cfg.human_approval.enabled = True
    return daemon


if __name__ == "__main__":
    with daemon_lock("/tmp/pentra-x-replies.lock"):
        asyncio.run(OfficialReplyRunner(build_daemon()).run())
