"""Failure-mode tests for the human-approved Telegram reply path."""
import asyncio
import json
import os
import random
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from bot import pacing as pacing_module
from bot.config import Config, HumanApproval, Pacing
from bot.daemon import Daemon
from bot.replies_daemon import OfficialReplyRunner
from bot.state import State
from bot.telegram_approval import (
    TelegramApprovalClient,
    TelegramApprovalError,
    build_x_reply_intent,
    parse_shift_command,
)


def run(coro):
    return asyncio.run(coro)


def state():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return State(path)


def cfg() -> Config:
    return Config(
        env={
            "TELEGRAM_BOT_TOKEN": "bot-token",
            "TELEGRAM_CHAT_ID": "12345",
        },
        targets=[{"handle": "alice", "tier": "A"}],
        pacing=Pacing(
            active_hours_utc=(0, 24),
            min_tweet_chars=20,
            max_tweet_age_seconds=120,
        ),
        dry_run=False,
        model="unused",
        started_at_iso="",
        tier_weight={"A": 1.0},
        stream_interval_seconds=5,
        human_approval=HumanApproval(
            enabled=True,
            min_candidate_gap_seconds=0,
            per_account_cooldown_seconds=0,
            require_operator_active=False,
        ),
        reply_consents={},
    )


def test_x_intent_preserves_reply_target_and_copy():
    text = "The useful distinction is speed versus relevance."
    url = build_x_reply_intent("1900000000000000000", text, "@alice")
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "pentra.dev"
    assert parsed.path == "/x-reply"
    assert parsed.query == ""
    assert parse_qs(parsed.fragment) == {
        "id": ["1900000000000000000"],
        "text": [text],
        "author": ["alice"],
    }
    # X's own web intent works too (e.g. a desktop reply assistant).
    direct = urlsplit(build_x_reply_intent("1900000000000000000", text, "@alice", root="https://x.com/intent/post"))
    assert (direct.netloc, direct.path) == ("x.com", "/intent/post")
    assert parse_qs(direct.query) == {"in_reply_to": ["1900000000000000000"], "text": [text]}


def test_telegram_notification_uses_handoff_and_official_source_link():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 77,
                    "chat": {"id": 12345},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    approval = TelegramApprovalClient(cfg(), client=client)
    try:
        result = run(
            approval.send_reply_draft(
                tweet_id="source-1",
                author="alice",
                source_text="<b>source must be escaped</b>",
                reply_text="a useful, bounded reply",
                source_age_seconds=12,
                expires_in_seconds=108,
            )
        )
    finally:
        run(client.aclose())

    assert result["message_id"] == 77
    _, payload = requests[0]
    assert "&lt;b&gt;source must be escaped&lt;/b&gt;" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"]
    handoff_url = urlsplit(buttons[0][0]["url"])
    assert handoff_url.scheme == "https"
    assert handoff_url.netloc == "pentra.dev"
    assert handoff_url.path == "/x-reply"
    assert parse_qs(handoff_url.fragment) == {
        "id": ["source-1"],
        "text": ["a useful, bounded reply"],
        "author": ["alice"],
    }
    assert buttons[1][0]["copy_text"]["text"] == "a useful, bounded reply"
    assert buttons[2][0]["url"] == "https://x.com/alice/status/source-1"
    assert "api.x.com" not in json.dumps(payload)


def test_telegram_failure_is_fail_closed():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"ok": False, "description": "bot was blocked by the user"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    approval = TelegramApprovalClient(cfg(), client=client)
    try:
        with pytest.raises(TelegramApprovalError, match="bot was blocked"):
            run(
                approval.send_reply_draft(
                    tweet_id="source-1",
                    author="alice",
                    source_text="source",
                    reply_text="reply",
                    source_age_seconds=12,
                    expires_in_seconds=108,
                )
            )
    finally:
        run(client.aclose())


def test_long_reply_omits_telegram_copy_button_without_truncating_handoff():
    requests = []
    reply_text = "x" * 280

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 78,
                    "chat": {"id": 12345},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    approval = TelegramApprovalClient(cfg(), client=client)
    try:
        result = run(
            approval.send_reply_draft(
                tweet_id="1900000000000000000",
                author="alice",
                source_text="source",
                reply_text=reply_text,
                source_age_seconds=12,
                expires_in_seconds=108,
            )
        )
    finally:
        run(client.aclose())

    buttons = requests[0]["reply_markup"]["inline_keyboard"]
    assert [button[0]["text"] for button in buttons] == [
        "Open reply in X app",
        "View source post",
    ]
    handoff_query = parse_qs(urlsplit(result["intent_url"]).fragment)
    assert handoff_query["text"] == [reply_text]


def test_reply_draft_reservation_is_durable_and_idempotent():
    st = state()
    now = time.time()
    kwargs = {
        "tweet_id": "source-1",
        "author": "alice",
        "source_text": "source",
        "reply_text": "reply",
        "source_created_at": now,
        "intent_url": build_x_reply_intent("source-1", "reply"),
        "expires_at": now + 120,
        "now": now,
    }
    assert st.reserve_reply_draft(**kwargs)
    assert not st.reserve_reply_draft(**kwargs)
    assert st.reply_draft_status("source-1") == "reserved"
    st.complete_reply_draft_notification(
        tweet_id="source-1",
        telegram_chat_id="12345",
        telegram_message_id=77,
    )
    assert st.reply_draft_status("source-1") == "notified"
    assert st.reply_draft_count_today(now) == 1


def test_human_approval_daemon_notifies_without_autonomous_consent(monkeypatch):
    local_cfg = cfg()
    st = state()
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = local_cfg
    daemon.state = st
    daemon.anthropic = object()
    daemon.rng = random.Random(1)

    sent = []

    class FakeApproval:
        async def send_reply_draft(self, **kwargs):
            sent.append(kwargs)
            return {
                "chat_id": "12345",
                "message_id": 77,
                "intent_url": build_x_reply_intent(
                    kwargs["tweet_id"], kwargs["reply_text"]
                ),
            }

    daemon.approval = FakeApproval()
    def forbidden_random_veto(*_args, **_kwargs):
        raise AssertionError("human approval must not use the random tier veto")

    monkeypatch.setattr(pacing_module, "selectivity_pass", forbidden_random_veto)
    generation_options = []

    def generate_once(*_args, **kwargs):
        generation_options.append(kwargs)
        return {
            "action": "reply",
            "text": "one source-grounded thought",
        }

    monkeypatch.setattr(
        "bot.daemon.generate.decide",
        generate_once,
    )
    autonomous_calls = []

    async def forbidden_autonomous_publish(*args, **kwargs):
        autonomous_calls.append((args, kwargs))
        raise AssertionError("human approval mode must never publish through the API")

    monkeypatch.setattr("bot.daemon.poster.reply", forbidden_autonomous_publish)
    event = {
        "id_str": "source-1",
        "text": "this source tweet is long enough for the deterministic gate",
        "author": {"screen_name": "alice"},
        "created_at_epoch": time.time(),
    }

    run(daemon.handle_tweet(event))
    run(daemon.handle_tweet(event))

    assert len(sent) == 1
    assert len(generation_options) == 1
    assert generation_options[0]["secondary_audit"] is True
    assert generation_options[0]["human_review"] is True
    assert autonomous_calls == []
    assert st.reply_draft_status("source-1") == "notified"
    assert st.count_today() == 0


def test_approval_spacing_queues_next_valid_draft_instead_of_dropping_it(
    monkeypatch,
):
    local_cfg = cfg()
    local_cfg.human_approval.min_candidate_gap_seconds = 0.02
    st = state()
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = local_cfg
    daemon.state = st
    daemon.anthropic = object()
    daemon.rng = random.Random(1)
    daemon.approval_send_lock = asyncio.Lock()

    sent = []

    class FakeApproval:
        async def send_reply_draft(self, **kwargs):
            sent.append(kwargs["tweet_id"])
            return {"chat_id": "12345", "message_id": len(sent)}

    daemon.approval = FakeApproval()
    monkeypatch.setattr(
        "bot.daemon.generate.decide",
        lambda *_args, **_kwargs: {
            "action": "reply",
            "text": "one source-grounded thought",
        },
    )
    now = time.time()
    first = {
        "id_str": "spacing-1",
        "text": "this first source is substantial enough for a useful reply",
        "author": {"screen_name": "alice"},
        "created_at_epoch": now,
    }
    second = {
        "id_str": "spacing-2",
        "text": "this second source is substantial enough for a useful reply",
        "author": {"screen_name": "bob"},
        "created_at_epoch": now,
    }

    async def deliver_both():
        await daemon.handle_tweet(first)
        await daemon.handle_tweet(second)

    run(deliver_both())

    assert sent == ["spacing-1", "spacing-2"]
    assert st.reply_draft_status("spacing-2") == "notified"
    held = st.db.execute(
        "SELECT COUNT(*) FROM generation_outcomes "
        "WHERE evidence_id='bob|original' AND outcome='held'"
    ).fetchone()[0]
    assert held == 0


def test_approval_daily_cap_blocks_before_generation(monkeypatch):
    local_cfg = cfg()
    local_cfg.human_approval.daily_candidate_cap = 1
    st = state()
    now = time.time()
    assert st.reserve_reply_draft(
        tweet_id="existing",
        author="bob",
        source_text="source",
        reply_text="reply",
        source_created_at=now,
        intent_url=build_x_reply_intent("existing", "reply"),
        expires_at=now + 120,
        now=now,
    )
    st.complete_reply_draft_notification(
        tweet_id="existing",
        telegram_chat_id="12345",
        telegram_message_id=1,
    )

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = local_cfg
    daemon.state = st
    daemon.anthropic = object()
    daemon.rng = random.Random(1)
    generated = False

    def forbidden_generation(*_args, **_kwargs):
        nonlocal generated
        generated = True
        raise AssertionError("daily-capped candidates must not reach the model")

    monkeypatch.setattr("bot.daemon.generate.decide", forbidden_generation)
    event = {
        "id_str": "source-2",
        "text": "this source tweet is long enough for the deterministic gate",
        "author": {"screen_name": "alice"},
        "created_at_epoch": now,
    }
    run(daemon.handle_tweet(event))
    assert not generated


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/on", ("on", 300)),
        ("/on 1h", ("on", 60)),
        ("/on 90m", ("on", 90)),
        ("/on 3h", ("on", 180)),
        ("/on@madmanxbot 2h", ("on", 120)),
        ("/always", ("always", None)),
        ("/off", ("off", None)),
        ("/status", ("status", None)),
        ("/on forever", None),
        ("hello", None),
    ],
)
def test_shift_commands(text, expected):
    assert parse_shift_command(
        text,
        default_minutes=300,
        max_minutes=480,
    ) == expected


def test_operator_shift_gates_paid_reply_candidates():
    local_cfg = cfg()
    local_cfg.human_approval.require_operator_active = True
    st = state()
    now = time.time()

    assert pacing_module.can_notify_reply(
        local_cfg,
        st,
        "alice",
        now,
    ) == (False, "operator unavailable")

    st.set_operator_continuous(True, now=now)
    assert pacing_module.can_notify_reply(
        local_cfg,
        st,
        "alice",
        now + 86400,
    ) == (True, "clear")

    st.set_operator_continuous(False, now=now)

    st.set_operator_active_until(now + 3600, now=now)
    assert pacing_module.can_notify_reply(
        local_cfg,
        st,
        "alice",
        now,
    ) == (True, "clear")

    assert pacing_module.can_notify_reply(
        local_cfg,
        st,
        "alice",
        now + 3601,
    ) == (False, "operator unavailable")


def test_telegram_update_offset_is_durable():
    st = state()
    assert st.telegram_update_offset() == 0
    st.set_telegram_update_offset(42)
    assert st.telegram_update_offset() == 42


def test_reply_runner_on_and_off_commands_control_paid_monitoring():
    class Approval:
        def __init__(self):
            self.messages = []

        async def send_message(self, text):
            self.messages.append(text)

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    daemon.state = state()
    daemon.approval = Approval()
    daemon.anthropic = type(
        "AnthropicPreflight", (), {"preflight": lambda self, model: None}
    )()
    runner = OfficialReplyRunner(daemon)

    assert not daemon.state.operator_is_active()
    run(runner._apply_command(("on", 300)))
    assert daemon.state.operator_is_active()
    assert "ACTIVE for 300 minutes" in daemon.approval.messages[-1]

    run(runner._apply_command(("off", None)))
    assert not daemon.state.operator_is_active()
    assert "paid X stream is closing now" in daemon.approval.messages[-1]


def test_reply_runner_always_command_is_durable_and_off_is_a_kill_switch():
    class Approval:
        def __init__(self):
            self.messages = []

        async def send_message(self, text):
            self.messages.append(text)

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    daemon.state = state()
    daemon.approval = Approval()
    daemon.anthropic = type(
        "AnthropicPreflight", (), {"preflight": lambda self, model: None}
    )()
    runner = OfficialReplyRunner(daemon)

    run(runner._apply_command(("always", None)))
    assert daemon.state.operator_is_continuous()
    assert daemon.state.operator_is_active(time.time() + 86400 * 30)
    assert "ACTIVE continuously" in daemon.approval.messages[-1]

    run(runner._apply_command(("off", None)))
    assert not daemon.state.operator_is_continuous()
    assert not daemon.state.operator_is_active()


def test_reply_runner_shift_waiter_stops_immediately_on_off():
    class Approval:
        async def send_message(self, _text):
            return None

    async def scenario():
        daemon = Daemon.__new__(Daemon)
        daemon.cfg = cfg()
        daemon.state = state()
        daemon.approval = Approval()
        daemon.anthropic = type(
            "AnthropicPreflight", (), {"preflight": lambda self, model: None}
        )()
        runner = OfficialReplyRunner(daemon)
        await runner._apply_command(("on", 300))
        waiter = asyncio.create_task(runner._wait_until_shift_stops())
        await asyncio.sleep(0)
        await runner._apply_command(("off", None))
        return await asyncio.wait_for(waiter, timeout=1)

    assert run(scenario()) == "off"


def test_reply_runner_preserves_timed_intent_when_generation_budget_is_closed():
    class Approval:
        def __init__(self):
            self.messages = []

        async def send_message(self, text):
            self.messages.append(text)

    class ClosedGeneration:
        def preflight(self, _model):
            from bot.llm_budget import GenerationBudgetOpen
            raise GenerationBudgetOpen("daily generation reserve exhausted")

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    daemon.state = state()
    daemon.approval = Approval()
    daemon.anthropic = ClosedGeneration()
    runner = OfficialReplyRunner(daemon)

    run(runner._apply_command(("on", 300)))

    assert daemon.state.operator_is_active()
    assert len(daemon.approval.messages) == 1
    assert "PAUSED for the requested 300-minute window" in daemon.approval.messages[0]
    assert "resume automatically after the UTC reset" in daemon.approval.messages[0]


def test_reply_runner_status_distinguishes_continuous_intent_from_capacity():
    class ClosedGeneration:
        def preflight(self, _model):
            from bot.llm_budget import GenerationBudgetOpen
            raise GenerationBudgetOpen("daily generation reserve exhausted")

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    daemon.state = state()
    daemon.anthropic = ClosedGeneration()
    runner = OfficialReplyRunner(daemon)
    daemon.state.set_operator_continuous(True)

    assert (
        "PAUSED (continuous; spend cap resets automatically)"
        in runner._status_text()
    )


def test_setup_discovers_private_and_group_chats_once_each():
    from scripts.telegram_setup import discover_chats

    updates = [
        {"message": {"chat": {"id": 1, "type": "private", "username": "owner"}}},
        {"message": {"chat": {"id": -100, "type": "supergroup", "title": "Pentra replies"}}},
        {"message": {"chat": {"id": 1, "type": "private", "username": "owner"}}},
        {"message": {"chat": {"id": -5, "type": "channel", "title": "ignored"}}},
    ]
    chats = discover_chats(updates)
    assert [chat["id"] for chat in chats] == [-100, 1]


def test_handoff_root_can_come_from_the_env_file_and_blank_means_default():
    from bot.telegram_approval import DEFAULT_X_REPLY_HANDOFF_ROOT, X_REPLY_HANDOFF_ROOT

    assert X_REPLY_HANDOFF_ROOT == DEFAULT_X_REPLY_HANDOFF_ROOT
    url = build_x_reply_intent("7", "text", "a", root="https://x.com/intent/post")
    assert url.startswith("https://x.com/intent/post?in_reply_to=7")


def test_env_file_handoff_root_reaches_the_telegram_button():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5, "chat": {"id": 12345}}})

    config = cfg()
    config.env["PENTRA_X_REPLY_HANDOFF_ROOT"] = "https://x.com/intent/post"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        run(TelegramApprovalClient(config, client=client).send_reply_draft(
            tweet_id="42",
            author="alice",
            source_text="source",
            reply_text="reply",
            source_age_seconds=1,
            expires_in_seconds=100,
        ))
    finally:
        run(client.aclose())
    button = urlsplit(requests[0]["reply_markup"]["inline_keyboard"][0][0]["url"])
    assert (button.netloc, button.path) == ("x.com", "/intent/post")
    assert parse_qs(button.query) == {"in_reply_to": ["42"], "text": ["reply"]}


def test_status_separates_a_one_off_measurement_from_stream_reads():
    from datetime import datetime, timezone

    class OpenGeneration:
        def preflight(self, _model):
            return None

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    today = datetime.now(timezone.utc).date().isoformat()
    daemon.cfg.x_detection.daily_microusd_cap = 500_000
    daemon.cfg.x_detection.incident_daily_baseline_day = today
    daemon.cfg.x_detection.incident_daily_baseline_microusd = 20_000   # 4 audit reads
    daemon.state = state()
    daemon.anthropic = OpenGeneration()
    for i in range(6):   # 4 audit reads + 2 stream reads
        daemon.state.record_x_stream_read(post_id=f"p{i}", author="a", cost_microusd=5_000)
    runner = OfficialReplyRunner(daemon)
    text = runner._status_text()
    assert "X reads today: 2/100 ($0.010) + 4 one-off measurement reads" in text


def test_post_draft_goes_to_telegram_with_an_x_app_handoff_and_no_x_write():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 91, "chat": {"id": 12345}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    approval = TelegramApprovalClient(cfg(), client=client)
    try:
        result = run(approval.send_post_draft(post_text="a <useful> post & more"))
    finally:
        run(client.aclose())

    assert result["message_id"] == 91
    url, payload = requests[0]
    assert "api.telegram.org" in url and "api.x.com" not in json.dumps(payload)
    assert "a &lt;useful&gt; post &amp; more" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"]
    handoff = urlsplit(buttons[0][0]["url"])
    assert (handoff.scheme, handoff.netloc, handoff.path) == ("https", "pentra.dev", "/x-reply")
    assert parse_qs(handoff.fragment) == {"kind": ["post"], "text": ["a <useful> post & more"]}
    assert buttons[1][0]["copy_text"]["text"] == "a <useful> post & more"


def test_post_drafts_mode_sends_one_draft_per_slot_and_never_publishes(monkeypatch):
    from bot import daemon as daemon_module

    c = cfg()
    c.pacing.standalone_posts_per_day = 2
    c.pacing.standalone_min_gap_seconds = 3600
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = c
    daemon.state = state()
    daemon.post_drafts = True
    daemon.original_anthropic = daemon.anthropic = object()
    sent = []

    class FakeApproval:
        async def send_post_draft(self, **kwargs):
            sent.append(kwargs)
            return {"message_id": len(sent)}

    async def never_publish(*args, **kwargs):
        raise AssertionError("post drafts never publish through the X API")

    def fake_post(cfg_, client, recent_posts=None, evidence_rotation_index=None, trace=None, **kwargs):
        trace["evidence_id"] = "page-two-refresh"
        return "an evidence-bound original post"

    daemon.approval = FakeApproval()
    monkeypatch.setattr(daemon_module.poster, "post", never_publish)
    monkeypatch.setattr(daemon_module.generate, "standalone_post", fake_post)

    run(daemon.maybe_standalone())
    run(daemon.maybe_standalone())  # same slot: already delivered

    assert sent == [{"post_text": "an evidence-bound original post"}]
    assert daemon.state.count_today("post_draft") == 1
    assert daemon.state.count_today("post") == 0
    outcomes = daemon.state.recent_generation_outcomes(kind="standalone_post")
    assert outcomes[0]["outcome"] == "notified"


def test_post_draft_telegram_failure_records_and_does_not_count_as_delivered(monkeypatch):
    from bot import daemon as daemon_module

    c = cfg()
    c.pacing.standalone_posts_per_day = 2
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = c
    daemon.state = state()
    daemon.post_drafts = True
    daemon.original_anthropic = daemon.anthropic = object()

    class FailingApproval:
        async def send_post_draft(self, **kwargs):
            raise TelegramApprovalError("Telegram sendMessage failed HTTP 502")

    daemon.approval = FailingApproval()
    monkeypatch.setattr(daemon_module.generate, "standalone_post", lambda *a, trace=None, **k: "a post")
    run(daemon.maybe_standalone())
    assert daemon.state.count_today("post_draft") == 0
    assert daemon.state.recent_generation_outcomes(kind="standalone_post")[0]["outcome"] == "notify_failed"


def test_each_draft_also_pushes_to_the_phone_and_tap_opens_the_x_handoff():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), json.loads(request.content)))
        if "ntfy.sh" in str(request.url):
            return httpx.Response(200, json={"id": "abc"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5, "chat": {"id": 12345}}})

    c = cfg()
    c.human_approval.phone_push_topic = "pentra-drafts-test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    approval = TelegramApprovalClient(c, client=client)
    try:
        result = run(approval.send_reply_draft(
            tweet_id="77", author="alice", source_text="source", reply_text="a sharp reply",
            source_age_seconds=10, expires_in_seconds=600,
        ))
    finally:
        run(client.aclose())

    assert "api.telegram.org" in requests[0][0]
    push_url, push = requests[1]
    assert push_url == "https://ntfy.sh/"
    assert push["topic"] == "pentra-drafts-test" and push["priority"] == 5
    assert push["click"] == result["intent_url"] and push["message"] == "a sharp reply"
    assert push["actions"][0]["url"] == result["intent_url"]


def test_a_failed_phone_push_never_fails_the_draft():
    async def handler(request: httpx.Request) -> httpx.Response:
        if "ntfy.sh" in str(request.url):
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 6, "chat": {"id": 12345}}})

    c = cfg()
    c.human_approval.phone_push_topic = "pentra-drafts-test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    approval = TelegramApprovalClient(c, client=client)
    try:
        result = run(approval.send_post_draft(post_text="a post"))
    finally:
        run(client.aclose())
    assert result["message_id"] == 6


# --- Oct 19 2026: daily reply window and X-credit notice --------------------
# Every reply @pentradev posted in Oct 2026 went up between 12:00 and 23:00
# UTC; /always spread the 90 daily reads over the whole day, so drafts written
# outside that window expired unposted and the evening ran out of reads.

def _ts_utc(hour, minute=0):
    from datetime import datetime, timezone
    return datetime(2026, 10, 19, hour, minute, tzinfo=timezone.utc).timestamp()


def test_shift_window_is_utc_hours_and_may_wrap_midnight():
    day = HumanApproval(shift_hours_utc=[12, 23])
    assert not day.in_shift_window(_ts_utc(11, 59)) and day.in_shift_window(_ts_utc(12))
    assert day.in_shift_window(_ts_utc(22, 59)) and not day.in_shift_window(_ts_utc(23))
    assert day.seconds_until_window_change(_ts_utc(10)) == 2 * 3600
    assert day.seconds_until_window_change(_ts_utc(20, 30)) == 2.5 * 3600
    night = HumanApproval(shift_hours_utc=[20, 4])
    assert night.in_shift_window(_ts_utc(23)) and night.in_shift_window(_ts_utc(3)) and not night.in_shift_window(_ts_utc(12))
    assert HumanApproval().in_shift_window(_ts_utc(3)) and HumanApproval().seconds_until_window_change() is None


def _window_runner(messages):
    class Approval:
        async def send_message(self, text):
            messages.append(text)

    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    daemon.cfg.human_approval.shift_hours_utc = [12, 23]
    daemon.state = state()
    daemon.approval = Approval()
    daemon.anthropic = type("AnthropicPreflight", (), {"preflight": lambda self, model: None})()
    return daemon, OfficialReplyRunner(daemon)


def test_always_monitoring_runs_only_inside_the_window_and_a_timed_shift_overrides_it():
    messages = []
    daemon, runner = _window_runner(messages)
    run(runner._apply_command(("always", None)))
    assert daemon.state.operator_is_continuous()
    assert daemon.state.operator_is_active(_ts_utc(15))
    assert not daemon.state.operator_is_active(_ts_utc(3))
    # An explicit /on shift is the owner asking for monitoring now.
    daemon.state.set_operator_active_until(_ts_utc(5))
    assert daemon.state.operator_is_active(_ts_utc(3))


def test_reply_runner_without_a_window_keeps_always_around_the_clock():
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg()
    daemon.state = state()
    daemon.approval = type("A", (), {"send_message": lambda self, text: None})()
    daemon.anthropic = type("AnthropicPreflight", (), {"preflight": lambda self, model: None})()
    OfficialReplyRunner(daemon)
    daemon.state.set_operator_continuous(True)
    assert daemon.state.operator_is_active(_ts_utc(3)) and daemon.state.shift_window is None


def test_x_credit_outage_is_announced_once_per_outage():
    messages = []
    _daemon, runner = _window_runner(messages)
    run(runner._x_credits_empty_notice())
    run(runner._x_credits_empty_notice())
    assert len(messages) == 1 and "X API credits are empty" in messages[0]
    runner.x_credit_notice_sent = False  # a stream session that ran re-arms it
    run(runner._x_credits_empty_notice())
    assert len(messages) == 2


def _approval_daemon(monkeypatch, decide):
    local_cfg = cfg()
    local_cfg.human_approval.min_candidate_gap_seconds = 0
    st = state()
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = local_cfg
    daemon.state = st
    daemon.anthropic = object()
    daemon.rng = random.Random(1)
    daemon.approval_send_lock = asyncio.Lock()
    sent = []

    class FakeApproval:
        async def send_reply_draft(self, **kwargs):
            sent.append(kwargs["tweet_id"])
            return {"chat_id": "12345", "message_id": len(sent)}

    daemon.approval = FakeApproval()
    monkeypatch.setattr("bot.daemon.generate.decide", decide)
    return daemon, st, sent


def test_one_authors_posts_are_drafted_one_at_a_time(monkeypatch):
    """Oct 19 2026: workers generated for sibling posts of one author before
    any draft existed, paying for calls the cooldown then discarded."""
    calls = []

    def slow_decide(*args, **_kwargs):
        calls.append(args[1])
        time.sleep(0.05)
        return {"action": "reply", "text": "one source-grounded thought"}

    daemon, st, sent = _approval_daemon(monkeypatch, slow_decide)
    daemon.cfg.human_approval.generation_worker_threads = 2
    daemon.cfg.human_approval.per_account_cooldown_seconds = 1800
    now = time.time()
    first = {"id_str": "sibling-1", "text": "the first post is long enough for the deterministic gate",
             "author": {"screen_name": "alice"}, "created_at_epoch": now}
    second = {"id_str": "sibling-2", "text": "the second post is long enough for the deterministic gate",
              "author": {"screen_name": "alice"}, "created_at_epoch": now}

    async def deliver_together():
        await asyncio.gather(daemon.handle_tweet(first), daemon.handle_tweet(second))

    run(deliver_together())
    assert sent == ["sibling-1"]
    assert len(calls) == 1, "the sibling waited and the author cooldown held it without a model call"


def test_freshness_is_measured_when_x_delivered_the_post(monkeypatch):
    """A post 60 s old on delivery that waited 40 s for a worker is still fresh."""
    daemon, st, sent = _approval_daemon(monkeypatch, lambda *_a, **_k: {"action": "reply", "text": "one source-grounded thought"})
    now = time.time()
    delivered = {"id_str": "fresh-1", "text": "a post that waited in the queue for a free worker",
                 "author": {"screen_name": "carol"}, "created_at_epoch": now - 100, "_received_at": now - 40}
    run(daemon.handle_tweet(delivered))
    assert sent == ["fresh-1"]
    stale = {"id_str": "stale-1", "text": "a post that was already old when it was delivered",
             "author": {"screen_name": "dave"}, "created_at_epoch": now - 200, "_received_at": now - 40}
    run(daemon.handle_tweet(stale))
    assert sent == ["fresh-1"]
