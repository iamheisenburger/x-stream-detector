"""News posts (Oct 24 2026): a post within hours of an official Google Search change.

Arshad keeps @pentradev a faceless brand account, so posts carry the growth;
timely, sourced news is what faceless accounts grow on. Two official sources
only: the Google Search Central blog and the Google Search Status Dashboard
(core, spam and other ranking updates, and indexing or serving incidents).
Every fact in the post comes from the item's own text, fetched from Google.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BLOG_FEED = "https://developers.google.com/search/blog/feed.xml"
STATUS_FEED = "https://status.search.google.com/incidents.json"

# A news item is posted only when it first appears and is at most this old.
FRESH_SECONDS = 3 * 86_400
# The feeds are read at most this often.
CHECK_SECONDS = 1_800
# One news post a day keeps the posts inside the $0.05 daily write cap
# (two plan posts and one news post at $0.015 each).
NEWS_POSTS_PER_DAY = 1
CARD_CHARS = 2_000

# Event, community and video posts teach a small business nothing to act on.
_SKIP_TITLE_RE = re.compile(
    r"\b(?:search central live|live|events?|meetups?|speakers|conferences?|coming to|webinars?|office hours|"
    r"learning paths?|playlists?|youtube|podcasts?|episodes?|recap|community|hackathon)\b",
    re.I,
)
_LINK_RE = re.compile(r"<https?://[^>]+>|https?://\S+")
_FOOTER_RE = re.compile(r"\s*Except as otherwise noted, the content of this page is licensed.*$", re.S)


def write_budget_allows_news(cfg, state, now: float) -> bool:
    """A news post never takes a plan slot's write budget (Oct 24 2026): the
    month's $1 cap must still hold every remaining slot post after it (two a
    day for 30 days is $0.90), and so must the day's $0.05 cap."""
    import calendar

    create = int(cfg.publishing.create_microusd)
    per_day = max(0, int(cfg.pacing.standalone_posts_per_day))
    day_start = calendar.timegm(time.gmtime(now)[:3] + (0, 0, 0))
    slot_posts_today = max(0, state.count_since("post", day_start) - state.news_published_since(day_start))
    slots_left_today = max(0, per_day - slot_posts_today)
    year, month, mday = time.gmtime(now)[:3]
    days_after_today = calendar.monthrange(year, month)[1] - mday
    month_reserve = create * (slots_left_today + per_day * days_after_today)
    if state.publisher_cost_microusd_month(now) + month_reserve + create > cfg.publishing.monthly_microusd_cap:
        return False
    return state.publisher_cost_microusd_today(now) + create * (slots_left_today + 1) <= cfg.publishing.daily_microusd_cap


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pentra-x news (+https://pentra.dev)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def _month_label(ts: float) -> str:
    return time.strftime("%B %Y", time.gmtime(ts)).lower()


def page_text(raw: str, limit: int = CARD_CHARS) -> str:
    """The article body of a developers.google.com page, as plain text."""
    start = raw.find('class="devsite-article-body')
    body = raw[start:] if start >= 0 else raw
    body = re.sub(r"(?is)<(script|style|nav|aside|footer)[^>]*>.*?</\1>", " ", body)
    parts = [
        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", part))).strip()
        for part in re.findall(r"(?is)<(?:p|li|h2|h3)[^>]*>(.*?)</(?:p|li|h2|h3)>", body)
    ]
    text = _FOOTER_RE.sub("", " ".join(part for part in parts if len(part) > 25))
    return _LINK_RE.sub("", text)[:limit].strip()


def blog_items(raw: bytes) -> list[dict]:
    items = []
    for item in ET.fromstring(raw).findall("./channel/item"):
        url = (item.findtext("link") or "").strip()
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            continue
        summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))).strip()
        if url and title:
            items.append({"id": f"blog:{url}", "kind": "blog", "title": title, "url": url,
                          "published": published, "summary": summary,
                          "skip": bool(_SKIP_TITLE_RE.search(title))})
    return items


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def status_items(raw: bytes) -> list[dict]:
    """One item when an incident or update starts; one more when a ranking
    update finishes rolling out."""
    items = []
    for incident in json.loads(raw):
        desc = str(incident.get("external_desc") or "").strip()
        updates = sorted(incident.get("updates") or [], key=lambda u: str(u.get("when") or u.get("created") or ""))
        texts = [_LINK_RE.sub("", str(u.get("text") or "")).strip() for u in updates]
        begin, end = _ts(incident.get("begin")), _ts(incident.get("end"))
        uri = str(incident.get("uri") or "")
        url = f"https://status.search.google.com/{uri.lstrip('/')}" if uri else "https://status.search.google.com/"
        if not desc or begin is None:
            continue
        first_link = next((m.group(0).strip("<>") for u in updates for m in [_LINK_RE.search(str(u.get("text") or ""))] if m), "")
        base = {"kind": "status", "url": url, "doc_url": first_link, "skip": False}
        items.append({**base, "id": f"status:{incident.get('id')}:start", "title": desc, "published": begin,
                      "summary": texts[0] if texts else ""})
        if end is not None and re.search(r"\bupdate\b", desc, re.I):
            items.append({**base, "id": f"status:{incident.get('id')}:end", "title": f"{desc}: rollout complete",
                          "published": end, "summary": " ".join(texts)})
    return items


def news_cards(item: dict, fetch=fetch) -> list[dict]:
    """The item's own text as the post's evidence: a blog post's article body,
    or a status update plus the Google doc page it links to."""
    label = "google search central blog" if item["kind"] == "blog" else "google search status dashboard"
    when = time.strftime("%b %d, %Y", time.gmtime(item["published"]))
    text = f"{item['title']} ({label}, {when}). {item.get('summary') or ''}".strip()
    if item["kind"] == "blog":
        try:
            body = page_text(fetch(item["url"]).decode("utf-8", "replace"))
        except Exception:
            body = ""
        if body:
            text = f"{item['title']} ({label}, {when}). {body}"
    cards = [{"id": f"news:{item['id']}", "text": text[:CARD_CHARS], "source": f"{label}, {_month_label(item['published'])}",
              "url": item["url"]}]
    doc_url = str(item.get("doc_url") or "")
    if doc_url.startswith("https://developers.google.com/search/"):
        try:
            doc = page_text(fetch(doc_url).decode("utf-8", "replace"), limit=1_500)
        except Exception:
            doc = ""
        if doc:
            cards.append({"id": f"news-doc:{doc_url}", "text": doc, "source": "google search central documentation",
                          "url": doc_url})
    return cards
