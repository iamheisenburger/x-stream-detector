"""Turn one filtered-stream payload into a flat post dict."""
from __future__ import annotations

from datetime import datetime

MAX_CONTEXT_ITEM_CHARS = 500
MAX_CONTEXT_ITEMS = 3


def _bounded(value: object) -> str:
    return " ".join(str(value or "").split())[:MAX_CONTEXT_ITEM_CHARS]


def _full_text(tweet: dict) -> str:
    # X truncates `text` for long posts; the complete text is in note_tweet.
    note = tweet.get("note_tweet") or {}
    full = str(note.get("text") or "").strip() if isinstance(note, dict) else ""
    return full or str(tweet.get("text") or "").strip()


def _link_context(tweet: dict) -> list[dict[str, str]]:
    """Link previews X already attached to the post (no web fetch)."""
    note = tweet.get("note_tweet") or {}
    containers = [tweet.get("entities") or {}, (note.get("entities") or {}) if isinstance(note, dict) else {}]
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for container in containers:
        if not isinstance(container, dict):
            continue
        for item in container.get("urls") or []:
            if not isinstance(item, dict):
                continue
            row = {
                "url": _bounded(item.get("unwound_url") or item.get("expanded_url")),
                "title": _bounded(item.get("title")),
                "description": _bounded(item.get("description")),
            }
            key = (row["url"], row["title"], row["description"])
            if not (row["title"] or row["description"]) or key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= MAX_CONTEXT_ITEMS:
                return rows
    return rows


def _media_context(tweet: dict, media_by_key: dict[str, dict]) -> list[dict[str, str]]:
    """Alt text the creator wrote for attached images or video."""
    attachments = tweet.get("attachments") or {}
    if not isinstance(attachments, dict):
        return []
    rows: list[dict[str, str]] = []
    for key in attachments.get("media_keys") or []:
        media = media_by_key.get(str(key)) or {}
        alt = _bounded(media.get("alt_text"))
        if alt:
            rows.append({"type": _bounded(media.get("type")), "alt_text": alt})
        if len(rows) >= MAX_CONTEXT_ITEMS:
            break
    return rows


def _epoch(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_stream_event(payload: dict) -> dict | None:
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        return None
    includes = payload.get("includes") or {}
    users = {str(u.get("id")): u for u in includes.get("users", []) if isinstance(u, dict)}
    included = {str(t.get("id")): t for t in includes.get("tweets", []) if isinstance(t, dict) and t.get("id")}
    media_by_key = {
        str(m.get("media_key")): m for m in includes.get("media", []) if isinstance(m, dict) and m.get("media_key")
    }
    references = [r for r in data.get("referenced_tweets") or [] if isinstance(r, dict)]
    ref_types = {str(r.get("type")) for r in references}

    def ref(kind: str) -> str:
        return next((str(r.get("id") or "") for r in references if str(r.get("type") or "") == kind), "")

    parent_id, quote_id = ref("replied_to"), ref("quoted")
    parent, quoted = included.get(parent_id) or {}, included.get(quote_id) or {}
    author = users.get(str(data.get("author_id"))) or {}
    username = str(author.get("username") or "")
    post_id = str(data["id"])
    matching = [r for r in payload.get("matching_rules") or [] if isinstance(r, dict)]
    return {
        "id": post_id,
        "url": f"https://x.com/{username or 'i'}/status/{post_id}",
        "text": _full_text(data),
        "author": {
            "id": str(author.get("id") or data.get("author_id") or ""),
            "username": username,
            "name": str(author.get("name") or ""),
            "description": str(author.get("description") or ""),
            "public_metrics": author.get("public_metrics") or {},
            "verified": bool(author.get("verified")),
            "verified_type": str(author.get("verified_type") or ""),
        },
        "created_at_epoch": _epoch(data.get("created_at")),
        "lang": str(data.get("lang") or ""),
        "is_reply": "replied_to" in ref_types,
        "is_quote": "quoted" in ref_types,
        "parent_id": parent_id,
        "parent_text": _full_text(parent) if parent else "",
        "parent_author": str((users.get(str(parent.get("author_id") or "")) or {}).get("username") or ""),
        "quote_id": quote_id,
        "quote_text": _full_text(quoted) if quoted else "",
        "quote_author": str((users.get(str(quoted.get("author_id") or "")) or {}).get("username") or ""),
        "link_context": _link_context(data),
        "media_context": _media_context(data, media_by_key),
        "matching_rule_tags": [str(r.get("tag") or "") for r in matching],
        # Posts delivered inside this event (the quoted or replied-to post).
        # The ledger counts them too, so it errs high, never low.
        "included_post_ids": sorted(i for i in included if i != post_id),
    }
