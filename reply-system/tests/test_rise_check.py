"""Rise check (R3, Oct 19 2026): a free same-day re-read of each drafted post.

Our replies get about 0.2% of the source post's impressions, so the source's
size decides most of a reply's reach. X does not bill a post read again on the
same UTC day (its usage counter showed 186 posts against 191 in our ledger
after 30 re-reads), so the bot can look at each drafted post again at about
90 seconds and an hour, store its early numbers, and show the live numbers on
the draft so the fastest risers get posted first.
"""
import time
from datetime import datetime, timezone

from bot.replies_daemon import OfficialReplyRunner, rise_line
from tests.test_telegram_approval import cfg, run, state

DAY = datetime(2026, 10, 20, 15, tzinfo=timezone.utc).timestamp()


def _draft(s, tweet_id, *, source_created_at, author="alice", message_id=7, read_at=None):
    s.record_x_stream_read(post_id=tweet_id, author=author, cost_microusd=5000,
                           now=source_created_at + 5 if read_at is None else read_at)
    s.reserve_reply_draft(tweet_id=tweet_id, author=author, source_text="the source post",
                          reply_text="the draft", source_created_at=source_created_at,
                          intent_url="u", expires_at=source_created_at + 900, now=source_created_at + 10)
    s.complete_reply_draft_notification(tweet_id=tweet_id, telegram_chat_id="1", telegram_message_id=message_id)


def test_rise_line_is_short_and_readable():
    metrics = {"impression_count": 2412, "like_count": 31, "reply_count": 9}
    assert rise_line(92, metrics) == "Now (1m 32s): 2.4K views · 31 likes · 9 replies"
    assert rise_line(3700, {"impression_count": 1_250_000, "like_count": 1, "reply_count": 1}) == (
        "Now (1h 1m): 1.2M views · 1 like · 1 reply")
    assert rise_line(45, {}) == "Now (45s): 0 views · 0 likes · 0 replies"


def test_only_drafts_read_today_and_not_yet_checked_are_due():
    s = state()
    _draft(s, "fresh", source_created_at=DAY - 100)
    _draft(s, "young", source_created_at=DAY - 30, message_id=8)
    _draft(s, "stale", source_created_at=DAY - 1200, message_id=9)
    # Read by the stream yesterday: a re-read today would be billed again.
    _draft(s, "yesterday", source_created_at=DAY - 120, message_id=10, read_at=DAY - 86_400)
    due = s.drafts_due_rise_check(kind="rise_90s", min_age=90, max_age=600, now=DAY)
    assert [d["tweet_id"] for d in due] == ["fresh"]
    assert due[0]["telegram_message_id"] == 7 and due[0]["author"] == "alice"
    s.record_source_snapshot(post_id="fresh", kind="rise_90s", age_seconds=100,
                             metrics={"impression_count": 10}, now=DAY)
    assert s.drafts_due_rise_check(kind="rise_90s", min_age=90, max_age=600, now=DAY) == []
    later = s.drafts_due_rise_check(kind="rise_60m", min_age=60, max_age=600, now=DAY)
    assert [d["tweet_id"] for d in later] == ["fresh"], "each step is checked once"


def test_edit_keeps_the_buttons_and_adds_the_live_line(monkeypatch):
    from bot.telegram_approval import TelegramApprovalClient

    client = TelegramApprovalClient(cfg())
    calls = []

    async def fake_post(method, payload, **_):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 7, "chat": {"id": 12345}}}

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "phone_push", lambda **_: _noop())
    draft = dict(tweet_id="20", author="alice", source_text="s", reply_text="r",
                 source_age_seconds=11, expires_in_seconds=900, author_reach="230K views/post")
    run(client.send_reply_draft(**draft))
    run(client.edit_reply_draft(message_id=7, live_line="Now (1m 32s): 2.4K views · 31 likes · 9 replies", **draft))
    (_, sent), (method, edited) = calls
    assert method == "editMessageText" and edited["message_id"] == 7
    assert edited["reply_markup"] == sent["reply_markup"]
    assert edited["text"].startswith(sent["text"].split("\n\n")[0] + "\nNow (1m 32s): 2.4K views")
    assert edited["text"].endswith(sent["text"].split("\n\n", 1)[1])


async def _noop():
    return False


class _FakeApi:
    def __init__(self, metrics):
        self.metrics = metrics
        self.calls = []

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        ids = kwargs["params"]["ids"].split(",")
        return {"data": [{"id": i, "public_metrics": self.metrics} for i in ids]}


class _FakeApproval:
    def __init__(self):
        self.edits = []

    async def edit_reply_draft(self, **kwargs):
        self.edits.append(kwargs)


class _Daemon:
    def __init__(self, s):
        self.cfg = cfg()
        self.state = s
        self.approval = _FakeApproval()

    def _author_reach(self, author):
        return "230K views/post"


def test_one_pass_rereads_for_free_stores_and_edits(monkeypatch):
    s = state()
    now = time.time()
    _draft(s, "p1", source_created_at=now - 95)
    runner = OfficialReplyRunner.__new__(OfficialReplyRunner)
    runner.daemon = _Daemon(s)
    runner.cfg = runner.daemon.cfg
    runner.cfg.x_detection.pricing_valid_through = "2099-12-31"
    from bot.x_read_budget import XReadBudget
    runner.budget = XReadBudget(runner.cfg.x_detection, s)
    reads_before = s.db.execute("SELECT COUNT(*), SUM(cost_microusd) FROM x_stream_reads").fetchone()
    api = _FakeApi({"impression_count": 2412, "like_count": 31, "reply_count": 9})

    run(runner._rise_check_once(api))

    assert len(api.calls) == 1 and api.calls[0][2]["params"]["tweet.fields"] == "public_metrics"
    assert s.db.execute("SELECT COUNT(*), SUM(cost_microusd) FROM x_stream_reads").fetchone() == reads_before
    snap = s.db.execute("SELECT post_id, kind, impressions, likes, replies FROM source_snapshots").fetchall()
    assert snap == [("p1", "rise_90s", 2412, 31, 9)]
    (edit,) = runner.daemon.approval.edits
    assert edit["message_id"] == 7 and edit["live_line"].endswith("2.4K views · 31 likes · 9 replies")
    assert edit["author_reach"] == "230K views/post"
    run(runner._rise_check_once(api))
    assert len(api.calls) == 1, "a checked draft is not read again at the same step"


def test_a_failing_rise_check_is_logged_and_the_loop_keeps_going(monkeypatch):
    import asyncio
    import pytest
    from bot import replies_daemon

    calls = []

    class FlakyState:
        def drafts_due_rise_check(self, **_):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            if len(calls) >= 4:
                raise asyncio.CancelledError()
            return []

    runner = OfficialReplyRunner.__new__(OfficialReplyRunner)
    runner.daemon = type("D", (), {"state": FlakyState()})()
    runner.cfg = cfg()
    monkeypatch.setattr(replies_daemon, "RISE_CHECK_POLL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        run(runner._rise_check_loop())
    assert len(calls) >= 4


def test_restart_notice_is_silent_while_always_is_on(monkeypatch):
    """Oct 19 2026: six deploys sent six 10-line 'controls are ready' pushes in
    one evening. With /always on, the notice still lands in the chat, silently."""
    from bot.telegram_approval import TelegramApprovalClient

    client = TelegramApprovalClient(cfg())
    payloads = []

    async def fake_post(method, payload, **_):
        payloads.append(payload)
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(client, "_post", fake_post)
    s = state()
    runner = OfficialReplyRunner.__new__(OfficialReplyRunner)
    runner.daemon = _Daemon(s)
    runner.daemon.approval = client
    runner.cfg = runner.daemon.cfg
    runner._status_text = lambda: "status"
    s.set_operator_continuous(True)
    run(runner._announce_ready())
    s.set_operator_continuous(False)
    run(runner._announce_ready())
    assert payloads[0]["disable_notification"] is True and "controls are ready" in payloads[0]["text"]
    assert "disable_notification" not in payloads[1]
