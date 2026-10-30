"""Main loop: stream target tweets -> deterministic skip gate -> selectivity ->
pacing -> LLM decision -> post reply. Detection, generation, and publishing run
as independent tasks so one action can never block fresh tweet ingestion.

Run: python -m bot.daemon
"""
import asyncio
import json
import fcntl
import os
import random
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from anthropic import Anthropic

from . import config, generate, pacing, poster, stream
from .costguard import CostCircuitOpen, DeliveryGuard
from .health import check_session_alive
from .llm_budget import GenerationBudgetOpen, MeteredAnthropic
from .skipgate import normalize_tweet, should_skip, tweet_age_seconds
from .state import State
from .telegram_approval import (
    TelegramApprovalClient,
    TelegramApprovalError,
    build_x_reply_intent,
)


def log(msg: str):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


def _last_rewrite(trace: dict) -> str:
    last = ""
    for key in ("clarification_repair", "bounded_copy_repair"):
        step = trace.get(key) or {}
        last = str((step.get("second") or {}).get("after") or (step.get("shortened") or {}).get("after")
                   or step.get("after") or last)
    for step in trace.get("deterministic_neutral_question_repairs") or []:
        last = str(step.get("after") or last)
    return last.strip()


def _rejected_draft_line(author: str, decision: dict, trace: dict | None, dry_run: bool) -> str | None:
    """Oct 23 2026: production kept no text of a rejected draft, so the live v7
    losses (prevalence words, restating vetoes) could not be read. A copy-guard
    or reviewer rejection now logs the draft, its last rewrite and the rewrite
    after a veto."""
    trace = trace or {}
    candidate = str((trace.get("single_pass") or {}).get("text") or "").strip()
    if not candidate:
        return None
    if dry_run:
        return f"DRY CANDIDATE @{author}: {candidate!r}"
    reason = str(decision.get("reason") or "")
    if "copy guard" not in reason and "grounding veto" not in reason:
        return None
    line = f"REJECTED DRAFT @{author} [{reason[:90]}]: {candidate!r}"
    last = _last_rewrite(trace)
    if last and last != candidate:
        line += f" -> {last!r}"
    retry = (trace.get("veto_rewrite") or {}).get("retry") or {}
    retry_text = str((retry.get("single_pass") or {}).get("text") or "").strip()
    if retry_text:
        line += f" | after veto: {retry_text!r}"
        retry_last = _last_rewrite(retry)
        if retry_last and retry_last != retry_text:
            line += f" -> {retry_last!r}"
    return line


def _decide_reply_in_worker(
    cfg: config.Config,
    source_text: str,
    author: str,
    recent_replies: list[str],
    trace: dict | None,
    human_review: bool,
) -> dict:
    """Run one metered synchronous Claude pipeline off the stream loop.

    The worker owns both its Anthropic client and SQLite connection. This
    avoids sharing thread-bound sqlite objects while the main event loop keeps
    ingesting the one production filtered stream.
    """
    worker_state = State()
    raw_anthropic = Anthropic(api_key=cfg.anthropic_key)
    metered = MeteredAnthropic(
        raw_anthropic,
        cfg.generation,
        worker_state,
        lane="reply",
    )
    try:
        return generate.decide(
            cfg,
            source_text,
            author,
            metered,
            recent_replies=recent_replies,
            trace=trace,
            secondary_audit=True,
            human_review=human_review,
        )
    finally:
        worker_state.db.close()
        close = getattr(raw_anthropic, "close", None)
        if callable(close):
            close()


def reconnect_delay_seconds(cfg: config.Config) -> int:
    """Honor TwitterAPI.io's documented websocket-slot release window."""
    return max(95, int(cfg.detection.reconnect_backoff_seconds))


@contextmanager
def daemon_lock(path: str = "/tmp/pentra-x-daemon.lock"):
    """Prevent two daemons from owning the same paid provider rule set."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Pentra X daemon is already running") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Daemon:
    # Original posts go to Telegram for a person to publish from the X app
    # instead of through the X API (set by the posts service entrypoint).
    post_drafts = False

    def __init__(self):
        self.cfg = config.load()
        self.state = State()
        raw_anthropic = Anthropic(api_key=self.cfg.anthropic_key)
        self.anthropic = MeteredAnthropic(
            raw_anthropic,
            self.cfg.generation,
            self.state,
            lane="reply",
        )
        self.original_anthropic = MeteredAnthropic(
            raw_anthropic,
            self.cfg.generation,
            self.state,
            lane="original",
        )
        self.approval = TelegramApprovalClient(self.cfg)
        self.approval_send_lock = asyncio.Lock()
        self.rng = random.Random()

    @staticmethod
    def _is_approval_spacing_hold(reason: str) -> bool:
        return str(reason or "").startswith("approval gap not elapsed")

    def _record_reply_outcome(
        self,
        tw: dict,
        *,
        outcome: str,
        reason: str = "",
    ) -> None:
        """Persist source-level funnel evidence without storing credentials."""
        self.state.record_generation_outcome(
            kind="reply",
            bucket=str(tw.get("id") or "unknown"),
            outcome=outcome,
            reason=reason,
            evidence_id=(
                f"{str(tw.get('author') or '')}|"
                f"{'reply' if tw.get('is_reply') else 'original'}"
            ),
        )

    def enforce_authoritative_provider_caps(self) -> None:
        today = self.state.provider_balance_debits_today()
        month = self.state.provider_balance_debits_month()
        if today > self.cfg.detection.daily_credit_cap:
            raise CostCircuitOpen(
                f"authoritative daily provider debit cap exceeded: {today} credits"
            )
        if month > self.cfg.detection.monthly_credit_cap:
            raise CostCircuitOpen(
                f"authoritative monthly provider debit cap exceeded: {month} credits"
            )

    REACH_POOL_PATH = os.path.join(
        config.ROOT, "research", "output", "pentra-x-reply-targets.json"
    )

    def _author_reach(self, author: str) -> str:
        """'230K views/post' from the measured pool, so the biggest drafts get
        tapped first. Empty when the account is not in the pool."""
        cache = getattr(self, "_reach_cache", None)
        if cache is None:
            cache = {}
            try:
                with open(self.REACH_POOL_PATH, encoding="utf-8") as fh:
                    pool = json.load(fh)
                for target in pool.get("targets") or []:
                    views = float(target.get("medianViews") or 0)
                    if views >= 1000:
                        cache[str(target.get("handle") or "").lstrip("@").lower()] = views
            except (OSError, ValueError, TypeError, AttributeError):
                cache = {}
            self._reach_cache = cache
        views = cache.get(str(author or "").lstrip("@").lower())
        if not views:
            return ""
        if views >= 1_000_000:
            return f"{views / 1_000_000:.1f}M views/post"
        return f"{views / 1000:.0f}K views/post"

    def _capture_post(self, tw: dict, source_text: str) -> None:
        path = str(self.cfg.human_approval.capture_posts_path or "").strip()
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "id": str(tw.get("id") or ""),
                    "author": str(tw.get("author") or ""),
                    "created": tw.get("created"),
                    "received": time.time(),
                    "is_reply": bool(tw.get("is_reply")),
                    "text": source_text,
                }) + "\n")
        except OSError as exc:
            log(f"post capture failed: {exc}")

    async def handle_tweet(self, evt: dict):
        """One author's posts are drafted one at a time. Concurrent workers
        used to generate for sibling posts before any draft existed, paying
        for calls the author cooldown then discarded; a sibling now waits and
        the cooldown holds it without a model call when a draft went out."""
        author = str(normalize_tweet(evt).get("author") or "").lstrip("@").lower()
        if not author:
            await self._handle_tweet(evt)
            return
        locks = self.__dict__.setdefault("_author_locks", {})
        lock = locks.setdefault(author, asyncio.Lock())
        async with lock:
            await self._handle_tweet(evt)

    async def _handle_tweet(self, evt: dict):
        decision_started = time.perf_counter()
        tw = normalize_tweet(evt)
        source_text = str(tw.get("context_text") or tw["text"])
        self._capture_post(tw, source_text)
        skip, reason = should_skip(tw, self.cfg, self.state, now=evt.get("_received_at"))
        if skip:
            self._record_reply_outcome(
                tw,
                outcome="deterministic_rejected",
                reason=reason,
            )
            log(f"skip @{tw['author']} [{reason}]")
            return

        human_approval = self.cfg.human_approval.enabled
        if human_approval:
            if self.state.reply_draft_exists(tw["id"]):
                log(f"skip @{tw['author']} [approval draft already exists]")
                return
            ok, reason = pacing.can_notify_reply(
                self.cfg, self.state, tw["author"]
            )
        else:
            if self.cfg.reply_consent_for(tw["author"]) is None:
                log(f"skip @{tw['author']} [no auditable automated-reply opt-in]")
                return
            ok, reason = pacing.can_reply(self.cfg, self.state, tw["author"])

        if not ok and not (
            human_approval and self._is_approval_spacing_hold(reason)
        ):
            self._record_reply_outcome(tw, outcome="held", reason=reason)
            log(f"hold @{tw['author']} [{reason}]")
            return

        if (
            not human_approval
            and not pacing.selectivity_pass(self.cfg, tw["author"], self.rng)
        ):
            log(f"skip @{tw['author']} [selectivity/reach]")
            return

        # final quality gate: the LLM decides and writes
        # Oct 23 2026: always traced, so a rejected draft's text can be logged.
        trace = {}
        try:
            recent_replies = (
                self.state.recent_texts("reply", 12)
                + self.state.recent_reply_drafts(12)
            )[-12:]
            if self.cfg.human_approval.generation_worker_threads > 0:
                decision = await asyncio.to_thread(
                    _decide_reply_in_worker,
                    self.cfg,
                    source_text,
                    tw["author"],
                    recent_replies,
                    trace,
                    human_approval,
                )
            else:
                decision = generate.decide(
                    self.cfg,
                    source_text,
                    tw["author"],
                    self.anthropic,
                    recent_replies=recent_replies,
                    trace=trace,
                    # Telegram review removes autonomous publishing risk, but
                    # it does not make fabricated or weakly grounded copy
                    # useful. Keep the hostile factual verifier in both modes.
                    secondary_audit=True,
                    human_review=human_approval,
                )
        except GenerationBudgetOpen:
            raise
        except Exception as e:
            self._record_reply_outcome(
                tw,
                outcome="generation_error",
                reason=f"{type(e).__name__}: {e}",
            )
            log(f"gen error: {type(e).__name__} {e}")
            return
        if decision.get("action") != "reply":
            rejected = _rejected_draft_line(tw["author"], decision, trace, self.cfg.dry_run)
            if rejected:
                log(rejected)
            self._record_reply_outcome(
                tw,
                outcome="quality_rejected",
                reason=str(decision.get("reason") or ""),
            )
            log(f"skip @{tw['author']} [llm: {decision.get('reason','')[:60]}]")
            return

        text = decision["text"]
        decision_ms = (time.perf_counter() - decision_started) * 1000
        if human_approval:
            # Model decisions may finish concurrently. Serialize only the
            # tiny reservation boundary and wait out the Telegram spacing gap
            # instead of throwing away an otherwise valid source post.
            approval_lock = getattr(self, "approval_send_lock", None)
            if approval_lock is None:
                approval_lock = asyncio.Lock()
                self.approval_send_lock = approval_lock
            async with approval_lock:
                while True:
                    ok, reason = pacing.can_notify_reply(
                        self.cfg,
                        self.state,
                        tw["author"],
                    )
                    if ok:
                        break
                    if not self._is_approval_spacing_hold(reason):
                        self._record_reply_outcome(
                            tw,
                            outcome="held",
                            reason=reason,
                        )
                        log(f"hold @{tw['author']} [{reason}]")
                        return
                    now = time.time()
                    wait_seconds = max(
                        0.05,
                        self.cfg.human_approval.min_candidate_gap_seconds
                        - (now - self.state.last_reply_draft_at()),
                    )
                    queued_age = tweet_age_seconds(tw["created"], now)
                    if (
                        queued_age is None
                        or queued_age + wait_seconds
                        > self._approval_window_seconds()
                    ):
                        age_label = (
                            "unknown"
                            if queued_age is None
                            else f"{queued_age:.1f}s"
                        )
                        self._record_reply_outcome(
                            tw,
                            outcome="deadline_rejected",
                            reason=(
                                "notification queue would exceed reply deadline; "
                                f"age={age_label}"
                            ),
                        )
                        log(
                            f"skip @{tw['author']} "
                            "[notification queue would exceed reply deadline]"
                        )
                        return
                    await asyncio.sleep(wait_seconds)

                final_age = tweet_age_seconds(tw["created"], time.time())
                if (
                    final_age is None
                    or final_age > self._approval_window_seconds()
                ):
                    age_label = (
                        "unknown" if final_age is None else f"{final_age:.1f}s"
                    )
                    self._record_reply_outcome(
                        tw,
                        outcome="deadline_rejected",
                        reason=f"reply deadline elapsed; age={age_label}",
                    )
                    log(
                        f"skip @{tw['author']} "
                        f"[reply deadline elapsed; age={age_label}]"
                    )
                    return
                remaining_seconds = max(
                    0,
                    int(self._approval_window_seconds() - final_age),
                )
                if remaining_seconds <= 0:
                    self._record_reply_outcome(
                        tw,
                        outcome="deadline_rejected",
                        reason="no approval window remains",
                    )
                    log(f"skip @{tw['author']} [no approval window remains]")
                    return
                intent_url = build_x_reply_intent(tw["id"], text, tw["author"])
                now = time.time()
                expires_at = min(
                    now + self.cfg.human_approval.candidate_ttl_seconds,
                    now + remaining_seconds,
                )
                if self.cfg.dry_run:
                    log(
                        f"DRY APPROVAL -> @{tw['author']} ({tw['id']}) "
                        f"age={final_age:.1f}s decision={decision_ms:.0f}ms: "
                        f"{text!r}"
                    )
                    return
                if not self.state.reserve_reply_draft(
                    tweet_id=tw["id"],
                    author=tw["author"],
                    source_text=source_text,
                    reply_text=text,
                    source_created_at=now - final_age,
                    intent_url=intent_url,
                    expires_at=expires_at,
                    now=now,
                ):
                    self._record_reply_outcome(
                        tw,
                        outcome="held",
                        reason="approval reservation already exists",
                    )
                    log(
                        f"skip @{tw['author']} "
                        "[approval reservation already exists]"
                    )
                    return
                if self.cfg.human_approval.observing():
                    self.state.mark_reply_draft_observed(tweet_id=tw["id"])
                    self._record_reply_outcome(
                        tw,
                        outcome="draft_observed",
                        reason=str(decision.get("reason") or ""),
                    )
                    log(
                        f"OBSERVED DRAFT -> @{tw['author']} ({tw['id']}) "
                        f"age={final_age:.1f}s decision={decision_ms:.0f}ms: {text!r}"
                    )
                    return
            approval = getattr(self, "approval", None)
            if approval is None:
                approval = TelegramApprovalClient(self.cfg)
                self.approval = approval
            try:
                result = await approval.send_reply_draft(
                    tweet_id=tw["id"],
                    author=tw["author"],
                    source_text=source_text,
                    reply_text=text,
                    source_age_seconds=final_age,
                    expires_in_seconds=max(1, int(expires_at - now)),
                    author_reach=self._author_reach(tw["author"]),
                )
            except TelegramApprovalError as exc:
                self._record_reply_outcome(
                    tw,
                    outcome="notification_failed",
                    reason=str(exc),
                )
                self.state.fail_reply_draft_notification(
                    tweet_id=tw["id"],
                    error=str(exc),
                )
                raise
            self.state.complete_reply_draft_notification(
                tweet_id=tw["id"],
                telegram_chat_id=result["chat_id"],
                telegram_message_id=result["message_id"],
            )
            self._record_reply_outcome(
                tw,
                outcome="draft_notified",
                reason=str(decision.get("reason") or ""),
            )
            log(
                f"APPROVAL SENT -> @{tw['author']} ({tw['id']}) "
                f"age={final_age:.1f}s decision={decision_ms:.0f}ms "
                f"expires_in={int(expires_at - now)}s"
            )
            return

        final_age = tweet_age_seconds(tw["created"], time.time())
        if final_age is None or final_age > self.cfg.pacing.max_tweet_age_seconds:
            age_label = "unknown" if final_age is None else f"{final_age:.1f}s"
            self._record_reply_outcome(
                tw,
                outcome="deadline_rejected",
                reason=f"reply deadline elapsed; age={age_label}",
            )
            log(
                f"skip @{tw['author']} [reply deadline elapsed; age={age_label}]"
            )
            return

        log(
            f"REPLY -> @{tw['author']} ({tw['id']}) "
            f"age={final_age:.1f}s decision={decision_ms:.0f}ms: {text!r}"
        )
        ok = await poster.reply(
            self.cfg,
            self.state,
            tw["id"],
            tw["author"],
            text,
        )
        if ok:
            if self.cfg.dry_run:
                log("dry-run reply validated; live action ledger unchanged")
                return
            self.state.record("reply", text, tweet_id=tw["id"], author=tw["author"])
            log("posted; worker remains available and pacing is enforced by state")

    # Original posts v3: with no artifact drafted in this long, a slot asks for
    # one instead of writing.
    ARTIFACT_ASK_QUIET_SECONDS = 6 * 3600
    ARTIFACT_ASK_TEXT = (
        "No new artifact for this Pentra post slot, so nothing was written. "
        "Send one with /artifact and the facts as text (a screenshot captioned "
        "/artifact works too): an article's position change, a fact-check block, "
        "a signup number, a decision and why, a bug. Every number must be real. "
        "Or send /howto for a checked Search Console how-to."
    )

    async def _artifact_posts_step(self) -> None:
        """v3 (Oct 20 2026): every post comes from one real artifact the owner
        sent. A new artifact becomes two drafts right away; a slot with nothing
        new asks for one instead of writing."""
        artifact = self.state.next_new_post_artifact(source="telegram")
        if artifact is not None:
            await self._draft_from_artifact(artifact)
            return
        if not pacing.should_standalone_post(self.cfg, self.state, kind="post_ask"):
            return
        queued = self.state.next_new_post_artifact(source="feed")
        if queued is not None and await self._draft_from_artifact(queued):
            self.state.record("post_ask", f"slot filled by feed artifact {queued['id']}")
            return
        if time.time() - self.state.last_post_artifact_drafted_at() < self.ARTIFACT_ASK_QUIET_SECONDS:
            self.state.record("post_ask", "not sent: an artifact was drafted recently")
            return
        from .stances import HOW_TO_CARDS
        if self.state.howto_artifacts_done() < len(HOW_TO_CARDS):
            # Oct 21 2026: a slot with no artifact, or whose artifact failed
            # every check, posts the next checked how-to card instead of
            # asking; it asks only once every card is used.
            artifact_id = self.state.add_post_artifact(kind="howto", text="", source="auto")
            filled = await self._draft_from_artifact({"id": artifact_id, "kind": "howto", "text": "", "photo_file_id": ""})
            self.state.record("post_ask", f"slot filled by how-to card {artifact_id}" if filled
                              else "slot empty: the artifact and the how-to card both failed")
            return
        if queued is not None:
            self.state.record("post_ask", "slot empty: the artifact failed and no how-to card is left")
            return
        try:
            await self.approval.send_message(self.ARTIFACT_ASK_TEXT)
        except TelegramApprovalError as exc:
            log(f"artifact request not delivered: {exc}")
            return
        self.state.record("post_ask", self.ARTIFACT_ASK_TEXT)

    async def _draft_from_artifact(self, artifact: dict) -> bool:
        """True once the drafts reached Telegram or the post was published."""
        text = str(artifact.get("text") or "")
        if artifact.get("kind") == "howto":
            from .stances import HOW_TO_CARDS
            text = HOW_TO_CARDS[self.state.howto_artifacts_done() % len(HOW_TO_CARDS)]["text"]
        trace: dict = {}
        try:
            drafts = generate.artifact_posts(
                self.cfg,
                getattr(self, "original_anthropic", self.anthropic),
                artifact_text=text,
                recent_posts=self.state.recent_texts(("post", "post_draft"), 10),
                trace=trace,
                # A how-to card's data is its steps, not numbers or quotes; the
                # grounding audit still holds every draft to the card.
                min_shared_tokens=0 if artifact.get("kind") == "howto" else 2,
                generic_ok=artifact.get("kind") == "howto",
            )
        except GenerationBudgetOpen:
            raise
        except Exception as exc:
            self.state.finish_post_artifact(artifact["id"], status="failed", note=f"{type(exc).__name__}: {exc}")
            log(f"artifact post error: {type(exc).__name__}: {exc}")
            return False
        if not drafts:
            # Rejected drafts say which check failed (Oct 21 2026: two drafts
            # failed "too long" and the note read "Missing: none, artifact
            # supports both drafts").
            rejected = "; ".join((trace.get("rejected") or {}).values())
            missing = str(trace.get("missing") or "")
            self.state.finish_post_artifact(artifact["id"], status="failed", note=rejected or missing or "no draft")
            if not self.post_drafts:
                # Automatic posting: the slot falls back to a how-to card; no ask.
                log(f"artifact {artifact['id']} drafted nothing: {rejected or missing or 'no draft'}")
                return False
            try:
                await self.approval.send_message(
                    f"No post from that artifact: the drafts failed the checks ({rejected})." if rejected
                    else "No post from that artifact: it can't fill a shape with real data yet. "
                    + (f"Missing: {missing}" if missing else "Send more facts with /artifact.")
                )
            except TelegramApprovalError as exc:
                log(f"artifact reply not delivered: {exc}")
            return False
        if not self.post_drafts:
            return await self._publish_artifact_post(artifact, drafts, artifact_text=text)
        try:
            if artifact.get("photo_file_id"):
                await self.approval.send_photo(
                    file_id=artifact["photo_file_id"], caption="Screenshot for the drafts below"
                )
            for index, draft in enumerate(drafts, 1):
                await self.approval.send_post_draft(
                    post_text=draft["text"], topic=f"option {index}/{len(drafts)} · {draft['shape']}"
                )
                self.state.record("post_draft", draft["text"])
        except TelegramApprovalError as exc:
            self.state.finish_post_artifact(artifact["id"], status="failed", note=f"Telegram: {exc}")
            log(f"artifact drafts not delivered: {exc}")
            return False
        self.state.finish_post_artifact(artifact["id"], status="drafted", note=f"{len(drafts)} drafts")
        log(f"ARTIFACT DRAFTS sent: {len(drafts)} from artifact {artifact['id']}")
        return True

    # Oct 21 2026 (Arshad: "automate posts... stop adding my manual
    # intervention"): with review-first off, the best passing draft goes out
    # through the official X API and Telegram gets a quiet note. X accepts
    # Premium-length posts over the API (first post: 361 characters, Oct 21).
    POST_SHAPE_ORDER = ("receipt", "fact_check", "build", "teardown", "how_to", "take")

    async def _publish_artifact_post(self, artifact: dict, drafts: list[dict], *, artifact_text: str) -> bool:
        order = {shape: index for index, shape in enumerate(self.POST_SHAPE_ORDER)}
        best = sorted(drafts, key=lambda d: order.get(d["shape"], len(order)))[0]
        text = best["text"]
        try:
            published, problem = await poster.post(self.cfg, self.state, text), ""
        except poster.PublishError as exc:
            published, problem = False, str(exc)[:200]
        if not published:
            self.state.finish_post_artifact(artifact["id"], status="failed",
                                            note=f"publish: {problem or 'publisher returned false'}")
            log(f"POST PUBLISH FAILED artifact={artifact['id']}: {problem}")
            try:
                await self.approval.send_message(f"Post not published ({problem or 'the publisher refused it'}).",
                                                 silent=True)
            except TelegramApprovalError as exc:
                log(f"publish failure notice not delivered: {exc}")
            return False
        self.state.record("post", text)
        self.state.finish_post_artifact(artifact["id"], status="published", note=f"{best['shape']}, {len(text)} chars")
        log(f"POST PUBLISHED artifact={artifact['id']} shape={best['shape']}: {text!r}")
        try:
            await self.approval.send_message(f"Posted on @pentradev ({best['shape']}):\n{text}", silent=True)
        except TelegramApprovalError as exc:
            log(f"post notice not delivered: {exc}")
        return True

    async def _value_posts_step(self) -> None:
        """Posts v4 (Oct 22 2026): each slot publishes the next post of the
        weekly plan (post_cards.POST_PLAN), built from sourced cards to teach
        the reader one thing. An entry that fails its checks is skipped for the
        next one, three times at most per slot, and tried again on a later day
        (State.next_plan_index). The owner's own /artifact still goes first."""
        artifact = self.state.next_new_post_artifact(source="telegram")
        if artifact is not None:
            await self._draft_from_artifact(artifact)
            return
        if not pacing.should_standalone_post(self.cfg, self.state, kind="post_ask"):
            return
        from .post_cards import POST_PLAN, plan_entry

        for _ in range(3):
            index = self.state.next_plan_index(len(POST_PLAN))
            format_id, cards = plan_entry(index)
            entry_id = self.state.add_post_artifact(
                kind="plan", text=json.dumps({"index": index, "format": format_id, "cards": [c["id"] for c in cards]}),
                source="auto",
            )
            trace: dict = {}
            try:
                post = generate.value_post(
                    self.cfg, getattr(self, "original_anthropic", self.anthropic), format_id=format_id,
                    cards=cards, recent_posts=self.state.recent_texts(("post", "post_draft"), 10), trace=trace,
                )
            except GenerationBudgetOpen:
                raise
            except Exception as exc:
                self.state.finish_post_artifact(entry_id, status="failed", note=f"{type(exc).__name__}: {exc}")
                log(f"plan post {index} error: {type(exc).__name__}: {exc}")
                continue
            if not post:
                self.state.finish_post_artifact(entry_id, status="failed", note=str(trace.get("rejected") or "no post"))
                log(f"plan post {index} ({format_id}) failed: {trace.get('rejected')}")
                continue
            entry = {"id": entry_id, "kind": "plan", "text": "", "photo_file_id": ""}
            if self.post_drafts:
                await self.approval.send_post_draft(post_text=post["text"], topic=f"plan post · {format_id}")
                self.state.record("post_draft", post["text"])
                self.state.finish_post_artifact(entry_id, status="drafted", note=format_id)
                filled = True
            else:
                filled = await self._publish_artifact_post(
                    entry, [{"shape": format_id, "text": post["text"]}],
                    artifact_text="\n".join(c["text"] for c in cards),
                )
            if filled:
                self.state.record("post_ask", f"slot filled by plan post {index} ({format_id})")
                return
        self.state.record("post_ask", "slot empty: three plan posts failed their checks")

    async def _news_step(self, now: float | None = None, fetch=None) -> None:
        """News posts (Oct 24 2026): within hours of an official Google Search
        change, one post on what changed and what to do, written from Google's
        own text (bot/news.py). At most one a day, inside the active window."""
        from . import news

        now = time.time() if now is None else now
        if not getattr(self.cfg, "news_posts", False):
            return
        if now - getattr(self, "_news_checked_at", 0.0) < news.CHECK_SECONDS:
            return
        self._news_checked_at = now
        fetch = fetch or news.fetch
        items: list[dict] = []
        for url, parse in ((news.BLOG_FEED, news.blog_items), (news.STATUS_FEED, news.status_items)):
            try:
                items += parse(await asyncio.to_thread(fetch, url))
            except Exception as exc:
                log(f"news feed unreadable ({url}): {type(exc).__name__}: {exc}")
        if not items:
            return
        seeding = not self.state.news_seen_any()
        added = self.state.add_news_items(items, seeding=seeding, now=now)
        if seeding:
            log(f"news: {added} existing items recorded as seen (never posted)")
            return
        bounds = pacing.active_window_bounds(self.cfg, now)
        if bounds is None or self.state.news_attempts_since(bounds[0]) >= news.NEWS_POSTS_PER_DAY:
            return
        item = self.state.next_news_item(fresh_seconds=news.FRESH_SECONDS, now=now)
        if item is None:
            return
        if not news.write_budget_allows_news(self.cfg, self.state, now):
            # Waits (still 'new'): a later day or month may have room within FRESH_SECONDS.
            log(f"news post waits: the write caps are kept for the plan slots ({item['title']})")
            return
        cards = await asyncio.to_thread(news.news_cards, item, fetch)
        trace: dict = {}
        try:
            post = generate.value_post(
                self.cfg, getattr(self, "original_anthropic", self.anthropic), format_id="search_news", cards=cards,
                recent_posts=self.state.recent_texts(("post", "post_draft"), 10), trace=trace,
            )
        except GenerationBudgetOpen:
            raise
        except Exception as exc:
            self.state.finish_news_item(item["id"], status="failed", note=f"{type(exc).__name__}: {exc}", now=now)
            log(f"news post error ({item['title']}): {type(exc).__name__}: {exc}")
            return
        if not post:
            self.state.finish_news_item(item["id"], status="failed", note=str(trace.get("rejected") or "no post"), now=now)
            log(f"news post failed ({item['title']}): {trace.get('rejected')}")
            return
        if self.post_drafts:
            await self.approval.send_post_draft(post_text=post["text"], topic="news post")
            self.state.record("post_draft", post["text"])
            self.state.finish_news_item(item["id"], status="published", note="drafted", now=now)
            return
        entry_id = self.state.add_post_artifact(kind="news", text=json.dumps({"news": item["id"]}), source="news")
        published = await self._publish_artifact_post(
            {"id": entry_id, "kind": "news", "text": "", "photo_file_id": ""},
            [{"shape": "search_news", "text": post["text"]}],
            artifact_text="\n".join(c["text"] for c in cards),
        )
        self.state.finish_news_item(item["id"], status="published" if published else "failed",
                                    note="published" if published else "publish refused", now=now)

    async def maybe_standalone(self):
        if str(getattr(self.cfg, "post_prompt_version", "v1")) == "v4":
            try:
                await self._news_step()
            except GenerationBudgetOpen:
                raise
            except Exception as exc:  # a news problem never stops the plan posts
                log(f"news step error: {type(exc).__name__}: {exc}")
            await self._value_posts_step()
            return
        if str(getattr(self.cfg, "post_prompt_version", "v1")) == "v3":
            await self._artifact_posts_step()
            return
        ledger_kind = "post_draft" if self.post_drafts else "post"
        if not pacing.should_standalone_post(self.cfg, self.state, kind=ledger_kind):
            return
        slot = pacing.standalone_slot_key(self.cfg)
        if slot is None or not self.state.reserve_generation_attempt(
            kind="standalone_post",
            bucket=slot,
            limit=self.cfg.pacing.standalone_generation_attempts_per_slot,
        ):
            return
        trace: dict = {}
        try:
            window_start, slot_index = (int(part) for part in slot.split(":", 1))
            rotation_index = (
                (window_start // 86400)
                * max(1, int(self.cfg.pacing.standalone_posts_per_day))
                + slot_index
            )
        except (TypeError, ValueError):
            rotation_index = None
        try:
            text = generate.standalone_post(
                self.cfg,
                getattr(self, "original_anthropic", self.anthropic),
                recent_posts=self.state.recent_texts(("post", "post_draft"), 10),
                evidence_rotation_index=rotation_index,
                trace=trace,
            )
        except GenerationBudgetOpen as exc:
            self.state.record_generation_outcome(
                kind="standalone_post",
                bucket=slot,
                outcome="budget_held",
                reason=str(exc),
                evidence_id=str(trace.get("evidence_id") or ""),
            )
            raise
        except Exception as e:
            self.state.record_generation_outcome(
                kind="standalone_post",
                bucket=slot,
                outcome="error",
                reason=str(e),
                evidence_id=str(trace.get("evidence_id") or ""),
            )
            log(f"post gen error slot={slot}: {e}")
            return
        if not text:
            reason = str(trace.get("rejection") or "quality gate returned no post")
            self.state.record_generation_outcome(
                kind="standalone_post",
                bucket=slot,
                outcome="quality_rejected",
                reason=reason,
                evidence_id=str(trace.get("evidence_id") or ""),
            )
            log(
                "POST REJECTED "
                f"slot={slot} evidence={trace.get('evidence_id') or 'unknown'} "
                f"reason={reason}"
            )
            return
        if text and self.post_drafts:
            await self._deliver_post_draft(slot, text, trace)
            return
        if text:
            log(f"POST: {text!r}")
            published = await poster.post(self.cfg, self.state, text)
            if published:
                if self.cfg.dry_run:
                    log("dry-run post validated; live action ledger unchanged")
                else:
                    self.state.record("post", text)
                self.state.record_generation_outcome(
                    kind="standalone_post",
                    bucket=slot,
                    outcome="validated" if self.cfg.dry_run else "published",
                    evidence_id=str(trace.get("evidence_id") or ""),
                )
            else:
                self.state.record_generation_outcome(
                    kind="standalone_post",
                    bucket=slot,
                    outcome="publish_failed",
                    reason="publisher returned false",
                    evidence_id=str(trace.get("evidence_id") or ""),
                )
                log(
                    "POST PUBLISH FAILED "
                    f"slot={slot} evidence={trace.get('evidence_id') or 'unknown'}"
                )

    async def _deliver_post_draft(self, slot: str, text: str, trace: dict) -> None:
        evidence_id = str(trace.get("evidence_id") or "")
        try:
            sent = await self.approval.send_post_draft(post_text=text)
        except TelegramApprovalError as exc:
            self.state.record_generation_outcome(
                kind="standalone_post",
                bucket=slot,
                outcome="notify_failed",
                reason=str(exc),
                evidence_id=evidence_id,
            )
            log(f"POST DRAFT NOT DELIVERED slot={slot}: {exc}")
            return
        self.state.record("post_draft", text)
        self.state.record_generation_outcome(
            kind="standalone_post",
            bucket=slot,
            outcome="notified",
            evidence_id=evidence_id,
        )
        log(
            f"POST DRAFT sent slot={slot} evidence={evidence_id or 'unknown'} "
            f"message={sent.get('message_id')}: {text!r}"
        )

    async def standalone_loop(self):
        """Evaluate standalone posting independently of incoming replies."""
        while True:
            try:
                await self.maybe_standalone()
            except GenerationBudgetOpen as exc:
                log(f"standalone post held by generation budget: {exc}")
                await asyncio.sleep(300)
                continue
            await asyncio.sleep(60)

    async def balance_loop(
        self,
        api: stream.TwitterApi,
        guard: DeliveryGuard,
    ):
        while True:
            await asyncio.sleep(self.cfg.detection.balance_check_seconds)
            guard.verify_balance(await api.get_balance_credits())

    async def ingest_loop(
        self,
        ws,
        guard: DeliveryGuard,
        queue: asyncio.Queue,
        expected_ids: set[str],
        expected_tags: set[str],
    ):
        """Meter the paid stream immediately; slow generation runs separately."""
        async for event in stream.iter_owned_tweets(
            ws,
            expected_rule_ids=expected_ids,
            expected_rule_tags=expected_tags,
        ):
            snapshot = guard.observe(event)
            if snapshot.classification in {
                "bootstrap_stale",
                "bootstrap_replay",
                "connection_replay",
            }:
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull as exc:
                raise CostCircuitOpen(
                    "generation queue overflowed; paid detection is outrunning processing"
                ) from exc

    async def processing_loop(self, queue: asyncio.Queue):
        while True:
            event = await queue.get()
            try:
                await self.handle_tweet(event)
            finally:
                queue.task_done()

    async def stream_supervisor(
        self,
        first_ws,
        guard: DeliveryGuard,
        queue: asyncio.Queue,
        expected_ids: set[str],
        expected_tags: set[str],
    ):
        """Reconnect the websocket without mutating or reinstalling paid rules."""
        try:
            await self.ingest_loop(
                first_ws,
                guard,
                queue,
                expected_ids,
                expected_tags,
            )
        except stream.StreamDisconnected as exc:
            log(f"websocket disconnected; reconnecting in-process: {exc}")

        while True:
            guard.begin_connection()
            reconnect_delay = reconnect_delay_seconds(self.cfg)
            log(
                "waiting "
                f"{reconnect_delay}s for TwitterAPI.io to release its websocket slot"
            )
            await asyncio.sleep(reconnect_delay)
            try:
                async with stream.open_tweet_websocket(
                    self.cfg.twitterapi_key
                ) as ws:
                    log(
                        "websocket reconnected without changing the paid rule set "
                        f"(reconnect={guard.reconnects})"
                    )
                    await self.ingest_loop(
                        ws,
                        guard,
                        queue,
                        expected_ids,
                        expected_tags,
                    )
            except stream.StreamDisconnected as exc:
                log(f"websocket disconnected again: {exc}")

    def _approval_window_seconds(self) -> int:
        """Deadline for sending, and keeping open, a human-approval draft."""
        window = int(self.cfg.human_approval.approval_window_seconds or 0)
        return window if window > 0 else int(self.cfg.pacing.max_tweet_age_seconds)

    async def run(self):
        approval_mode = self.cfg.human_approval.enabled
        log(
            f"daemon start | dry_run={self.cfg.dry_run} | model={self.cfg.model} "
            f"| targets={len(self.cfg.targets)} | "
            f"mode={'post-drafts' if self.post_drafts else 'human-approval' if approval_mode else 'autonomous'} | "
            f"cap_today={pacing.daily_cap(self.cfg, time.time())}"
        )

        if not self.cfg.detection.enabled:
            log("detection disabled; no billable rules created")
            if (
                self.post_drafts
                and not self.cfg.dry_run
                and self.cfg.pacing.standalone_posts_per_day > 0
            ):
                verification = await self.approval.verify()
                if verification["chat_id"] != self.cfg.telegram_chat_id:
                    raise RuntimeError(
                        "Telegram destination verification failed; "
                        "post drafts not activated"
                    )
                log(
                    "original post drafts go to Telegram for a human Post tap "
                    f"({self.cfg.pacing.standalone_posts_per_day}/day)"
                )
                await self.standalone_loop()
                return
            if (
                not self.cfg.dry_run
                and self.cfg.publishing.enabled
                and self.cfg.pacing.standalone_posts_per_day > 0
            ):
                if not await check_session_alive(self.cfg):
                    raise RuntimeError(
                        "official X account preflight failed; "
                        "standalone publishing not activated"
                    )
                log("official standalone-post scheduler active without reply detection")
                await self.standalone_loop()
            return
        if not self.cfg.targets:
            log("no targets configured; no billable rules created")
            return
        if self.cfg.dry_run and not self.cfg.detection.allow_billable_detection_in_dry_run:
            log("dry_run blocks billable detection; enable the explicit detection canary flag")
            return
        if not self.cfg.twitterapi_key:
            raise RuntimeError("TWITTERAPI_IO_KEY is missing")
        credits_today = self.state.provider_credits_today()
        credits_month = self.state.provider_credits_month()
        self.enforce_authoritative_provider_caps()
        if credits_today >= self.cfg.detection.daily_credit_cap:
            raise CostCircuitOpen(
                f"daily detection cap already reached: {credits_today} credits"
            )
        if credits_month >= self.cfg.detection.monthly_credit_cap:
            raise CostCircuitOpen(
                f"monthly detection cap already reached: {credits_month} credits"
            )

        # Do not pay to detect tweets when the live posting session cannot act.
        detection_handles = self.cfg.handles
        if not self.cfg.dry_run:
            if approval_mode:
                verification = await self.approval.verify()
                if verification["chat_id"] != self.cfg.telegram_chat_id:
                    raise RuntimeError(
                        "Telegram destination verification failed; "
                        "paid detection not activated"
                    )
                log(
                    "human approval channel ready"
                    + (
                        f" (@{verification['bot_username']})"
                        if verification["bot_username"]
                        else ""
                    )
                )
            else:
                if not self.cfg.publishing.enabled:
                    raise RuntimeError(
                        "official X publishing is disabled; detection not activated"
                    )
                if (
                    self.cfg.publishing.require_ai_reply_approval
                    and not self.cfg.x_ai_reply_approval_reference
                ):
                    raise RuntimeError(
                        "X AI reply approval reference is missing; "
                        "paid detection not activated"
                    )
                detection_handles = self.cfg.consented_reply_handles
                if not detection_handles:
                    raise RuntimeError(
                        "no target has auditable automated-reply opt-in; "
                        "paid detection not activated"
                    )
                if not await check_session_alive(self.cfg):
                    raise RuntimeError(
                        "official X account preflight failed; detection not activated"
                    )

        async with stream.TwitterApi(self.cfg.twitterapi_key) as api:
            # The provider has a second account-monitor stream which bypasses
            # filter rule tags. It must stay empty for attributable billing.
            await api.assert_no_monitored_users()
            pre_setup_balance = await api.get_balance_credits()
            self.state.record_provider_balance(pre_setup_balance)
            self.enforce_authoritative_provider_caps()
            if pre_setup_balance < self.cfg.detection.minimum_balance_credits:
                raise CostCircuitOpen(
                    f"provider balance below startup reserve: {pre_setup_balance} < "
                    f"{self.cfg.detection.minimum_balance_credits} credits"
                )

            rules_installed = False
            cleanup_error: Exception | None = None
            try:
                if not self.state.reserve_provider_paid_start(
                    self.cfg.detection.max_paid_starts_per_day
                ):
                    raise CostCircuitOpen(
                        "paid provider startup limit reached for today; "
                        "refusing another rule lifecycle"
                    )
                staged_rules = await stream.stage_rules(
                    api,
                    detection_handles,
                    interval_seconds=self.cfg.stream_interval_seconds,
                    namespace=self.cfg.detection.rule_namespace,
                )
                rules_installed = True
                expected_ids = {rule.rule_id for rule in staged_rules}
                expected_tags = {rule.tag for rule in staged_rules}
                async with stream.open_tweet_websocket(self.cfg.twitterapi_key) as ws:
                    rules = await stream.activate_staged_rules(api, staged_rules)
                    post_setup_balance = await api.get_balance_credits()
                    self.state.record_provider_balance(post_setup_balance)
                    self.enforce_authoritative_provider_caps()
                    immediate_setup_credits = max(
                        0, pre_setup_balance - post_setup_balance
                    )
                    expected_setup_credits = (
                        len(rules)
                        * self.cfg.detection.credits_per_rule_activation
                    )
                    setup_credits = max(immediate_setup_credits, expected_setup_credits)
                    delayed_setup_credits = max(
                        0, expected_setup_credits - immediate_setup_credits
                    )
                    if setup_credits:
                        self.state.record_provider_delivery(
                            tweet_id="",
                            rule_id="",
                            author="",
                            classification="control_setup",
                            credits=setup_credits,
                        )
                    if setup_credits > self.cfg.detection.max_control_plane_setup_credits:
                        raise CostCircuitOpen(
                            f"provider setup cost anomaly: {setup_credits} > "
                            f"{self.cfg.detection.max_control_plane_setup_credits} credits"
                        )
                    log(
                        "provider control-plane setup reserve="
                        f"{setup_credits} credits "
                        f"(immediate={immediate_setup_credits}, "
                        f"delayed={delayed_setup_credits})"
                    )
                    log(
                        f"websocket ready before activating {len(rules)} canonical "
                        f"filter rule(s) for {len(detection_handles)} eligible targets"
                    )
                    guard = DeliveryGuard(
                        self.cfg.detection,
                        self.state,
                        detection_handles,
                        starting_balance_credits=post_setup_balance,
                    )
                    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(
                            self.stream_supervisor(
                                ws,
                                guard,
                                queue,
                                expected_ids,
                                expected_tags,
                            ),
                            name="metered-twitter-ingest",
                        )
                        tasks.create_task(
                            self.processing_loop(queue), name="reply-processing"
                        )
                        tasks.create_task(
                            self.balance_loop(api, guard), name="provider-balance-guard"
                        )
                        if self.cfg.publishing.enabled:
                            tasks.create_task(
                                self.standalone_loop(),
                                name="standalone-post-scheduler",
                            )
            finally:
                if rules_installed:
                    try:
                        pre_cleanup_balance = await api.get_balance_credits()
                        self.state.record_provider_balance(pre_cleanup_balance)
                        deactivated = await stream.deactivate_all_rules_with_api(api)
                        post_cleanup_balance = await api.get_balance_credits()
                        self.state.record_provider_balance(post_cleanup_balance)
                        immediate_cleanup_credits = max(
                            0, pre_cleanup_balance - post_cleanup_balance
                        )
                        expected_cleanup_credits = (
                            deactivated
                            * self.cfg.detection.credits_per_rule_cleanup
                        )
                        cleanup_credits = max(
                            immediate_cleanup_credits,
                            expected_cleanup_credits,
                        )
                        if cleanup_credits:
                            self.state.record_provider_delivery(
                                tweet_id="",
                                rule_id="",
                                author="",
                                classification="control_cleanup",
                                credits=cleanup_credits,
                            )
                        if (
                            cleanup_credits
                            > self.cfg.detection.max_control_plane_cleanup_credits
                        ):
                            raise CostCircuitOpen(
                                f"provider cleanup cost anomaly: {cleanup_credits} > "
                                f"{self.cfg.detection.max_control_plane_cleanup_credits} credits"
                            )
                        log(
                            "shutdown: verified canonical rules inactive after "
                            f"deactivating {deactivated}"
                        )
                        log(
                            "provider control-plane cleanup reserve="
                            f"{cleanup_credits} credits "
                            f"(immediate={immediate_cleanup_credits})"
                        )
                        self.enforce_authoritative_provider_caps()
                    except Exception as exc:
                        cleanup_error = exc
                        log(f"FATAL: provider rule cleanup could not be verified: {exc}")
                if cleanup_error is not None:
                    raise cleanup_error


if __name__ == "__main__":
    with daemon_lock():
        asyncio.run(Daemon().run())
