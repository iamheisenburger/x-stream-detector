"""Posted-reply tracking (Oct 19 2026): which drafts were posted and how they did."""
import os
import sqlite3
import tempfile

from bot.state import State
from scripts.daily_summary import build_summary
from scripts.track_posted_replies import posted_summary, replies_from_search, store_results


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    State(path)  # creates reply_drafts and the ledgers
    return path


def _draft(db, tweet_id, created_at):
    db.execute(
        """INSERT INTO reply_drafts(tweet_id,author,source_text,reply_text,source_created_at,intent_url,
               status,created_at,expires_at,day) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (tweet_id, "paulg", "source", "draft", created_at - 10, "https://x.com/intent", "notified",
         created_at, created_at + 900, "2026-10-19"),
    )


PAGE = {
    "data": [
        {"id": "900", "created_at": "2026-10-19T13:02:00.000Z", "text": "@paulg  the boring fix ships the company.",
         "referenced_tweets": [{"type": "replied_to", "id": "100"}],
         "public_metrics": {"impression_count": 812, "like_count": 4, "reply_count": 1, "retweet_count": 0,
                            "quote_count": 0, "bookmark_count": 1}},
        {"id": "901", "created_at": "2026-10-19T14:00:00.000Z", "text": "an original post, not a reply",
         "public_metrics": {"impression_count": 50}},
        {"id": "902", "created_at": "2026-10-19T15:00:00.000Z", "text": "@levelsio reply to a post we never drafted",
         "referenced_tweets": [{"type": "replied_to", "id": "555"}], "public_metrics": {"impression_count": 30}},
    ]
}


def test_search_page_yields_only_replies_with_their_metrics():
    rows = replies_from_search(PAGE)
    assert [r["reply_id"] for r in rows] == ["900", "902"]
    assert rows[0]["source_tweet_id"] == "100" and rows[0]["impressions"] == 812 and rows[0]["likes"] == 4
    assert rows[0]["text"] == "@paulg the boring fix ships the company."


def test_results_link_to_drafts_and_metrics_update_in_place():
    path = _db()
    db = sqlite3.connect(path)
    _draft(db, "100", 1_792_400_000)
    db.commit()
    assert store_results(db, replies_from_search(PAGE), now=1_792_410_000) == 1
    later = {"data": [dict(PAGE["data"][0], public_metrics={"impression_count": 1500, "like_count": 9})]}
    store_results(db, replies_from_search(later), now=1_792_490_000)
    rows = db.execute("SELECT reply_id, draft_id, impressions, likes, measured_at FROM reply_results ORDER BY reply_id").fetchall()
    assert rows[0][0] == "900" and rows[0][1] is not None and rows[0][2:] == (1500, 9, 1_792_490_000)
    assert rows[1][0] == "902" and rows[1][1] is None


def test_daily_summary_reports_posted_drafts_and_settled_engagement():
    path = _db()
    db = sqlite3.connect(path)
    now = 1_792_500_000
    _draft(db, "100", now - 3_600)
    _draft(db, "101", now - 7_200)
    db.commit()
    store_results(db, replies_from_search(PAGE), now=now)
    # A reply posted two days ago has settled numbers.
    db.execute("UPDATE reply_results SET posted_at=? WHERE reply_id='902'", (now - 2 * 86_400,))
    db.commit()
    line = posted_summary(path, now - 86_400, now)
    assert line.startswith("Posted: 1 of 2 drafts from the last 24h")
    assert "median 30 impressions" in line
    assert "Posted: 1 of 2 drafts" in build_summary(path, now, 35)


def test_summary_without_tracking_data_is_unchanged():
    path = _db()
    assert posted_summary(path, 0, 1) is None
    assert "Posted:" not in build_summary(path, 1_792_500_000, 35)


def test_price_pins_are_flagged_a_week_ahead_and_after_expiry():
    from datetime import date
    from scripts.daily_summary import price_pin_warning
    pins = {"x_detection": "2026-12-31", "generation": "2026-12-31", "publishing": "2027-01-15"}
    assert price_pin_warning(pins, date(2026, 12, 23)) is None
    soon = price_pin_warning(pins, date(2026, 12, 26))
    assert soon.startswith("Price pins expire 2026-12-31 (in 5 days; generation, x_detection)")
    assert "publishing" not in soon
    expired = price_pin_warning(pins, date(2027, 1, 2))
    assert expired.startswith("PRICE PINS EXPIRED 2026-12-31 (generation, x_detection)")
    assert price_pin_warning(pins, date(2026, 12, 30)).startswith("Price pins expire 2026-12-31 (in 1 day;")


def test_follower_count_is_stored_daily_and_reported_with_its_change():
    """Oct 20 2026: we measured impressions and guessed at follows; the morning
    summary now says how many followers the account has and the change."""
    from scripts.daily_summary import record_followers
    path = _db()
    day = 1_792_540_800  # 2026-10-20 00:00 UTC
    assert record_followers(path, 32, day + 3300) == "Followers: 32"
    assert record_followers(path, 35, day + 86_400 + 3300) == "Followers: 35 (+3 since the last summary)"
    assert record_followers(path, 35, day + 86_400 + 3400) == "Followers: 35 (+3 since the last summary)", \
        "a rerun the same day compares with the day before, not with itself"


def test_follower_lookup_books_a_measurement_read_and_survives_errors():
    import asyncio
    from scripts.daily_summary import follower_line

    class Cfg:
        class publishing:
            expected_username = "pentradev"

    path = _db()

    async def ok_fetch(cfg, username):
        assert username == "pentradev"
        return 40

    async def broken_fetch(cfg, username):
        raise RuntimeError("HTTP 503")

    state = State(path)
    assert asyncio.run(follower_line(Cfg, state, path, now=1_792_540_800, fetch=ok_fetch)) == "Followers: 40"
    booked = state.db.execute("SELECT author, cost_microusd FROM x_stream_reads").fetchall()
    assert booked == [("measure:pentradev", 10_000)]
    line = asyncio.run(follower_line(Cfg, state, path, now=1_792_627_200, fetch=broken_fetch))
    assert line.startswith("Followers: unavailable")


def test_original_posts_are_measured_and_summarized_for_the_vision_checklist():
    """Vision checklist B4 (Oct 22 2026): our own posts had no metrics anywhere,
    so nobody could tell whether a post reached anyone or was saved."""
    from scripts.track_posted_replies import originals_from_search, posts_summary, store_post_results

    rows = originals_from_search(PAGE)
    assert [r["post_id"] for r in rows] == ["901"], "replies are not original posts"
    page = {"data": [
        {"id": "a1", "created_at": "2026-10-21T13:00:00.000Z", "text": "page-two check",
         "public_metrics": {"impression_count": 240, "like_count": 3, "reply_count": 1, "bookmark_count": 2}},
        {"id": "a2", "created_at": "2026-10-21T19:30:00.000Z", "text": "schema myth",
         "public_metrics": {"impression_count": 80, "like_count": 1, "reply_count": 0, "bookmark_count": 0}},
    ]}
    path = _db()
    db = sqlite3.connect(path)
    store_post_results(db, originals_from_search(page), now=1792800000.0)
    db.close()
    line = posts_summary(path, now=1792800000.0)
    assert line == ("Posts (7 days): 2 measured, median 160 views, 4 likes, 2 bookmarks, 1 replies; "
                    "saved or answered: 1 of 2")


def test_summary_shows_the_like_rate_and_post_slots_for_the_vision_checklist():
    """Vision checklist D1 (Oct 22 2026): the share of replies that earn a like
    (A6) and the posts published per day (B1) were missing from the summary."""
    path = _db()
    db = sqlite3.connect(path)
    now = 1_792_500_000
    _draft(db, "100", now - 3_600)
    store_results(db, replies_from_search(PAGE), now=now)
    db.execute("UPDATE reply_results SET posted_at=? WHERE reply_id IN ('900','902')", (now - 2 * 86_400,))
    db.execute("INSERT INTO actions(kind, text, at, day) VALUES('post', 'a how-to', ?, '2026-10-21')", (now - 600,))
    db.commit()
    assert "1 of 2 liked, 50%" in posted_summary(path, now - 86_400, now)
    assert "Original posts published (24h): 1 of 2 slots" in build_summary(path, now, 35)


def test_summary_shows_read_budget_use_for_the_vision_checklist(tmp_path):
    """Vision checklist A5/D1 (Oct 22 2026): read-budget use was the last
    checklist number missing from the morning summary."""
    import json as _json
    report = tmp_path / "active.json"
    report.write_text(_json.dumps({"activeTargetCount": 70, "dailyPaidReadTarget": 90.0, "expectedPaidReads": 49.49,
                                   "categoryReads": {"AI and automation": 13.66}}))
    line = build_summary(_db(), 1_792_500_000, 35, targets_report=str(report))
    assert "Targets: 70 accounts, 49 of 90 daily reads (55%), AI 15%" in line


def test_the_summary_shows_account_standing_and_reply_speed():
    """Vision checklist D6 (Oct 23 2026): X limited the account on Oct 22. Reply
    reach is the recovery signal, and the speed shows how soon after the
    original post each reply went up."""
    from scripts.daily_summary import standing_line
    from scripts.track_posted_replies import SCHEMA

    path = _db()
    assert standing_line(path, 1_792_500_000) is None
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    now = 1_792_500_000
    for i, (views, speed) in enumerate([(5, 28), (8, 31), (6, 24)]):
        tweet = f"30{i}"
        _draft(db, tweet, now - 3_600)
        db.execute("UPDATE reply_drafts SET source_created_at=? WHERE tweet_id=?", (now - 3_600 - speed, tweet))
        draft_id = db.execute("SELECT id FROM reply_drafts WHERE tweet_id=?", (tweet,)).fetchone()[0]
        db.execute("""INSERT INTO reply_results(reply_id,source_tweet_id,draft_id,posted_at,impressions,likes,text,
                      measured_at) VALUES(?,?,?,?,?,?,?,?)""", (f"r{i}", tweet, draft_id, now - 3_600, views, 0, "t", now))
    db.commit()
    line = standing_line(path, now)
    assert line.startswith("Standing (D6): replies median 6 views (3 in 24h"), line
    assert "posted a median 28 s after the original" in line
    assert line in build_summary(path, now, 35)


def test_the_standing_line_reads_post_views_when_no_replies_were_posted():
    """Oct 23 2026: Arshad paused replies while the account is limited, so the
    newest posts' views are the recovery signal."""
    from scripts.daily_summary import standing_line
    from scripts.track_posted_replies import POSTS_SCHEMA

    path = _db()
    db = sqlite3.connect(path)
    db.execute(POSTS_SCHEMA)
    now = 1_792_500_000
    for i, (age, views) in enumerate([(30 * 3_600, 7), (20 * 3_600, 3), (9 * 3_600, 12)]):
        db.execute("INSERT INTO post_results(post_id,posted_at,impressions,likes,text,measured_at) VALUES(?,?,?,?,?,?)",
                   (f"p{i}", now - age, views, 0, "t", now))
    db.commit()
    line = standing_line(path, now)
    assert line.startswith("Standing (D6): no replies posted in 24h; newest posts "), line
    assert line.endswith("newest posts Oct 19 16:40 3, Oct 20 03:40 12 views (limited since Oct 22, recovered at 20+)"), line
