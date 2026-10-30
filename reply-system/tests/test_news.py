"""News posts (Oct 24 2026): a post within hours of an official Google Search
change, from Google's own text, at most one a day."""
import json
from pathlib import Path

from bot import news
from tests.test_posts_v3 import _v3_daemon
from tests.test_telegram_approval import run

BLOG = b"""<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel>
<item><title>Announcing web multimodal Search performance reporting in Search Console</title>
<link>https://developers.google.com/search/blog/2026/09/web-multimodal-in-sc</link>
<description><![CDATA[<p>Understanding how users find your content is crucial.</p>]]></description>
<pubDate>Thu, 24 Sep 2026 00:00:00 +0000</pubDate></item>
<item><title>Search Central Live is coming to Bogota</title>
<link>https://developers.google.com/search/blog/2026/09/scl-bogota</link>
<description>event</description><pubDate>Tue, 08 Sep 2026 00:00:00 +0000</pubDate></item>
</channel></rss>"""

STATUS = json.dumps([{
    "id": "XhUD", "external_desc": "September 2026 spam update", "uri": "incidents/XhUD",
    "begin": "2026-09-24T16:15:00+00:00", "end": "2026-10-08T08:00:00+00:00",
    "updates": [
        {"when": "2026-10-08T08:37:09+00:00", "text": "The rollout was complete as of October 8, 2026."},
        {"when": "2026-09-24T16:15:47+00:00", "text": "Released the September 2026 spam update "
         "<https://developers.google.com/search/docs/appearance/spam-updates>, which applies globally."},
    ],
}]).encode()

PAGE = ("<html><nav><p>Menu link that should not appear anywhere</p></nav><div class=\"devsite-article-body\">"
        "<p>Today, we are excited to introduce Search Console reporting for web multimodal search.</p>"
        "<p>Use the new multimodal search type filter in the performance reports (https://example.com/x).</p>"
        "<p>Except as otherwise noted, the content of this page is licensed under the Creative Commons.</p>"
        "</div></html>").encode()


def test_the_official_feeds_parse_and_event_posts_are_skipped():
    blog = news.blog_items(BLOG)
    assert [i["skip"] for i in blog] == [False, True]
    assert blog[0]["id"] == "blog:https://developers.google.com/search/blog/2026/09/web-multimodal-in-sc"
    status = news.status_items(STATUS)
    assert [i["id"] for i in status] == ["status:XhUD:start", "status:XhUD:end"]
    assert status[0]["summary"].startswith("Released the September 2026 spam update")
    assert "https://" not in status[0]["summary"], "links are stripped so the post cannot carry a URL"
    assert status[0]["doc_url"] == "https://developers.google.com/search/docs/appearance/spam-updates"
    assert status[1]["title"] == "September 2026 spam update: rollout complete"


def test_page_text_keeps_the_article_body_only():
    text = news.page_text(PAGE.decode())
    assert text.startswith("Today, we are excited to introduce Search Console reporting")
    assert "Menu link" not in text and "licensed" not in text and "example.com" not in text


def test_news_cards_use_the_article_body_and_the_linked_google_doc():
    blog = news.blog_items(BLOG)[0]
    cards = news.news_cards(blog, fetch=lambda url: PAGE)
    assert "multimodal search type filter" in cards[0]["text"]
    assert cards[0]["source"] == "google search central blog, september 2026"
    status = news.status_items(STATUS)[0]
    cards = news.news_cards(status, fetch=lambda url: PAGE)
    assert [c["source"] for c in cards] == ["google search status dashboard, september 2026",
                                            "google search central documentation"]


def _news_daemon(monkeypatch, feeds):
    from bot import daemon as daemon_module

    daemon, _ = _v3_daemon(monkeypatch, [])
    daemon.cfg.post_prompt_version = "v4"
    daemon.cfg.news_posts = True
    daemon.post_drafts = False
    written, published = [], []

    def fake_value_post(cfg_, client, *, format_id, cards, recent_posts=None, trace=None):
        written.append((format_id, cards[0]["id"]))
        return {"text": "Search Console now reports multimodal searches.", "format": format_id}

    async def fake_post(cfg_, state_, text):
        published.append(text)
        return True

    monkeypatch.setattr(daemon_module.generate, "value_post", fake_value_post)
    monkeypatch.setattr(daemon_module.poster, "post", fake_post)

    def fetch(url):
        return feeds[url] if url in feeds else PAGE

    return daemon, fetch, written, published


def test_the_first_read_posts_nothing_then_a_new_item_is_posted_once_a_day(monkeypatch):
    fresh = (BLOG.replace(b"web-multimodal-in-sc", b"new-thing").replace(b"Announcing web", b"Announcing new")
             .replace(b"Thu, 24 Sep 2026", b"Mon, 21 Sep 2026"))
    published_at = news.blog_items(fresh)[0]["published"]
    feeds = {news.BLOG_FEED: BLOG, news.STATUS_FEED: b"[]"}
    daemon, fetch, written, published = _news_daemon(monkeypatch, feeds)
    run(daemon._news_step(now=published_at - 86_400, fetch=fetch))
    assert written == [] and published == [], "items that existed on the first read are never posted"
    feeds[news.BLOG_FEED] = fresh
    feeds[news.STATUS_FEED] = STATUS.replace(b"XhUD", b"NEW1").replace(b"2026-09-24T16:15", b"2026-09-21T08:00")
    now = published_at + 6 * 3_600
    run(daemon._news_step(now=now, fetch=fetch))
    assert written == [("search_news", "news:blog:https://developers.google.com/search/blog/2026/09/new-thing")]
    assert len(published) == 1
    run(daemon._news_step(now=now + news.CHECK_SECONDS + 1, fetch=fetch))
    assert len(published) == 1, "one news post a day; the other new item waits for tomorrow"
    run(daemon._news_step(now=now + 86_400, fetch=fetch))
    assert len(published) == 2 and written[1] == ("search_news", "news:status:NEW1:start")


def test_a_stale_or_skipped_item_is_never_posted(monkeypatch):
    feeds = {news.BLOG_FEED: b"<rss><channel></channel></rss>", news.STATUS_FEED: b"[]"}
    daemon, fetch, written, published = _news_daemon(monkeypatch, feeds)
    daemon.state.add_news_items([{"id": "seed", "title": "t", "url": "u", "published": 0.0}], seeding=True)
    feeds[news.BLOG_FEED] = BLOG
    published_at = news.blog_items(BLOG)[0]["published"]
    run(daemon._news_step(now=published_at + news.FRESH_SECONDS + 3_600, fetch=fetch))
    assert written == [] and published == [], "older than 3 days on first sight, and the event post is skipped"


def test_the_summary_counts_news_posts_apart_from_the_two_slots(tmp_path):
    from bot.state import State
    from scripts.daily_summary import build_summary

    path = str(tmp_path / "s.db")
    s = State(path)
    now = 1_792_500_000
    s.record("post", "slot post", now=now - 600)
    s.record("post", "news post", now=now - 300)
    s.add_news_items([{"id": "n1", "title": "t", "url": "u", "published": now - 900}], seeding=False, now=now - 900)
    s.finish_news_item("n1", status="published", now=now - 300)
    assert "Original posts published (24h): 1 of 2 slots, plus 1 news post" in build_summary(path, now, 35)


def test_live_config_runs_news_posts():
    cfg = json.loads(Path(__file__).resolve().parent.parent.joinpath("config.json").read_text())
    assert cfg["news_posts"] is True and cfg["post_prompt_version"] == "v4"


def test_a_news_post_never_takes_a_plan_slots_write_budget():
    """Oct 24 2026: two plan posts a day for 30 days is $0.90 of the $1 monthly
    write cap, so a news post may only use what is left after every remaining
    slot post of the month."""
    import calendar

    from tests.test_bot import _cfg
    from tests.test_telegram_approval import state as make_state

    cfg = _cfg()
    cfg.pacing.standalone_posts_per_day = 2
    now = float(calendar.timegm((2026, 11, 2, 9, 0, 0)))  # Nov 2, before either slot: 58 slot posts left
    s = make_state()
    assert news.write_budget_allows_news(cfg, s, now), "$0.87 reserved + $0.015 news fits $1"
    s.record_publisher_usage(endpoint="post", remote_post_id="a", cost_microusd=120_000, now=now - 86_400)
    assert not news.write_budget_allows_news(cfg, s, now), "$0.12 spent + $0.87 reserved leaves no room"
    late = float(calendar.timegm((2026, 11, 30, 20, 0, 0)))
    s2 = make_state()
    s2.record_publisher_usage(endpoint="post", remote_post_id="b", cost_microusd=870_000, now=late - 2 * 86_400)
    s2.record_publisher_usage(endpoint="post", remote_post_id="c", cost_microusd=15_000, now=late - 7_200)
    s2.record_publisher_usage(endpoint="post", remote_post_id="d", cost_microusd=15_000, now=late - 3_600)
    s2.record("post", "slot 1", now=late - 7_200)
    s2.record("post", "slot 2", now=late - 3_600)
    assert news.write_budget_allows_news(cfg, s2, late), "both of today's slots are done; the last $0.10 is free"
