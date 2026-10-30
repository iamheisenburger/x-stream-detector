import json
import random
import time

from bot import pacing as pacing_module
from bot.config import HumanApproval
from bot.daemon import Daemon
from bot.replies_daemon import OfficialReplyRunner
from scripts.daily_summary import build_summary
from scripts.replay_drafts import load_posts, summarize
from tests.test_telegram_approval import cfg as approval_cfg, run, state


def _daemon(monkeypatch, tmp_path, **approval):
    config = approval_cfg()
    config.pacing.max_tweet_age_seconds = 90
    config.human_approval = HumanApproval(
        enabled=True, min_candidate_gap_seconds=0, per_account_cooldown_seconds=0,
        require_operator_active=False, candidate_ttl_seconds=900,
        approval_window_seconds=900, **approval,
    )
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = config
    daemon.state = state()
    daemon.anthropic = object()
    daemon.rng = random.Random(1)
    sent = []

    class FakeApproval:
        async def send_reply_draft(self, **kwargs):
            sent.append(kwargs)
            return {"chat_id": "1", "message_id": 1, "intent_url": "x"}

    daemon.approval = FakeApproval()
    monkeypatch.setattr(pacing_module, "selectivity_pass", lambda *_a, **_k: True)
    monkeypatch.setattr("bot.daemon.generate.decide",
                        lambda *_a, **_k: {"action": "reply", "text": "one grounded thought", "reason": "ok"})
    return daemon, sent


def _event(post_id, age=5):
    return {"id_str": post_id, "text": "this source tweet is long enough for the deterministic gate",
            "author": {"screen_name": "alice"}, "created_at_epoch": time.time() - age}


def test_observe_only_stores_drafts_without_sending_and_capture_logs_posts(monkeypatch, tmp_path):
    capture = tmp_path / "posts.jsonl"
    daemon, sent = _daemon(monkeypatch, tmp_path, observe_only=True, capture_posts_path=str(capture))
    run(daemon.handle_tweet(_event("p1")))
    assert sent == []
    assert daemon.state.reply_draft_status("p1") == "observed"
    assert daemon.state.reply_outcome_counts_today() == {"draft_observed": 1}
    line = json.loads(capture.read_text().splitlines()[0])
    assert line["id"] == "p1" and line["author"] == "alice" and "deterministic gate" in line["text"]
    stats = daemon.state.reply_latency_stats_today()
    assert stats["count"] == 1 and stats["median"] < 10


def test_normal_mode_still_sends(monkeypatch, tmp_path):
    daemon, sent = _daemon(monkeypatch, tmp_path)
    run(daemon.handle_tweet(_event("p2")))
    assert len(sent) == 1 and daemon.state.reply_draft_status("p2") == "notified"


def test_status_shows_speed_outcomes_and_observe_flag(monkeypatch, tmp_path):
    daemon, _ = _daemon(monkeypatch, tmp_path, observe_only=True)

    class OpenGeneration:
        def preflight(self, _model):
            return None

    run(daemon.handle_tweet(_event("p3")))
    daemon.anthropic = OpenGeneration()
    text = OfficialReplyRunner(daemon)._status_text()
    assert "OBSERVE-ONLY" in text
    assert "Speed today: post → draft median" in text
    assert "draft observed 1" in text


def test_morning_summary_reads_the_last_day(monkeypatch, tmp_path):
    daemon, _ = _daemon(monkeypatch, tmp_path)
    run(daemon.handle_tweet(_event("p4", age=12)))
    daemon.state.record_x_stream_read(post_id="p4", author="alice", cost_microusd=5000)
    text = build_summary(daemon.state.path, time.time(), 35)
    assert "Drafts: 1 sent" in text
    assert "Speed post → draft: median 12s" in text
    assert "X reads $0.01 (1)" in text
    assert "of $35" in text


def test_replay_helpers(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"id": "1", "author": "a", "text": "t", "is_reply": False},
        {"id": "1", "author": "a", "text": "t", "is_reply": False},
        {"id": "2", "author": "b", "text": "r", "is_reply": True},
        {"id": "3", "author": "c", "text": "u", "is_reply": False},
    ]) + "\nnot json\n")
    assert [p["id"] for p in load_posts(f, 10)] == ["1", "3"]
    s = summarize([{"action": "reply"}, {"action": "skip", "reason": "news"}, {"action": "skip", "reason": "news"}])
    assert s["drafts"] == 1 and s["draft_rate"] == 0.333 and s["top_skip_reasons"][0] == ("news", 2)


def test_timed_observation_ends_on_its_own(monkeypatch, tmp_path):
    from bot.config import HumanApproval
    timed = HumanApproval(observe_only_until_iso="2026-09-26T01:30:00+00:00")
    assert timed.observing(now=1_790_000_000 - 10**6)          # well before
    end = 1_790_386_200  # 2026-09-26T01:30:00Z
    assert timed.observing(now=end - 1)
    assert not timed.observing(now=end + 1)
    assert not HumanApproval().observing()
    assert HumanApproval(observe_only=True).observing()
    assert not HumanApproval(observe_only_until_iso="not a date").observing()

    daemon, sent = _daemon(monkeypatch, tmp_path, observe_only_until_iso="2000-01-01T00:00:00Z")
    run(daemon.handle_tweet(_event("after-window")))
    assert len(sent) == 1


def test_replay_interleaves_variants_and_restores_config():
    from types import SimpleNamespace
    from bot.llm_budget import GenerationBudgetOpen
    from scripts.replay_drafts import replay

    cfg = SimpleNamespace(reply_prompt_version="v1")
    seen = []

    def fake_decide(c, text, author, client, **_):
        seen.append((c.reply_prompt_version, text))
        if len(seen) == 3:
            raise GenerationBudgetOpen("daily cap")
        return {"action": "reply", "text": f"{c.reply_prompt_version}:{text}", "reason": ""}

    posts = [{"id": "1", "author": "a", "text": "one"}, {"id": "2", "author": "b", "text": "two"}]
    rows = replay(cfg, None, posts, ["v1", "v2"], decide=fake_decide)
    assert [(r["variant"], r["action"]) for r in rows] == [("v1", "reply"), ("v2", "reply"), ("v1", "budget_stop")]
    assert seen[:2] == [("v1", "one"), ("v2", "one")]
    assert cfg.reply_prompt_version == "v1"


def test_replay_can_carry_each_variants_own_recent_drafts_and_keep_traces():
    """Production passes the account's recent drafts to the writer and the
    repeated-frame guard; a replay without them can't test frame variety."""
    from types import SimpleNamespace
    from scripts.replay_drafts import replay

    cfg = SimpleNamespace(reply_prompt_version="v4")
    seen = []

    def fake_decide(c, text, author, client, recent_replies=None, trace=None, **_):
        seen.append((c.reply_prompt_version, list(recent_replies or [])))
        if trace is not None:
            trace["single_pass"] = {"text": text}
        return {"action": "reply", "text": f"{c.reply_prompt_version}:{text}", "reason": ""}

    posts = [{"id": str(i), "author": "a", "text": f"post {i}"} for i in range(3)]
    rows = replay(cfg, None, posts, ["v4", "v5"], decide=fake_decide, carry_recent=True, keep_trace=True)
    assert seen[4] == ("v4", ["v4:post 0", "v4:post 1"])
    assert seen[5] == ("v5", ["v5:post 0", "v5:post 1"])
    assert rows[0]["trace"] == {"single_pass": {"text": "post 0"}}
    seen.clear()
    replay(cfg, None, posts, ["v4"], decide=fake_decide)
    assert all(recent == [] for _, recent in seen), "default stays a clean-slate replay"


def test_v2_prompt_keeps_truth_rules_and_examples_pass_copy_guard():
    import re
    from bot import generate
    from bot.config import Config

    v2 = generate._reply_review_system_v2()
    for rule in ("Never invent experience", "Never mention or pitch", "medical",
                 "third-party news reactions", "source_quote", "reviews every draft"):
        assert rule in v2
    assert "classifier" in generate._reply_review_system_v2(require_candidate=True)
    assert generate._required_candidate_fallback_system_v2().startswith(v2[:200])
    examples = re.findall(r'post: "(.*)"\nreply: "(.*)"', generate._REPLY_EXAMPLES_V2)
    assert len(examples) == 6
    for post, reply in examples:
        assert generate._public_text_issue(reply, source_text=post) is None, reply
    assert Config.__dataclass_fields__["reply_prompt_version"].default == "v1"


def test_decide_uses_v2_prompt_only_when_configured(monkeypatch):
    from types import SimpleNamespace
    from bot import generate
    from tests.test_telegram_approval import cfg as approval_cfg

    systems = []

    class Messages:
        def create(self, **kwargs):
            systems.append(kwargs["system"][0]["text"])
            raise RuntimeError("stop after capturing the prompt")

    client = SimpleNamespace(messages=Messages())
    config = approval_cfg()
    for version in ("v1", "v2"):
        config.reply_prompt_version = version
        try:
            generate.decide(config, "a founder post about hiring your first salesperson and what to test",
                            "someone", client, human_review=True)
        except RuntimeError:
            pass
    assert "reply-guy" not in systems[0] and "How a good reply reads" not in systems[0]
    assert "How a good reply reads" in systems[1]


def test_travel_and_family_posts_are_never_forced_into_a_draft():
    from bot.generate import high_confidence_work_source
    fiji = ("Namale you are one of my absolute FAVORITE places on earth! Before heading to our event in "
            "Australia, we flew into Fiji and spent time with the people who make it so magical: our Namale "
            "team and their beautiful families. They are some of the most welcoming people you will ever meet.")
    assert not high_confidence_work_source(fiji)
    assert high_confidence_work_source(
        "We just shipped a new pricing page for our SaaS and the team measured conversion before and after the change."
    )


def test_draft_header_shows_the_authors_reach(monkeypatch, tmp_path):
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"targets": [
        {"handle": "alice", "medianViews": 229503.0},
        {"handle": "Big", "medianViews": 1132198.0},
    ]}))
    daemon, sent = _daemon(monkeypatch, tmp_path)
    monkeypatch.setattr(type(daemon), "REACH_POOL_PATH", str(pool))
    assert daemon._author_reach("@Alice") == "230K views/post"
    assert daemon._author_reach("big") == "1.1M views/post"
    assert daemon._author_reach("nobody") == ""
    run(daemon.handle_tweet(_event("reach-1")))
    assert sent[0]["author_reach"] == "230K views/post"


def test_telegram_message_puts_reach_next_to_the_author(monkeypatch):
    from bot.telegram_approval import TelegramApprovalClient
    from tests.test_telegram_approval import cfg as approval_cfg

    client = TelegramApprovalClient(approval_cfg())
    posted = {}

    async def fake_post(method, payload):
        posted.update(payload)
        return {"result": {"message_id": 7, "chat": {"id": 1}}}

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_chat_id", lambda: "1")
    run(client.send_reply_draft(tweet_id="20", author="alice", source_text="s", reply_text="r",
                                source_age_seconds=11, expires_in_seconds=900, author_reach="230K views/post"))
    assert "@alice · 230K views/post · 11s old" in posted["text"]
    run(client.send_reply_draft(tweet_id="21", author="bob", source_text="s", reply_text="r",
                                source_age_seconds=9, expires_in_seconds=900))
    assert "@bob · 9s old" in posted["text"]


def test_sanitizer_drops_the_mention_marker_but_keeps_the_name():
    from bot.generate import _public_text_issue, _sanitize_mechanical_reply
    text, changes = _sanitize_mechanical_reply("which part of @poteto's video made the call for you?", "Watched a @poteto video")
    assert text == "which part of poteto's video made the call for you?"
    assert "removed mention marker" in changes
    assert _public_text_issue(text, source_text="Watched a @poteto video") is None
    same, none = _sanitize_mechanical_reply("email me at hi@example.com", "")
    assert same == "email me at hi@example.com" and "removed mention marker" not in none


def test_morning_summary_reports_a_one_off_measurement_apart(monkeypatch, tmp_path):
    daemon, _ = _daemon(monkeypatch, tmp_path)
    for i in range(5):
        daemon.state.record_x_stream_read(post_id=f"m{i}", author="audit", cost_microusd=5000)
    daemon.state.record_x_stream_read(post_id="s1", author="alice", cost_microusd=5000)
    text = build_summary(daemon.state.path, time.time(), 35, one_off_read_microusd=20000)
    assert "X reads $0.01 (2) + one-off measurement $0.02 (4)" in text
    plain = build_summary(daemon.state.path, time.time(), 35)
    assert "X reads $0.03 (6)," in plain


def test_morning_summary_shows_the_draft_rate_for_the_last_three_days(monkeypatch, tmp_path):
    """The R2 exit (at least 40% of delivered posts become drafts, over 3 days)
    is read off the 00:55 summary. Measurement reads and a day's one-off
    audit reads are not posts delivered for drafting."""
    from datetime import datetime, timezone

    daemon, _ = _daemon(monkeypatch, tmp_path)
    s = daemon.state
    day = lambda d, h=15: datetime(2026, 10, d, h, tzinfo=timezone.utc).timestamp()
    for i in range(10):
        s.record_x_stream_read(post_id=f"a{i}", author="alice", cost_microusd=5000, now=day(17))
    for i in range(4):
        s.reserve_reply_draft(tweet_id=f"a{i}", author="alice", source_text="s", reply_text="r",
                              source_created_at=day(17) - 9, intent_url="u", expires_at=day(17) + 900, now=day(17))
        s.complete_reply_draft_notification(tweet_id=f"a{i}", telegram_chat_id="1", telegram_message_id=i)
    for i in range(3):
        s.record_x_stream_read(post_id=f"m{i}", author="measure:pentradev", cost_microusd=5000, now=day(19))
    for i in range(7):
        s.record_x_stream_read(post_id=f"c{i}", author="carol", cost_microusd=5000, now=day(19))
    for i in range(3):
        s.reserve_reply_draft(tweet_id=f"c{i}", author="carol", source_text="s", reply_text="r",
                              source_created_at=day(19) - 9, intent_url="u", expires_at=day(19) + 900, now=day(19))
        s.complete_reply_draft_notification(tweet_id=f"c{i}", telegram_chat_id="1", telegram_message_id=10 + i)
    text = build_summary(s.path, day(20, 0) + 3300, 35, one_off_reads_by_day={"2026-10-19": 2})
    assert "Draft rate (target 40%): Oct 17 40% (4/10), Oct 18 no posts, Oct 19 60% (3/5)" in text
