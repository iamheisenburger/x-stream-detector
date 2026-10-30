"""R3 report: each posted reply with its timing and its source post's early numbers."""
import os
import sqlite3
import tempfile

from bot.state import State
from scripts.report_reply_visibility import BASELINE_MEDIAN_IMPRESSIONS, visibility_report
from scripts.track_posted_replies import SCHEMA


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    State(path)
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    return path, db


def _posted(db, n, *, author, impressions, likes, source_at, posted_after, views_90s, replies_90s):
    db.execute("""INSERT INTO reply_drafts(tweet_id,author,source_text,reply_text,source_created_at,intent_url,
                  status,created_at,expires_at,day) VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (f"s{n}", author, "src", "draft", source_at, "u", "notified", source_at + 10, source_at + 900,
                "2026-10-20"))
    draft_id = db.execute("SELECT id FROM reply_drafts WHERE tweet_id=?", (f"s{n}",)).fetchone()[0]
    db.execute("""INSERT INTO reply_results(reply_id,source_tweet_id,draft_id,posted_at,impressions,likes,replies,
                  reposts,quotes,bookmarks,text,measured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
               (f"r{n}", f"s{n}", draft_id, source_at + posted_after, impressions, likes, 0, 0, 0, 0, "t",
                source_at + 90_000))
    db.execute("""INSERT INTO source_snapshots(post_id,kind,age_seconds,impressions,likes,replies,at)
                  VALUES(?,?,?,?,?,?,?)""", (f"s{n}", "rise_90s", 92, views_90s, 3, replies_90s, source_at + 92))


def test_report_rows_and_medians_against_the_5x_target():
    path, db = _db()
    t = 1_792_500_000
    _posted(db, 1, author="paulg", impressions=900, likes=4, source_at=t, posted_after=75, views_90s=3100, replies_90s=6)
    _posted(db, 2, author="rauchg", impressions=300, likes=1, source_at=t + 3600, posted_after=40, views_90s=800, replies_90s=2)
    _posted(db, 3, author="a16z", impressions=120, likes=0, source_at=t + 7200, posted_after=400, views_90s=400, replies_90s=1)
    db.commit()
    report = visibility_report(path)
    assert [row["author"] for row in report["rows"]] == ["paulg", "rauchg", "a16z"]
    first = report["rows"][0]
    assert first["posted_after_s"] == 75 and first["views_90s"] == 3100 and first["replies_90s"] == 6
    assert report["posted"] == 3 and report["median_impressions"] == 300 and report["likes"] == 5
    assert report["target_median"] == 5 * BASELINE_MEDIAN_IMPRESSIONS
    assert report["median_posted_after_s"] == 75
