import json

import pytest

from bot.reply_formula import (
    ReplyFormula,
    choose_reach_floor,
    gate_failure,
    opportunity,
)
from scripts.build_reply_pool import build_formula_pool, load_candidates
from scripts.select_x_reply_targets import LiveYield, adjusted_score
from tests.test_reply_pool import WINDOW, candidates_file, row

KEYWORDS = json.load(open("config.json"))["x_detection"]["original_rule_keywords"]
F = ReplyFormula(daily_draft_target=4, assumed_draft_rate=0.5,
                 reach_floors=(100_000, 50_000, 10_000))


def test_opportunity_is_impressions_per_post_and_ignores_final_reply_counts():
    # We reply within minutes of posting, before the crowd, so a post's final
    # reply count never lowers its value.
    assert opportunity(400_000, 700, F) == 400_000
    assert opportunity(400_000, 700, F) > opportunity(150_000, 20, F)


def test_floor_is_the_highest_that_still_meets_volume():
    # need 4 / 0.5 = 8 on-topic posts a day
    accounts = [(300_000, 3.0), (120_000, 2.0), (60_000, 4.0), (20_000, 5.0)]
    assert choose_reach_floor(accounts, F) == {
        "floor": 50_000.0, "supply": 9.0, "required": 8.0, "meetsVolume": True}
    short = choose_reach_floor([(300_000, 1.0)], F)
    assert short["floor"] == 10_000 and short["meetsVolume"] is False


def test_gates_reject_inactive_off_topic_and_off_lane_accounts():
    ok = dict(sample_posts=5, posts_per_day=1.0, on_topic_share=0.5, off_lane_share=0.0, formula=F)
    assert gate_failure(**ok) == ""
    assert gate_failure(**{**ok, "sample_posts": 2}).startswith("only 2")
    assert gate_failure(**{**ok, "posts_per_day": 0.2}).startswith("inactive")
    assert gate_failure(**{**ok, "on_topic_share": 0.1}).startswith("off-topic")
    assert gate_failure(**{**ok, "off_lane_share": 0.4}).startswith("off-lane")


def _acct(handle, views, replies=20, per_day=2, text="how we grew our saas with seo content"):
    r = row(handle, [views] * 5, replies=replies)
    r["posts_per_day"] = per_day
    for p in r["posts"]:
        p["text"] = text
    return r


def test_formula_pool_prefers_big_accounts_and_ranks_by_opportunity(tmp_path):
    rows = [
        _acct("mega", 500_000, replies=300),
        _acct("big", 200_000, replies=10),
        _acct("mid", 60_000, per_day=4),
        _acct("small", 20_000, per_day=6),
        _acct("sleepy", 900_000, per_day=0.1),
        _acct("offtopic", 900_000, text="a sunset photo"),
    ]
    cands = load_candidates(candidates_file(tmp_path, [
        {"handle": r["handle"], "category": "SaaS and founders"} for r in rows]))
    audit = {**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00", "rows": rows}
    pool = build_formula_pool(cands, [audit], set(), KEYWORDS, F)

    assert pool["reachFloor"] == 50_000          # 2 + 2 + 4 = 8 posts/day >= 8 needed
    assert [t["handle"] for t in pool["targets"]] == ["mega", "big", "mid"]
    reasons = {r["handle"]: r["reason"] for r in pool["rejected"]}
    assert reasons["small"].startswith("below today's reach floor")
    assert reasons["sleepy"].startswith("inactive")
    assert reasons["offtopic"].startswith("off-topic")
    assert pool["formula"]["meetsVolume"] is True
    assert pool["targets"][0]["opportunityPerReply"] == 500_000


def test_selector_follows_opportunity_when_the_formula_scored_the_pool():
    strong = {"handle": "a", "scoreComponents": {"opportunity": 0.9, "cadence": 0.5, "nicheFit": 0.5}}
    weak = {"handle": "b", "scoreComponents": {"opportunity": 0.2, "cadence": 1.0, "nicheFit": 1.0}}
    day = "2026-09-25"
    assert adjusted_score(strong, LiveYield(), day) > adjusted_score(weak, LiveYield(), day)


def test_shipped_formula_is_affordable_inside_the_read_budget():
    raw = json.load(open("config.json"))
    formula = ReplyFormula.from_config(raw["reply_formula"])
    assert formula.daily_draft_target >= 35
    reads_per_day = raw["x_detection"]["daily_microusd_cap"] // raw["x_detection"]["post_read_microusd"]
    assert formula.required_posts_per_day <= reads_per_day
    assert max(formula.reach_floors) == 100_000
    assert formula.lowest_floor == raw["x_detection"]["target_min_median_views"]


def test_the_reach_floor_never_goes_below_40k():
    """Owner rule (Oct 19 2026): a thin supply never lowers the pool's 40K floor."""
    from bot.reply_formula import MIN_REACH_FLOOR, ReplyFormula, choose_reach_floor
    assert MIN_REACH_FLOOR == 40_000
    formula = ReplyFormula.from_config({"reach_floors": [100_000, 50_000, 40_000, 20_000, 10_000]})
    assert formula.lowest_floor == 40_000
    assert min(formula.reach_floors) == 40_000
    thin = [(15_000, 50.0), (25_000, 50.0), (45_000, 1.0)]
    assert choose_reach_floor(thin, formula)["floor"] == 40_000
    assert ReplyFormula().lowest_floor == 40_000
