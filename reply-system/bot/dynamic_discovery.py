"""Fail-closed reach and relevance verification for streamed author discovery.

The official X filtered stream supplies timely candidate posts. TwitterAPI.io
is used only for one bounded recent-post audit per previously unseen author;
it is never opened as another websocket. Verdicts are durable and expire so a
cached account cannot silently remain eligible after its content mix changes.
"""
from __future__ import annotations

import re
import statistics
import time
from datetime import datetime

import httpx

from .config import Config
from .state import State

API_URL = "https://api.twitterapi.io/twitter/user/last_tweets"
CREDITS_PER_RETURNED_TWEET = 15
URL_RE = re.compile(r"https?://\S+", re.I)
TOPICAL_RE = re.compile(
    r"\b(?:ai|agi|artificial intelligence|machine learning|llm|models?|agents?|"
    r"automation|software|saas|product|startup|founders?|operators?|company|"
    r"business|b2b|customers?|users?|developers?|engineering|code|coding|"
    r"cloud|api|growth|marketing|sales|revenue|conversion|design|launch|ship|"
    r"build|teams?|hiring|enterprise|open[- ]source|workflow|pricing|strategy|"
    r"management|leadership|ecommerce|commerce|retail|creator economy|creators?|"
    r"workplace|productivity|careers?|work|operations|strategy|consulting|"
    r"agencies?|services|content|audience|distribution|brands?|branding|"
    r"communication|writing|newsletters?|podcasts?|entrepreneurship|venture|"
    r"investors?|capital|fundraising|science|scientists?|researchers?|health|"
    r"fitness|education|teachers?|professors?|learning|psychology|habits?|"
    r"self[- ]improvement|doctors?|medicine)\b",
    re.I,
)
FORBIDDEN_RE = re.compile(
    r"\b(?:elections?|president|senator|congress|republican|democrat|trump|"
    r"biden|war|military|iran|israel|ukraine|gaza|football|soccer|nba|nfl|"
    r"cricket|movie|music|celebrity|crypto|bitcoin|ethereum|web3|memecoin|"
    r"stocks?|shares?|trading|portfolio|market rally|breaking news)\b",
    re.I,
)
FORBIDDEN_PROFILE_RE = re.compile(
    r"\b(?:news|journalist|reporter|correspondent|newsroom|news organization|"
    r"breaking news|political commentator|politician|actor|musician|athlete|"
    r"sports network|newspaper|magazine|publisher|crypto trader|web3 investor|"
    r"stock trader)\b",
    re.I,
)


def _timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for parser in (
        lambda: datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y"),
        lambda: datetime.fromisoformat(text.replace("Z", "+00:00")),
    ):
        try:
            return parser().timestamp()
        except ValueError:
            continue
    return 0.0


def _tweets(body: dict) -> list[dict]:
    rows = body.get("tweets") or (body.get("data") or {}).get("tweets") or []
    return [row for row in rows if isinstance(row, dict)]


def _official_as_provider_body(body: dict, stream_author: dict) -> dict:
    """Normalize official X v2 posts into the shared measurement shape."""
    author = {
        "userName": stream_author.get("username") or "",
        "name": stream_author.get("name") or "",
        "description": stream_author.get("description") or "",
        "followers": (
            (stream_author.get("public_metrics") or {}).get("followers_count")
            or 0
        ),
    }
    tweets = []
    for row in body.get("data") or []:
        if not isinstance(row, dict):
            continue
        note_tweet = row.get("note_tweet") or {}
        metrics = row.get("public_metrics") or {}
        tweets.append(
            {
                "id": str(row.get("id") or ""),
                "createdAt": row.get("created_at"),
                "viewCount": metrics.get("impression_count") or 0,
                "text": (
                    note_tweet.get("text")
                    if isinstance(note_tweet, dict)
                    else ""
                ) or row.get("text") or "",
                "isReply": False,
                "retweeted_tweet": None,
                "author": author,
            }
        )
    return {"tweets": tweets}


def measure_author(
    *,
    handle: str,
    body: dict,
    cfg: Config,
    now: float,
    stream_author: dict | None = None,
) -> dict:
    """Produce an auditable deterministic verdict from recent originals."""
    rows = _tweets(body)
    min_created = now - (
        max(1, cfg.x_detection.dynamic_discovery_max_post_age_days) * 86_400
    )
    max_created = now - max(
        0, cfg.x_detection.dynamic_discovery_min_post_age_seconds
    )
    originals: list[tuple[float, int, str]] = []
    for tweet in rows:
        if tweet.get("isReply") or tweet.get("retweeted_tweet"):
            continue
        created = _timestamp(tweet.get("createdAt"))
        if not created or created < min_created or created > max_created:
            continue
        try:
            views = int(tweet.get("viewCount") or 0)
        except (TypeError, ValueError):
            continue
        text = str(tweet.get("text") or "").strip()
        originals.append((created, views, text))
    originals.sort(key=lambda row: row[0], reverse=True)
    sample = originals[: max(1, cfg.x_detection.dynamic_discovery_max_sample_posts)]

    author = (rows[0].get("author") if rows else {}) or {}
    stream_author = stream_author or {}
    bio = str(
        (author.get("profile_bio") or {}).get("description")
        or author.get("description")
        or stream_author.get("description")
        or ""
    ).strip()
    metrics = stream_author.get("public_metrics") or {}
    try:
        followers = int(
            author.get("followers")
            or metrics.get("followers_count")
            or 0
        )
    except (TypeError, ValueError):
        followers = 0

    clean_texts = [URL_RE.sub("", text).strip() for _, _, text in sample]
    topical_posts = sum(bool(TOPICAL_RE.search(text)) for text in clean_texts)
    forbidden_posts = sum(bool(FORBIDDEN_RE.search(text)) for text in clean_texts)
    sample_posts = len(sample)
    topical_ratio = topical_posts / sample_posts if sample_posts else 0.0
    forbidden_ratio = forbidden_posts / sample_posts if sample_posts else 0.0
    view_values = [views for _, views, _ in sample]
    median_views = float(statistics.median(view_values)) if view_values else 0.0

    status = "qualified"
    reason = "fresh reach and relevance gates passed"
    if sample_posts < cfg.x_detection.dynamic_discovery_min_sample_posts:
        status = "rejected"
        reason = "fewer than three mature recent original posts"
    elif median_views < cfg.x_detection.dynamic_discovery_min_median_views:
        status = "rejected"
        reason = "fresh recent-original median is below 100K impressions"
    elif FORBIDDEN_PROFILE_RE.search(bio):
        status = "rejected"
        reason = "profile indicates a forbidden news/politics/entertainment/finance lane"
    elif forbidden_ratio > cfg.x_detection.dynamic_discovery_max_forbidden_ratio:
        status = "rejected"
        reason = "recent originals are too concentrated in forbidden topics"
    elif topical_ratio < cfg.x_detection.dynamic_discovery_min_topical_ratio:
        status = "rejected"
        reason = "recent originals are not predominantly AI/software/business relevant"

    evidence = [
        {
            "created_at": created,
            "views": views,
            "topical": bool(TOPICAL_RE.search(clean)),
            "forbidden": bool(FORBIDDEN_RE.search(clean)),
            "text": text[:500],
        }
        for (created, views, text), clean in zip(sample, clean_texts)
    ]
    return {
        "handle": handle.lstrip("@").lower(),
        "status": status,
        "median_views": median_views,
        "sample_posts": sample_posts,
        "topical_posts": topical_posts,
        "forbidden_posts": forbidden_posts,
        "followers": followers,
        "bio": bio,
        "reason": reason,
        "evidence": evidence,
        "estimated_provider_credits": len(rows) * CREDITS_PER_RETURNED_TWEET,
    }


class DynamicAuthorVerifier:
    def __init__(
        self,
        cfg: Config,
        state: State,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.cfg = cfg
        self.state = state
        self._client = client

    def _store_error(self, handle: str, reason: str, now: float) -> dict:
        verdict = {
            "handle": handle,
            "status": "error",
            "median_views": 0.0,
            "sample_posts": 0,
            "topical_posts": 0,
            "forbidden_posts": 0,
            "followers": 0,
            "bio": "",
            "reason": reason,
            "evidence": [],
            "estimated_provider_credits": 0,
        }
        # A provider outage or balance failure must not become a hot request
        # loop. Retry after ten minutes while remaining fail-closed.
        self.state.upsert_dynamic_author_audit(
            **verdict,
            checked_at=now,
            expires_at=now + 600,
        )
        return verdict

    def _store_profile_rejection(
        self,
        handle: str,
        stream_author: dict,
        now: float,
    ) -> dict:
        bio = str(stream_author.get("description") or "")
        verdict = {
            "handle": handle,
            "status": "rejected",
            "median_views": 0.0,
            "sample_posts": 0,
            "topical_posts": 0,
            "forbidden_posts": 0,
            "followers": int(
                (stream_author.get("public_metrics") or {}).get("followers_count")
                or 0
            ),
            "bio": bio,
            "reason": "profile indicates a forbidden news/politics/entertainment/finance lane",
            "evidence": [],
            "estimated_provider_credits": 0,
        }
        self.state.upsert_dynamic_author_audit(
            **verdict,
            checked_at=now,
            expires_at=(
                now + self.cfg.x_detection.dynamic_discovery_reject_ttl_seconds
            ),
        )
        return verdict

    async def verify_official(
        self,
        handle: str,
        *,
        user_id: str,
        stream_author: dict,
        api,
        record_read,
        now: float | None = None,
    ) -> dict:
        """Verify from a bounded official-X lookup on the existing client."""
        from .x_stream import XStreamError

        now = now or time.time()
        normalized = handle.lstrip("@").lower()
        cached = self.state.dynamic_author_audit(normalized, now=now)
        if cached is not None:
            cached["official_audit_exhausted"] = False
            return cached
        if (
            self.state.dynamic_author_audit_count_today(now)
            >= self.cfg.x_detection.dynamic_discovery_daily_author_audit_cap
        ):
            return {
                "handle": normalized,
                "status": "error",
                "reason": "daily author-audit cap reached",
                "official_audit_exhausted": False,
            }
        profile_text = " ".join(
            str(stream_author.get(key) or "")
            for key in ("username", "name", "description")
        )
        if FORBIDDEN_PROFILE_RE.search(profile_text):
            verdict = self._store_profile_rejection(
                normalized,
                stream_author,
                now,
            )
            verdict["official_audit_exhausted"] = False
            return verdict
        if not user_id:
            verdict = self._store_error(normalized, "official author id unavailable", now)
            verdict["official_audit_exhausted"] = False
            return verdict

        try:
            body = await api.recent_originals(
                user_id,
                max_results=(
                    self.cfg.x_detection.dynamic_discovery_official_sample_fetch_size
                ),
            )
        except XStreamError:
            verdict = self._store_error(normalized, "official reach audit request failed", now)
            verdict["official_audit_exhausted"] = False
            return verdict

        audit_exhausted = False
        rows = [
            row for row in body.get("data") or []
            if isinstance(row, dict) and row.get("id")
        ]
        for row in rows:
            snapshot = record_read(
                post_id=str(row.get("id")),
                author=normalized,
            )
            audit_exhausted = audit_exhausted or bool(snapshot.exhausted)
        verdict = measure_author(
            handle=normalized,
            body=_official_as_provider_body(body, stream_author),
            cfg=self.cfg,
            now=now,
            stream_author=stream_author,
        )
        verdict["estimated_provider_credits"] = 0
        ttl = (
            self.cfg.x_detection.dynamic_discovery_cache_ttl_seconds
            if verdict["status"] == "qualified"
            else self.cfg.x_detection.dynamic_discovery_reject_ttl_seconds
        )
        self.state.upsert_dynamic_author_audit(
            **verdict,
            checked_at=now,
            expires_at=now + max(60, ttl),
        )
        verdict["official_post_reads"] = len(rows)
        verdict["official_audit_exhausted"] = audit_exhausted
        return verdict

    async def verify(
        self,
        handle: str,
        *,
        stream_author: dict | None = None,
        now: float | None = None,
    ) -> dict:
        now = now or time.time()
        normalized = handle.lstrip("@").lower()
        cached = self.state.dynamic_author_audit(normalized, now=now)
        if cached is not None:
            return cached
        if not self.cfg.twitterapi_key:
            return self._store_error(normalized, "reach audit unavailable", now)
        if (
            self.state.dynamic_author_audit_count_today(now)
            >= self.cfg.x_detection.dynamic_discovery_daily_author_audit_cap
        ):
            return {
                "handle": normalized,
                "status": "error",
                "reason": "daily author-audit cap reached",
            }

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.cfg.x_detection.dynamic_discovery_request_timeout_seconds
        )
        try:
            response = await client.get(
                API_URL,
                params={"userName": normalized},
                headers={"X-API-Key": self.cfg.twitterapi_key},
            )
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("provider returned a non-object response")
            if response.status_code >= 400 or body.get("status") == "error":
                return self._store_error(
                    normalized,
                    f"reach audit provider HTTP {response.status_code}",
                    now,
                )
            verdict = measure_author(
                handle=normalized,
                body=body,
                cfg=self.cfg,
                now=now,
                stream_author=stream_author,
            )
        except (httpx.HTTPError, ValueError):
            return self._store_error(normalized, "reach audit provider error", now)
        finally:
            if owns_client:
                await client.aclose()

        ttl = (
            self.cfg.x_detection.dynamic_discovery_cache_ttl_seconds
            if verdict["status"] == "qualified"
            else self.cfg.x_detection.dynamic_discovery_reject_ttl_seconds
        )
        self.state.upsert_dynamic_author_audit(
            **verdict,
            checked_at=now,
            expires_at=now + max(60, ttl),
        )
        return verdict
