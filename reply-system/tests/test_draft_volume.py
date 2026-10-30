"""Draft-volume levers: keyword-scoped stream rules and a batch-friendly window."""
import json
import random
import time

from bot import pacing as pacing_module
from bot.config import HumanApproval
from bot.daemon import Daemon
from bot.telegram_approval import build_x_reply_intent
from bot.topics import matches_keywords
from bot.x_stream import MAX_RULE_VALUE, build_rule_values, canonical_rule_specs
from scripts.build_reply_pool import build_pool, load_candidates, on_topic_share
from scripts.select_x_reply_targets import (
    KEYWORD_SCOPED_YIELD_ASSUMPTION,
    build_report,
    expected_reads,
    reply_lane_targets,
    select_accounts,
)
from tests.test_reply_pool import WINDOW, candidates_file, row
from tests.test_telegram_approval import cfg as approval_cfg, run, state

KEYWORDS = json.load(open("config.json"))["x_detection"]["original_rule_keywords"]


def test_shipped_keywords_scope_every_original_rule_within_x_limits():
    handles = [f"account_{i:03d}" for i in range(400)]
    rules = build_rule_values(handles, KEYWORDS)
    assert all(len(rule) <= MAX_RULE_VALUE for rule in rules)
    assert all(rule.endswith("-is:reply -is:retweet lang:en") for rule in rules)
    # Every handle is covered by every keyword chunk (X bills a post once even
    # when several rules match it).
    for word in ("business", "marketing", "seo", "ai", '"indie hacker"'):
        covered = {h for rule in rules if f" {word} " in rule.replace("(", " ").replace(")", " ")
                   for h in handles if f"from:{h} " in rule + " " or f"from:{h})" in rule}
        assert covered == set(handles), word
    # No keywords keeps the reference author-only rules.
    assert build_rule_values(["a", "b"]) == ["(from:a OR from:b) -is:reply -is:retweet lang:en"]
    specs = canonical_rule_specs(["a"], "pentra-replies-v1", original_keywords=["seo"])
    assert specs[0].value == "(from:a) (seo) -is:reply -is:retweet lang:en"


def test_local_matcher_follows_whole_token_matching():
    assert matches_keywords("Our SEO: 3 lessons", ["seo"])
    assert not matches_keywords("a cranky founder-less take", ["rank", "founders"])
    assert matches_keywords("Indie-hacker life", ["indie hacker"])
    assert not matches_keywords("indie games, hacker news", ["indie hacker"])
    assert matches_keywords("anything", [])


def test_pool_estimates_delivered_posts_and_drops_accounts_with_none(tmp_path):
    cands = load_candidates(candidates_file(tmp_path, [
        {"handle": "ontopic", "category": "SEO and content"},
        {"handle": "offtopic", "category": "General business"},
    ]))
    on = row("ontopic", [300_000] * 4, days=[20, 21, 22, 23])
    for i, post in enumerate(on["posts"]):
        post["text"] = "google traffic tip" if i < 2 else "a sunset photo"
    off = row("offtopic", [300_000] * 4, days=[20, 21, 22, 23])
    for post in off["posts"]:
        post["text"] = "a sunset photo"
    audit = {**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00", "rows": [on, off]}

    pool = build_pool(cands, [audit], set(), keywords=KEYWORDS)
    assert [t["handle"] for t in pool["targets"]] == ["ontopic"]
    target = pool["targets"][0]
    assert target["onTopicShare"] == 0.5
    assert target["onTopicPostsPerDay"] == round(target["postsPerDay7"] * 0.5, 3)
    assert pool["keywordScoped"] is True
    assert {r["handle"]: r["reason"] for r in pool["rejected"]}["offtopic"].startswith("no sampled post")
    assert on_topic_share(off, []) == 1.0

    # Keyword-scoped selection budgets the posts X will actually deliver.
    assert expected_reads(target, keyword_scoped=True) < expected_reads(target)
    selected = select_accounts(reply_lane_targets(pool["targets"]), {}, 160, "2026-09-25", keyword_scoped=True)
    for item in selected:
        item["tier"] = "A"
    report = build_report(pool, selected, 160, 160.0, 100, "2026-09-25", True, keyword_scoped=True)
    projected = report["projectedStrongDrafts"]["atKeywordScopedYieldAssumption"]
    assert projected == round(report["expectedPaidReads"] * KEYWORD_SCOPED_YIELD_ASSUMPTION, 2)


def _approval_daemon(monkeypatch, *, window: int, max_age: int = 300):
    config = approval_cfg()
    config.pacing.max_tweet_age_seconds = max_age
    config.human_approval = HumanApproval(
        enabled=True,
        min_candidate_gap_seconds=0,
        per_account_cooldown_seconds=0,
        require_operator_active=False,
        candidate_ttl_seconds=1800,
        approval_window_seconds=window,
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
            return {
                "chat_id": "12345",
                "message_id": len(sent),
                "intent_url": build_x_reply_intent(kwargs["tweet_id"], kwargs["reply_text"]),
            }

    daemon.approval = FakeApproval()
    monkeypatch.setattr(pacing_module, "selectivity_pass", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "bot.daemon.generate.decide",
        lambda *_a, **_k: {"action": "reply", "text": "one source-grounded thought"},
    )
    return daemon, sent


def _event(post_id: str, age: float) -> dict:
    return {
        "id_str": post_id,
        "text": "this source tweet is long enough for the deterministic gate",
        "author": {"screen_name": "alice"},
        "created_at_epoch": time.time() - age,
    }


def test_batch_window_keeps_drafts_open_for_thirty_minutes(monkeypatch):
    daemon, sent = _approval_daemon(monkeypatch, window=1800)
    run(daemon.handle_tweet(_event("fresh-enough", age=200)))
    assert len(sent) == 1
    assert 1500 < sent[0]["expires_in_seconds"] <= 1600


def test_detection_freshness_still_rejects_old_posts(monkeypatch):
    daemon, sent = _approval_daemon(monkeypatch, window=1800)
    run(daemon.handle_tweet(_event("too-old", age=400)))
    assert sent == []


def test_zero_window_keeps_the_reference_single_deadline(monkeypatch):
    daemon, sent = _approval_daemon(monkeypatch, window=0)
    run(daemon.handle_tweet(_event("reference", age=200)))
    assert len(sent) == 1
    assert sent[0]["expires_in_seconds"] <= 100


def test_shipped_config_targets_thirty_five_drafts_from_day_one():
    raw = json.load(open("config.json"))
    assert raw["pacing"]["daily_cap_ramp"][0] >= 35
    assert raw["human_approval"]["approval_window_seconds"] == 900
    assert raw["human_approval"]["candidate_ttl_seconds"] == 900
    # Speed: drafts only for posts caught within 90 seconds of posting.
    assert raw["pacing"]["max_tweet_age_seconds"] == 90
    assert raw["human_approval"]["min_candidate_gap_seconds"] <= 1
    assert raw["x_detection"]["original_rule_keywords"] == KEYWORDS and len(KEYWORDS) >= 90
    # Broad general-business realm, not SEO only.
    for word in ("business", "sales", "founder", "marketing", "ai", "seo"):
        assert word in KEYWORDS
    # Oct 19 2026: lifestyle tokens bought 8-24% of billed posts and almost no
    # drafts; they are pruned so the daily read cap buys on-topic posts.
    for word in ("money", "work", "success", "career", "jobs", "habits", "wealth"):
        assert word not in KEYWORDS


def test_configured_reach_gate_admits_measured_niche_accounts(tmp_path):
    cands = load_candidates(candidates_file(tmp_path, [
        {"handle": "niche", "category": "SaaS and founders"},
        {"handle": "tiny", "category": "SaaS and founders"},
    ]))
    niche = row("niche", [18_000, 22_000, 30_000, 25_000], days=[20, 21, 22, 23])
    tiny = row("tiny", [2_000, 3_000, 4_000], days=[21, 22, 23])
    for r in (niche, tiny):
        for post in r["posts"]:
            post["text"] = "saas pricing lesson"
    audit = {**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00", "rows": [niche, tiny]}
    pool = build_pool(cands, [audit], set(), keywords=KEYWORDS, min_median_views=10_000)
    assert [t["handle"] for t in pool["targets"]] == ["niche"]
    assert "< 10000" in pool["rejected"][0]["reason"]
    assert reply_lane_targets(pool["targets"]) == []            # default 100K gate
    assert reply_lane_targets(pool["targets"], 10_000) == pool["targets"]
    assert pool["targets"][0]["scoreComponents"]["medianViews"] > 0
    raw = json.load(open("config.json"))
    # Owner rule (Oct 19 2026): the floor never goes below 40K; the pool picks
    # the highest floor at or above it that still meets the daily volume target.
    assert raw["x_detection"]["target_min_median_views"] == 40_000
    assert min(raw["reply_formula"]["reach_floors"]) == 40_000
