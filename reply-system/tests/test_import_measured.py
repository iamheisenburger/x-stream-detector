import json

from scripts.import_measured_accounts import (
    category_for,
    convert_row,
    formula_filter,
    import_rows,
)
from scripts.build_reply_pool import build_pool, load_candidates
from tests.test_reply_pool import candidates_file

KEYWORDS = json.load(open("config.json"))["x_detection"]["original_rule_keywords"]


def measured(handle, views, text="how we grew our saas with content", ppd=2.0, days=tuple("2026-08-0%d" % i for i in range(1, 6))):
    return {
        "handle": handle,
        "bio": "founder building a saas startup",
        "median_views": sorted(views)[len(views) // 2],
        "original_posts_per_day_7d": ppd,
        "sample_originals": [
            {"created_at": d + "T12:00:00+00:00", "views": v, "replies": 40, "text": text}
            for d, v in zip(days, views)
        ],
    }


def test_formula_keeps_big_active_on_topic_accounts_only():
    audit = {"generated_at": "2026-08-10T00:00:00+00:00", "rows": [
        measured("big", [300_000] * 5),
        measured("small", [40_000] * 5),
        measured("quiet", [300_000] * 5, ppd=0.1),
        measured("offtopic", [300_000] * 5, text="lovely sunset at the beach"),
        measured("politics", [300_000] * 5, text="the election and trump and congress on saas"),
        measured("excluded", [900_000] * 5),
    ]}
    rows = import_rows([audit], {"excluded"})
    assert {r["handle"] for r in rows} == {"big", "small", "quiet", "offtopic", "politics"}
    kept = formula_filter(rows, KEYWORDS, min_median=100_000, min_posts_per_day=0.5, min_on_topic=0.3)
    assert [r["handle"] for r in kept] == ["big"]


def test_repeat_measurements_merge_and_feed_the_pool(tmp_path):
    old = {"generated_at": "2026-08-01T00:00:00+00:00", "rows": [measured("acct", [50_000] * 5)]}
    new = {"generated_at": "2026-08-18T00:00:00+00:00", "rows": [measured("acct", [250_000] * 5)]}
    rows = import_rows([new, old], set())
    assert rows[0]["median_views"] == 250_000 and rows[0]["posts_per_day"] == 2.0
    cands = load_candidates(candidates_file(tmp_path, [{"handle": "acct", "category": "SaaS and founders"}]))
    pool = build_pool(cands, [{"generated_at": "2026-09-25T00:00:00+00:00", "mode": "imported", "rows": rows}],
                      set(), keywords=KEYWORDS, min_median_views=100_000)
    target = pool["targets"][0]
    assert target["postsPerDay7"] == 2.0 and target["onTopicPostsPerDay"] == 2.0
    assert target["measuredAt"].startswith("2026-08-18")


def test_rows_without_post_samples_are_skipped_and_categories_are_assigned():
    assert convert_row({"handle": "x", "median_views": 1e6}, "2026-08-10") is None
    assert category_for("SEO consultant and blogger", []) == "SEO and content"
    assert category_for("", ["new model from openai, the llm race"]) == "AI and automation"
    assert category_for("", ["nice weather"]) == "General business"


def test_distinct_posts_from_several_audits_are_pooled():
    a = {"generated_at": "2026-08-10T00:00:00+00:00", "rows": [measured("acct", [100_000] * 3, days=("2026-08-01", "2026-08-02", "2026-08-03"))]}
    b = {"generated_at": "2026-08-18T00:00:00+00:00", "rows": [measured("acct", [300_000] * 3, days=("2026-08-15", "2026-08-16", "2026-08-17"))]}
    row = import_rows([a, b], set())[0]
    assert row["sample_posts"] == 6
    assert row["median_views"] == 200_000
