import sqlite3

from scripts.select_x_reply_targets import (
    MIN_REACH_SAMPLE_POSTS,
    PRIMARY_MIN_MEDIAN_VIEWS,
    LiveYield,
    activate_all_accounts,
    adjusted_score,
    assign_tiers,
    deterministic_jitter,
    expected_reads,
    load_live_yield,
    reply_lane_targets,
    select_accounts,
)


CATEGORIES = [
    "General business",
    "SaaS and founders",
    "Sales and marketing",
    "AI and automation",
    "Product and growth",
]


def target(index: int) -> dict:
    category = CATEGORIES[index % len(CATEGORIES)]
    quality = 1 - index / 500
    return {
        "handle": f"target{index}",
        "category": category,
        "activeWindowPostsPerDay": 0.5,
        "medianViews": PRIMARY_MIN_MEDIAN_VIEWS + 50_000,
        "samplePosts": MIN_REACH_SAMPLE_POSTS + 3,
        "score": quality * 100,
        "scoreComponents": {
            "medianViews": quality,
            "reliableViewFloor": quality,
            "cadence": 0.7,
            "nicheFit": 0.8,
            "replyCompetition": 0.6,
            "liveDraftYield": 0.4,
        },
    }


def test_selector_uses_master_pool_without_exceeding_read_budget():
    selected = select_accounts(
        [target(index) for index in range(200)],
        {},
        daily_read_target=85,
        selection_date="2026-07-17",
    )
    assert 20 < len(selected) < 200
    assert sum(row["expectedReadsPerDay"] for row in selected) <= 85
    assert len({row["handle"] for row in selected}) == len(selected)
    for category in CATEGORIES:
        assert sum(row["category"] == category for row in selected) >= 4


def test_tiers_respect_generation_call_target():
    selected = select_accounts(
        [target(index) for index in range(200)],
        {},
        daily_read_target=85,
        selection_date="2026-07-17",
    )
    assign_tiers(selected, daily_generation_target=26)
    weights = {"A": 0.90, "B": 0.75, "C": 0.55, "D": 0.38, "E": 0.22}
    expected_calls = sum(
        row["expectedReadsPerDay"] * weights[row["tier"]] for row in selected
    )
    assert expected_calls <= 26
    assert any(row["tier"] != "E" for row in selected)


def test_live_yield_is_shrunk_and_daily_rotation_is_deterministic():
    row = target(0)
    prior = adjusted_score(row, LiveYield(), "2026-07-17")
    lucky = adjusted_score(row, LiveYield(reads=1, drafts=1), "2026-07-17")
    assert 0 < lucky - prior < 6
    assert deterministic_jitter("target0", "2026-07-17") == deterministic_jitter(
        "target0", "2026-07-17"
    )
    assert deterministic_jitter("target0", "2026-07-17") != deterministic_jitter(
        "target0", "2026-07-18"
    )


def test_shift_pool_keeps_all_200_targets_eligible_and_ranked():
    selected = activate_all_accounts(
        [target(index) for index in range(200)],
        {},
        selection_date="2026-07-17",
    )
    assert len(selected) == 200
    assert len({row["handle"] for row in selected}) == 200
    assert {row["tier"] for row in selected} == {"A"}
    assert [row["adjustedScore"] for row in selected] == sorted(
        (row["adjustedScore"] for row in selected),
        reverse=True,
    )


def test_reply_lane_keeps_general_business_without_admitting_unknown_categories():
    pool = [target(index) for index in range(200)]
    pool.append({**target(201), "handle": "celebrity", "category": "Entertainment"})
    lane = reply_lane_targets(pool)
    assert lane
    assert any(row["category"] == "General business" for row in lane)
    assert all(row["category"] != "Entertainment" for row in lane)
    assert sum(expected_reads(row) for row in lane) >= 75


def test_reply_lane_rejects_sub_100k_median_even_when_other_signals_are_strong():
    weak = {
        **target(0),
        "handle": "LunarResearcher",
        "medianViews": PRIMARY_MIN_MEDIAN_VIEWS - 1,
        "samplePosts": 12,
        "activeWindowPostsPerDay": 2.0,
        "score": 99,
    }
    lane = reply_lane_targets([weak, target(1)])
    assert [row["handle"] for row in lane] == ["target1"]


def test_reply_lane_accepts_exact_contract_floor():
    qualified = {
        **target(0),
        "handle": "qualified",
        "medianViews": PRIMARY_MIN_MEDIAN_VIEWS,
        "samplePosts": MIN_REACH_SAMPLE_POSTS,
    }
    assert [row["handle"] for row in reply_lane_targets([qualified])] == [
        "qualified"
    ]


def test_reply_lane_rejects_missing_or_too_small_reach_sample():
    missing = {
        **target(0),
        "handle": "missing",
        "samplePosts": 0,
    }
    too_small = {
        **target(1),
        "handle": "too_small",
        "samplePosts": MIN_REACH_SAMPLE_POSTS - 1,
    }
    assert reply_lane_targets([missing, too_small]) == []


def test_missing_live_history_tables_are_treated_as_empty(tmp_path):
    state_db = tmp_path / "legacy-state.db"
    connection = sqlite3.connect(state_db)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.close()
    assert load_live_yield(state_db) == {}


def test_brand_accounts_are_never_reply_targets():
    """Oct 20 2026: replies under company and product accounts (OpenAI, nvidia,
    github, a16z...) earned the fewest impressions, and a brand never likes or
    answers a reply, one of the reply-ranking inputs X documents. Only people."""
    from scripts.select_x_reply_targets import BRAND_ACCOUNTS
    brand = dict(target(1), handle="OpenAI")
    person = dict(target(2), handle="paulg")
    kept = reply_lane_targets([brand, person], min_median_views=PRIMARY_MIN_MEDIAN_VIEWS)
    assert [row["handle"] for row in kept] == ["paulg"]
    for handle in ("nvidia", "github", "a16z", "ycombinator", "cursor_ai", "anthropicai"):
        assert handle in BRAND_ACCOUNTS
    assert not {"paulg", "nikitabier", "alexhormozi", "thedankoe", "naval"} & BRAND_ACCOUNTS


def test_ai_industry_people_are_not_reply_targets():
    """Oct 20 2026: replies under the AI and automation category earned a
    median of 34 impressions (14 settled replies) against 132 under business
    accounts and 1,821 under @nikitabier, and those followers are researchers
    and developers, not Pentra's buyers. Arshad: replace them with founders,
    marketers and creators."""
    from scripts.select_x_reply_targets import AI_INDUSTRY_ACCOUNTS
    lab = dict(target(3), handle="sama")
    researcher = dict(target(8), handle="Karpathy")
    builder = dict(target(13), handle="gregisenberg")
    kept = reply_lane_targets([lab, researcher, builder], min_median_views=PRIMARY_MIN_MEDIAN_VIEWS)
    assert [row["handle"] for row in kept] == ["gregisenberg"]
    assert not {"gregisenberg", "levelsio", "alexhormozi", "naval", "mcuban", "dhh", "nikitabier",
                "nicksaraev", "liamottley_"} & AI_INDUSTRY_ACCOUNTS


def test_ai_and_automation_gets_at_most_a_quarter_of_the_reads():
    """Even when AI accounts score highest, founders, business, marketing, SEO
    and product people keep three quarters of the day's reads."""
    def row(handle, category, quality):
        return dict(target(0), handle=handle, category=category, scoreComponents={
            "medianViews": quality, "reliableViewFloor": quality, "cadence": 0.7,
            "nicheFit": 0.8, "replyCompetition": 0.6, "liveDraftYield": 0.4,
        })

    ai = [row(f"ai{i}", "AI and automation", 1.0) for i in range(100)]
    founders = [row(f"founder{i}", "SaaS and founders", 0.5) for i in range(100)]
    selected = select_accounts(ai + founders, {}, daily_read_target=40, selection_date="2026-10-20")

    def reads(category):
        return sum(r["expectedReadsPerDay"] for r in selected if r["category"] == category)

    assert reads("AI and automation") <= 0.25 * 40
    assert reads("SaaS and founders") >= 0.70 * 40
    assert sum(r["expectedReadsPerDay"] for r in selected) <= 40


def test_selector_runs_as_a_file_like_the_daily_timer(tmp_path):
    """Oct 19-20 2026: the timer runs the script by path, and `from bot...`
    failed with ModuleNotFoundError, so no daily selection ran."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "select_x_reply_targets.py"
    result = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr


def test_selector_refuses_to_collapse_the_live_targets(tmp_path, monkeypatch):
    """Oct 20 2026: a bad pool rebuild left 5 accounts; a working selector would
    have cut the stream from 67 targets to 5. The config stays as it was."""
    import json
    import sys

    import pytest

    from scripts import select_x_reply_targets as sel

    assert sel.collapse_refusal(118, 67) is None, "the Oct 20 AI cut stays allowed"
    assert sel.collapse_refusal(8, 1) is None, "small lists are left alone"
    pool = {"reachFloor": 40_000, "targets": [dict(target(i), handle=f"p{i}") for i in range(2)]}
    config = {"x_detection": {"daily_microusd_cap": 450_000, "post_read_microusd": 5_000},
              "targets": [{"handle": f"t{i}", "tier": "A"} for i in range(20)]}
    (tmp_path / "pool.json").write_text(json.dumps(pool))
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["select", "--pool", str(tmp_path / "pool.json"),
                                      "--config", str(tmp_path / "config.json"),
                                      "--state-db", str(tmp_path / "none.db"),
                                      "--report", str(tmp_path / "report.json")])
    with pytest.raises(SystemExit, match="from 20 to 2"):
        sel.main()
    assert json.loads((tmp_path / "config.json").read_text()) == config
