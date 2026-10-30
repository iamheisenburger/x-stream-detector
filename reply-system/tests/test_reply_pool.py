import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.config import (
    GenerationBudget,
    MonthlyCeilingExceeded,
    PublishingBudget,
    XDetectionBudget,
    check_monthly_ceiling,
)
from scripts.audit_x_target_reach_official import (
    _merge_bodies,
    _rows,
    audit_window,
    bootstrap_budget_cfg,
    build_handle_query,
)
from scripts.build_reply_pool import (
    audited_handles,
    build_pool,
    load_candidates,
    load_exclusions,
    posts_per_day,
    score_components,
)
from scripts.select_x_reply_targets import (
    REPLY_LANE_CATEGORIES,
    reply_lane_targets,
    select_accounts,
)


WINDOW = {
    "mode": "per_handle",
    "max_results": 10,
    "window_start": "2026-09-18T01:00:00Z",
    "window_end": "2026-09-24T00:00:00Z",
}


def post(day: int, views: int, replies: int = 20, text: str = "seo content that ranks") -> dict:
    return {
        "id": f"p{day}-{views}",
        "created_at": f"2026-09-{day:02d}T12:00:00Z",
        "views": views,
        "replies": replies,
        "text": text,
    }


def row(handle: str, views: list[int], *, relevant: float = 1.0, forbidden: int = 0,
        replies: int = 20, days: list[int] | None = None) -> dict:
    days = days or [23 - i for i in range(len(views))]
    posts = [post(d, v, replies) for d, v in zip(days, views)]
    views_sorted = sorted(views)
    mid = len(views_sorted) // 2
    median = (
        views_sorted[mid] if len(views_sorted) % 2
        else (views_sorted[mid - 1] + views_sorted[mid]) / 2
    ) if views_sorted else 0
    return {
        "handle": handle,
        "sample_posts": len(posts),
        "median_views": median,
        "median_replies": replies,
        "forbidden_posts": forbidden,
        "relevant_original_ratio": relevant,
        "posts": posts,
    }


def candidates_file(tmp_path, rows):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"candidates": rows}))
    return path


def test_seo_lane_is_a_reply_category():
    assert "SEO and content" in REPLY_LANE_CATEGORIES


def test_candidates_reject_bad_handles_categories_and_duplicates(tmp_path):
    ok = candidates_file(tmp_path, [{"handle": "@good_one", "category": "SEO and content"}])
    assert load_candidates(ok)["good_one"]["handle"] == "good_one"

    for bad in (
        [{"handle": "has space", "category": "SEO and content"}],
        [{"handle": "fine", "category": "Crypto"}],
        [
            {"handle": "Dup", "category": "SEO and content"},
            {"handle": "dup", "category": "General business"},
        ],
    ):
        with pytest.raises(ValueError):
            load_candidates(candidates_file(tmp_path, bad))


def test_exclusions_read_another_services_config_and_plain_lists(tmp_path):
    other = tmp_path / "other-config.json"
    other.write_text(json.dumps({"targets": [{"handle": "Shared", "tier": "A"}]}))
    plain = tmp_path / "list.txt"
    plain.write_text("# comment\n@Another\n")
    assert load_exclusions([other, plain]) == {"shared", "another"}


def test_pool_admits_only_measured_on_lane_high_reach_accounts(tmp_path):
    candidates = load_candidates(candidates_file(tmp_path, [
        {"handle": "strong", "category": "SEO and content", "why": "seo", "source": "s"},
        {"handle": "small", "category": "SaaS and founders"},
        {"handle": "thin", "category": "SaaS and founders"},
        {"handle": "offlane", "category": "General business"},
        {"handle": "politics", "category": "General business"},
        {"handle": "shared", "category": "Sales and marketing"},
        {"handle": "neverseen", "category": "AI and automation"},
    ]))
    audit = {**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00", "rows": [
        row("strong", [150_000, 220_000, 400_000, 180_000]),
        row("small", [40_000, 60_000, 90_000, 70_000]),
        row("thin", [900_000, 800_000]),
        row("offlane", [300_000, 300_000, 300_000], relevant=0.1),
        row("politics", [300_000, 300_000, 300_000], forbidden=2),
        row("shared", [300_000, 300_000, 300_000]),
    ]}
    pool = build_pool(candidates, [audit], {"shared"})

    assert [t["handle"] for t in pool["targets"]] == ["strong"]
    reasons = {r["handle"]: r["reason"] for r in pool["rejected"]}
    assert "< 100000" in reasons["small"]
    assert "only 2 original posts" in reasons["thin"]
    assert "on-topic share" in reasons["offlane"]
    assert "off-lane share" in reasons["politics"]
    assert reasons["shared"].startswith("excluded")
    assert pool["unmeasured"] == ["neverseen"]

    # The selector's own reach gate independently accepts what the pool built.
    assert reply_lane_targets(pool["targets"]) == pool["targets"]
    strong = pool["targets"][0]
    assert strong["category"] == "SEO and content"
    assert set(strong["scoreComponents"]) == {
        "medianViews", "reliableViewFloor", "cadence", "nicheFit", "replyCompetition",
    }
    assert 0 < strong["postsPerDay7"] < 1


def test_newest_audit_of_a_handle_wins(tmp_path):
    candidates = load_candidates(candidates_file(tmp_path, [
        {"handle": "mover", "category": "Product and growth"},
    ]))
    old = {**WINDOW, "generated_at": "2026-09-01T00:00:00+00:00",
           "rows": [row("mover", [500_000, 500_000, 500_000])]}
    new = {**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00",
           "rows": [row("mover", [20_000, 20_000, 20_000])]}
    pool = build_pool(candidates, [new, old], set())
    assert pool["targets"] == []
    assert audited_handles([old, new]) == {"mover"}


def test_posts_per_day_uses_covered_span_when_the_page_was_full():
    full = row("busy", [200_000] * 10, days=[23] * 10)
    # Ten posts all on the last day filled the page, so the rate is not 10/6.
    assert posts_per_day(full, WINDOW) > 5
    sparse = row("calm", [200_000] * 3, days=[20, 21, 22])
    assert posts_per_day(sparse, WINDOW) == pytest.approx(3 / (5 + 23 / 24), rel=1e-3)
    assert posts_per_day(row("none", []), WINDOW) == 0.0


def test_score_components_reward_reach_and_penalise_crowds_and_firehoses():
    calm = score_components(row("a", [1_000_000] * 4, replies=5), 3.0)
    crowded = score_components(row("b", [1_000_000] * 4, replies=800), 3.0)
    firehose = score_components(row("c", [1_000_000] * 4, replies=5), 30.0)
    assert calm["replyCompetition"] > crowded["replyCompetition"]
    assert calm["cadence"] == 1.0
    assert firehose["cadence"] == 0.25
    gate = score_components(row("d", [100_000] * 4), 1.0)
    assert gate["medianViews"] == 0.0
    assert all(0.0 <= v <= 1.0 for v in calm.values())


def test_selector_keeps_seo_lane_represented_within_read_budget(tmp_path):
    rows_ = []
    cands = []
    for i in range(12):
        category = "SEO and content" if i < 4 else "SaaS and founders"
        cands.append({"handle": f"h{i}", "category": category})
        # SaaS accounts have far bigger reach than the SEO ones.
        views = 150_000 if category == "SEO and content" else 3_000_000
        rows_.append(row(f"h{i}", [views] * 4, days=[20, 21, 22, 23]))
    pool = build_pool(
        load_candidates(candidates_file(tmp_path, cands)),
        [{**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00", "rows": rows_}],
        set(),
    )
    selected = select_accounts(reply_lane_targets(pool["targets"]), {}, 160, "2026-09-25")
    categories = [t["category"] for t in selected]
    assert categories.count("SEO and content") == 4
    assert sum(t["expectedReadsPerDay"] for t in selected) <= 160


def test_audit_per_handle_query_and_window():
    assert build_handle_query("someone") == "from:someone -is:reply -is:retweet lang:en"
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    start, end = audit_window(now, 24)
    assert (now - start).total_seconds() < 7 * 86_400
    assert end.isoformat().startswith("2026-09-24T00:00")
    with pytest.raises(ValueError):
        audit_window(now, 24 * 7)


def test_merged_per_handle_bodies_produce_one_row_per_handle():
    bodies = [
        {"data": [{"id": "1", "author_id": "u1", "created_at": "2026-09-23T00:00:00Z",
                   "public_metrics": {"impression_count": 120_000, "reply_count": 4},
                   "text": "seo tip"}],
         "includes": {"users": [{"id": "u1", "username": "One"}]}},
        {"data": [{"id": "2", "author_id": "u2", "created_at": "2026-09-23T00:00:00Z",
                   "public_metrics": {"impression_count": 300_000, "reply_count": 9},
                   "text": "saas pricing"}],
         "includes": {"users": [{"id": "u2", "username": "two"}]}},
    ]
    merged = _merge_bodies(bodies + bodies)
    assert len(merged["data"]) == 2
    rows = {r["handle"]: r for r in _rows(merged, ["one", "Two"])}
    assert rows["one"]["sample_posts"] == 1
    assert rows["Two"]["median_views"] == 300_000
    assert rows["one"]["relevant_original_posts"] == 1


def test_bootstrap_audit_cap_is_refused_once_the_stream_is_live():
    live = SimpleNamespace(x_detection=XDetectionBudget(enabled=True))
    with pytest.raises(SystemExit, match="bootstrap audit refused"):
        bootstrap_budget_cfg(live, 10)

    idle = SimpleNamespace(x_detection=XDetectionBudget(
        enabled=False, monthly_microusd_cap=24_000_000, daily_microusd_cap=800_000,
    ))
    raised = bootstrap_budget_cfg(idle, 10)
    assert raised.daily_microusd_cap == 10_000_000
    assert raised.monthly_microusd_cap == 24_000_000
    assert bootstrap_budget_cfg(idle, 100).daily_microusd_cap == 24_000_000


def test_lane_caps_cannot_exceed_the_owner_ceiling():
    def cfg(ceiling, x=24_000_000, g=20_000_000, p=1_000_000):
        return SimpleNamespace(
            monthly_ceiling_microusd=ceiling,
            x_detection=XDetectionBudget(monthly_microusd_cap=x),
            generation=GenerationBudget(monthly_microusd_cap=g),
            publishing=PublishingBudget(monthly_microusd_cap=p),
        )

    assert check_monthly_ceiling(cfg(50_000_000)) == 45_000_000
    with pytest.raises(MonthlyCeilingExceeded):
        check_monthly_ceiling(cfg(50_000_000, x=40_000_000))
    assert check_monthly_ceiling(cfg(0, x=400_000_000)) == 421_000_000


def test_shipped_config_is_inert_and_inside_thirty_five_dollars():
    raw = json.loads(open("config.json").read())
    assert raw["dry_run"] is True
    assert raw["targets"] == []
    assert raw["x_detection"]["enabled"] is False
    assert raw["x_detection"]["dynamic_discovery_enabled"] is False
    assert raw["publishing"]["enabled"] is False
    # The owner authorized automated original posts on Sep 30 2026; they
    # publish only with the account's own write token (checked at startup).
    assert raw["publishing"]["original_posts_authorized"] is True
    assert raw["publishing"]["expected_username"] == "pentradev"
    assert raw["human_approval"]["enabled"] is False
    assert raw["x_detection"]["rule_namespace"] == "pentra-replies-v1"
    total = (
        raw["x_detection"]["monthly_microusd_cap"]
        + raw["generation"]["monthly_microusd_cap"]
        + raw["publishing"]["monthly_microusd_cap"]
    )
    assert total <= raw["monthly_ceiling_usd"] * 1_000_000 == 35_000_000
    assert raw["human_approval"]["daily_candidate_cap"] == 50
    assert raw["human_approval"]["ramp_with_pacing"] is True
    assert raw["pacing"]["daily_cap_ramp"][-1] == 50


def test_selector_report_builds_for_a_real_selection(tmp_path):
    # Regression: the reference report referenced gate constants that no
    # longer existed, so every scheduled selection crashed after ranking.
    from scripts.select_x_reply_targets import build_report

    cands = [{"handle": f"h{i}", "category": "SEO and content"} for i in range(3)]
    rows_ = [row(f"h{i}", [200_000] * 4, days=[20, 21, 22, 23]) for i in range(3)]
    pool = build_pool(
        load_candidates(candidates_file(tmp_path, cands)),
        [{**WINDOW, "generated_at": "2026-09-25T00:00:00+00:00", "rows": rows_}],
        set(),
    )
    selected = select_accounts(reply_lane_targets(pool["targets"]), {}, 160, "2026-09-25")
    for item in selected:
        item["tier"] = "A"
    report = build_report(pool, selected, 160, 160.0, 100, "2026-09-25", True, daily_goal=50)
    assert report["hardReachGate"]["minimumMedianViews"] == 100_000
    assert report["activeTargetCount"] == 3
    assert report["projectedStrongDrafts"]["requiredAcceptanceRate"] == 0.3125
    json.dumps(report)


def test_paid_audit_waits_for_the_stream_window_to_close():
    """With /always on, the reply stream only runs 12:00-23:00 UTC; a paid
    audit is refused inside that window and allowed outside it (Oct 20 2026:
    the audit treated /always as active around the clock and could never run)."""
    import os
    import tempfile
    from datetime import datetime, timezone
    from bot.config import HumanApproval
    from bot.state import State
    from scripts.audit_x_target_reach_official import operator_blocks_audit

    class Cfg:
        human_approval = HumanApproval(shift_hours_utc=[12, 23])

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    state = State(path)
    state.set_operator_continuous(True)
    at = lambda hour: datetime(2026, 10, 20, hour, 30, tzinfo=timezone.utc).timestamp()
    assert operator_blocks_audit(Cfg, state, now=at(15))
    assert not operator_blocks_audit(Cfg, state, now=at(23))
    state.set_operator_active_until(at(23) + 3600, now=at(23))
    assert operator_blocks_audit(Cfg, state, now=at(23)), "a timed /on shift still blocks"


def test_live_x_read_cap_carries_the_one_off_audit_inside_the_ceiling():
    """Arshad approved $2.50 on top of the X reads for the Oct 20 audit; the
    ceiling stays $35 by taking it from the unused Claude allowance."""
    import json
    from pathlib import Path
    from bot.config import load
    live = json.loads(Path(__file__).resolve().parent.parent.joinpath("config.json").read_text())
    assert live["x_detection"]["monthly_microusd_cap"] == 16_000_000
    load()


def test_merge_adds_newly_audited_accounts_and_keeps_the_pool(tmp_path):
    """Oct 20 2026: a full rebuild from the audit files on the server cut the
    pool from 155 targets to 5, because most measurements live only in the
    pool. A merge judges just the new audit's handles and keeps the rest."""
    from bot.reply_formula import ReplyFormula
    from scripts.build_reply_pool import merge_into_pool

    candidates = load_candidates(candidates_file(tmp_path, [
        {"handle": "kept", "category": "SaaS and founders"},
        {"handle": "newstrong", "category": "General business"},
        {"handle": "newsmall", "category": "General business"},
        {"handle": "shared", "category": "General business"},
    ]))
    base = {
        "reachFloor": 40_000,
        "targets": [{"handle": "kept", "category": "SaaS and founders", "medianViews": 90_000,
                     "opportunityPerReply": 5, "onTopicPostsPerDay": 1.0}],
        "rejected": [{"handle": "old_reject", "reason": "measured in August"}],
        "unmeasured": ["newstrong", "newsmall", "shared"],
        "counts": {"targets": 1},
    }
    audit = {**WINDOW, "generated_at": "2026-10-20T23:02:00+00:00", "rows": [
        row("newstrong", [300_000, 250_000, 400_000, 280_000]),
        row("newsmall", [5_000, 6_000, 7_000, 8_000]),
        row("shared", [300_000, 300_000, 300_000, 300_000]),
    ]}
    merged = merge_into_pool(base, candidates, [audit], {"shared"}, [], ReplyFormula.from_config(None))

    assert sorted(t["handle"] for t in merged["targets"]) == ["kept", "newstrong"]
    reasons = {r["handle"]: r["reason"] for r in merged["rejected"]}
    assert set(reasons) == {"old_reject", "newsmall", "shared"} and reasons["shared"].startswith("excluded")
    assert merged["unmeasured"] == [] and merged["merged_added"] == ["newstrong"]
    assert merged["counts"]["targets"] == 2
