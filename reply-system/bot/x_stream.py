"""Official X filtered-stream client with idempotent canonical rules."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator

import httpx

from .config import Config

X_API_ROOT = "https://api.x.com"
X_TOKEN_URL = f"{X_API_ROOT}/oauth2/token"
X_RULES_URL = f"{X_API_ROOT}/2/tweets/search/stream/rules"
X_STREAM_URL = f"{X_API_ROOT}/2/tweets/search/stream"
MAX_RULE_VALUE = 1024
MAX_CONTEXT_ITEM_CHARS = 500
MAX_CONTEXT_ITEMS = 3


class XStreamError(RuntimeError):
    """Official X stream or rule management failed."""


class XCreditsDepleted(XStreamError):
    """X billing rejected a request because usable API credits are unavailable."""


class XRateLimitError(XStreamError):
    """X rejected a request until an endpoint-specific rate window resets."""

    def __init__(self, message: str, *, retry_at: float = 0.0):
        super().__init__(message)
        self.retry_at = max(0.0, float(retry_at or 0.0))
        # "This stream is currently at the maximum allowed connection limit":
        # X still counts the connection that just dropped. It is released
        # within about a minute, long before the 15-minute rate window resets.
        self.connection_limit = "connection limit" in message.lower()


@dataclass(frozen=True)
class RuleSpec:
    value: str
    tag: str


def _bounded_context(value: object) -> str:
    return " ".join(str(value or "").split())[:MAX_CONTEXT_ITEM_CHARS]


def _link_context(tweet: dict) -> list[dict[str, str]]:
    """Preserve X-supplied URL preview evidence without fetching the web."""
    entities = tweet.get("entities") or {}
    note_tweet = tweet.get("note_tweet") or {}
    note_entities = (
        note_tweet.get("entities") or {}
        if isinstance(note_tweet, dict)
        else {}
    )
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for container in (entities, note_entities):
        if not isinstance(container, dict):
            continue
        for item in container.get("urls") or []:
            if not isinstance(item, dict):
                continue
            row = {
                "url": _bounded_context(
                    item.get("unwound_url") or item.get("expanded_url")
                ),
                "title": _bounded_context(item.get("title")),
                "description": _bounded_context(item.get("description")),
            }
            fingerprint = (row["url"], row["title"], row["description"])
            # A naked expanded URL is not substantive evidence. It remains a
            # link-only post and fails closed in the deterministic gate.
            if not (row["title"] or row["description"]) or fingerprint in seen:
                continue
            seen.add(fingerprint)
            rows.append(row)
            if len(rows) >= MAX_CONTEXT_ITEMS:
                return rows
    return rows


def _media_context(tweet: dict, media_by_key: dict[str, dict]) -> list[dict[str, str]]:
    """Return only creator-authored alt text supplied in the stream event."""
    attachments = tweet.get("attachments") or {}
    if not isinstance(attachments, dict):
        return []
    rows: list[dict[str, str]] = []
    for media_key in attachments.get("media_keys") or []:
        media = media_by_key.get(str(media_key)) or {}
        alt_text = _bounded_context(media.get("alt_text"))
        if not alt_text:
            continue
        rows.append({
            "type": _bounded_context(media.get("type")),
            "alt_text": alt_text,
        })
        if len(rows) >= MAX_CONTEXT_ITEMS:
            break
    return rows


def keyword_clause(keywords: list[str] | None) -> str:
    terms = [
        f'"{k}"' if " " in k else k
        for k in dict.fromkeys(str(k).strip() for k in (keywords or []))
        if k
    ]
    return f" ({' OR '.join(terms)})" if terms else ""


KEYWORD_CLAUSE_BUDGET = 480   # leaves room for ~25 handles per rule


def keyword_chunks(keywords: list[str] | None) -> list[list[str]]:
    """Split a long topic list so every rule stays under X's length limit.

    X delivers (and bills) a post once even when several rules match it, so
    splitting the topic words across rules changes nothing but rule count.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    for word in dict.fromkeys(str(k).strip() for k in (keywords or []) if str(k).strip()):
        if current and len(keyword_clause(current + [word])) > KEYWORD_CLAUSE_BUDGET:
            chunks.append(current)
            current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks or [[]]


def build_rule_values(
    handles: list[str],
    keywords: list[str] | None = None,
) -> list[str]:
    """Pack targets into legal original, English stream rules.

    Without keywords every original post from the roster is delivered. With
    keywords, only posts containing one of them are delivered (and billed);
    long keyword lists are split across rules.

    Multiple rules still share the single production stream connection.
    """
    normalized = sorted(
        {
            handle.lstrip("@").strip().lower()
            for handle in handles
            if handle.lstrip("@").strip()
        }
    )
    if not normalized:
        return []

    values: list[str] = []
    for chunk in keyword_chunks(keywords):
        suffix = f"){keyword_clause(chunk)} -is:reply -is:retweet lang:en"
        terms: list[str] = []
        for handle in normalized:
            term = f"from:{handle}"
            candidate_terms = terms + [term]
            candidate = f"({' OR '.join(candidate_terms)}{suffix}"
            if len(candidate) > MAX_RULE_VALUE:
                if not terms:
                    raise ValueError(f"X handle cannot fit in one rule: {handle}")
                values.append(f"({' OR '.join(terms)}{suffix}")
                terms = [term]
            else:
                terms = candidate_terms
        if terms:
            values.append(f"({' OR '.join(terms)}{suffix}")
    return values


def build_reply_rule_values(
    handles: list[str],
    keywords: list[str],
) -> list[str]:
    """Pack a narrow self-contained-reply lane into canonical stream rules."""
    normalized_handles = sorted(
        {
            handle.lstrip("@").strip().lower()
            for handle in handles
            if handle.lstrip("@").strip()
        }
    )
    normalized_keywords = list(
        dict.fromkeys(
            str(keyword).strip()
            for keyword in keywords
            if str(keyword).strip()
        )
    )
    if not normalized_handles:
        return []
    keyword_terms = [
        f'"{keyword}"' if " " in keyword else keyword
        for keyword in normalized_keywords
    ]
    keyword_clause = (
        f" ({' OR '.join(keyword_terms)})"
        if keyword_terms
        else ""
    )
    suffix = f"){keyword_clause} is:reply -is:retweet lang:en"
    values: list[str] = []
    terms: list[str] = []
    for handle in normalized_handles:
        term = f"from:{handle}"
        candidate_terms = terms + [term]
        candidate = f"({' OR '.join(candidate_terms)}{suffix}"
        if len(candidate) > MAX_RULE_VALUE:
            if not terms:
                raise ValueError(
                    "reply keywords leave no room for target handle: " + handle
                )
            values.append(f"({' OR '.join(terms)}{suffix}")
            terms = [term]
        else:
            terms = candidate_terms
    if terms:
        values.append(f"({' OR '.join(terms)}{suffix}")
    return values


def canonical_rule_specs(
    handles: list[str],
    namespace: str,
    *,
    reply_handles: list[str] | None = None,
    reply_keywords: list[str] | None = None,
    discovery_rules: list[str] | None = None,
    original_keywords: list[str] | None = None,
) -> list[RuleSpec]:
    originals = build_rule_values(handles, original_keywords)
    replies = build_reply_rule_values(
        reply_handles or [],
        reply_keywords or [],
    )
    fixed = [
        RuleSpec(value=value, tag=f"{namespace}-{index:03d}")
        for index, value in enumerate(originals + replies, start=1)
    ]
    discovery = [
        RuleSpec(
            value=str(value).strip(),
            tag=f"{namespace}-discovery-{index:03d}",
        )
        for index, value in enumerate(discovery_rules or [], start=1)
        if str(value).strip()
    ]
    if any(len(rule.value) > MAX_RULE_VALUE for rule in discovery):
        raise ValueError("dynamic discovery rule exceeds X's 1024-character limit")
    return fixed + discovery


def parse_stream_event(payload: dict) -> dict | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    includes = payload.get("includes") or {}
    users = {
        str(user.get("id")): user
        for user in includes.get("users", [])
        if isinstance(user, dict)
    }
    included_tweets = {
        str(tweet.get("id")): tweet
        for tweet in includes.get("tweets", [])
        if isinstance(tweet, dict) and tweet.get("id")
    }
    media_by_key = {
        str(media.get("media_key")): media
        for media in includes.get("media", [])
        if isinstance(media, dict) and media.get("media_key")
    }
    created_at = data.get("created_at")
    created_epoch = None
    if created_at:
        try:
            created_epoch = datetime.fromisoformat(
                str(created_at).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            created_epoch = None
    references = data.get("referenced_tweets") or []
    reference_types = {
        str(reference.get("type"))
        for reference in references
        if isinstance(reference, dict)
    }
    note_tweet = data.get("note_tweet") or {}
    full_text = (
        str(note_tweet.get("text") or "")
        if isinstance(note_tweet, dict)
        else ""
    ).strip()
    replied_to_id = next(
        (
            str(reference.get("id") or "")
            for reference in references
            if isinstance(reference, dict)
            and str(reference.get("type") or "") == "replied_to"
        ),
        "",
    )
    quoted_id = next(
        (
            str(reference.get("id") or "")
            for reference in references
            if isinstance(reference, dict)
            and str(reference.get("type") or "") == "quoted"
        ),
        "",
    )
    parent = included_tweets.get(replied_to_id) or {}
    quoted = included_tweets.get(quoted_id) or {}
    parent_note_tweet = parent.get("note_tweet") or {}
    parent_full_text = (
        str(parent_note_tweet.get("text") or "")
        if isinstance(parent_note_tweet, dict)
        else ""
    ).strip()
    quoted_note_tweet = quoted.get("note_tweet") or {}
    quoted_full_text = (
        str(quoted_note_tweet.get("text") or "")
        if isinstance(quoted_note_tweet, dict)
        else ""
    ).strip()
    author = users.get(str(data.get("author_id"))) or {}
    parent_author = users.get(str(parent.get("author_id") or "")) or {}
    quoted_author = users.get(str(quoted.get("author_id") or "")) or {}
    matching_rules = payload.get("matching_rules") or []
    return {
        "id": str(data.get("id") or ""),
        # X truncates the ordinary `text` field for long-form posts. Supplying
        # the complete Note Tweet to the writer avoids drafts grounded in a
        # sentence that was cut off halfway through.
        "text": full_text or str(data.get("text") or ""),
        "author": {
            "id": str(author.get("id") or data.get("author_id") or ""),
            "username": str(author.get("username") or ""),
            "name": str(author.get("name") or ""),
            "description": str(author.get("description") or ""),
            "public_metrics": author.get("public_metrics") or {},
            "verified": bool(author.get("verified")),
            "verified_type": str(author.get("verified_type") or ""),
        },
        "created_at_epoch": created_epoch,
        "is_reply": "replied_to" in reference_types,
        "is_retweet": "retweeted" in reference_types,
        "parent_id": replied_to_id,
        "parent_text": (
            parent_full_text or str(parent.get("text") or "")
        ).strip(),
        "parent_author": str(parent_author.get("username") or ""),
        "quote_id": quoted_id,
        "quote_text": (
            quoted_full_text or str(quoted.get("text") or "")
        ).strip(),
        "quote_author": str(quoted_author.get("username") or ""),
        "link_context": _link_context(data),
        "media_context": _media_context(data, media_by_key),
        "lang": str(data.get("lang") or ""),
        "matching_rules": matching_rules,
        "dynamic_discovery_match": any(
            "-discovery-" in str(rule.get("tag") or "")
            for rule in matching_rules
            if isinstance(rule, dict)
        ),
    }


class OfficialXStream:
    def __init__(
        self,
        cfg: Config,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None
        self._bearer = cfg.x_bearer_token

    async def __aenter__(self) -> "OfficialXStream":
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self.cfg.x_detection.request_timeout_seconds,
                # X sends a keep-alive newline every 20 seconds. Silence for 90
                # seconds is a dead connection: reconnect instead of going blind.
                read=90.0,
                write=self.cfg.x_detection.request_timeout_seconds,
                pool=self.cfg.x_detection.request_timeout_seconds,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        if not self._bearer:
            self._bearer = await self._create_bearer()
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OfficialXStream must be used as an async context manager")
        return self._client

    @property
    def headers(self) -> dict[str, str]:
        if not self._bearer:
            raise RuntimeError("official X bearer token is unavailable")
        return {"Authorization": f"Bearer {self._bearer}"}

    async def _create_bearer(self) -> str:
        if not self.cfg.x_api_key or not self.cfg.x_api_secret:
            raise XStreamError(
                "X_API_KEY and X_API_SECRET are required for official read access"
            )
        response = await self.client.post(
            X_TOKEN_URL,
            auth=httpx.BasicAuth(self.cfg.x_api_key, self.cfg.x_api_secret),
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            raise XStreamError(
                f"official X app authentication failed HTTP {response.status_code}"
            )
        token = str(response.json().get("access_token") or "")
        if not token:
            raise XStreamError("official X app authentication returned no bearer token")
        return token

    async def _json(self, method: str, url: str, **kwargs) -> dict:
        response = await self.client.request(
            method,
            url,
            headers=self.headers,
            **kwargs,
        )
        if response.status_code < 200 or response.status_code >= 300:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("detail") or body.get("title") or "")[:180]
            except Exception:
                pass
            suffix = f": {detail}" if detail else ""
            message = (
                f"official X request failed HTTP {response.status_code}{suffix}"
            )
            if response.status_code == 402:
                raise XCreditsDepleted(message)
            if response.status_code == 429:
                raise XRateLimitError(
                    message,
                    retry_at=float(response.headers.get("x-rate-limit-reset") or 0),
                )
            raise XStreamError(message)
        return response.json()

    async def end_stale_stream_connections(self) -> int:
        """End filtered-stream connections X still counts for this app.

        Call only while this process holds no stream. The replies daemon is
        the app's single stream consumer, so a connection X lists as active
        at that moment is one X never noticed closing. One opened during a
        reconnect stayed "active" for two days and answered every reconnect
        with the connection-limit 429. Only this app's own connections are
        listed and ended (app bearer token); reads are not billed."""
        listed = await self._json(
            "GET",
            f"{X_API_ROOT}/2/connections",
            params={
                "status": "active",
                "endpoints": "filtered_stream",
                "max_results": 20,
                "connection.fields": "endpoint_name,connected_at",
            },
        )
        ids = [
            str(item["id"])
            for item in listed.get("data") or []
            if isinstance(item, dict)
            and item.get("id")
            and item.get("endpoint_name", "filtered_stream") == "filtered_stream"
        ]
        if not ids:
            return 0
        ended = await self._json(
            "DELETE",
            f"{X_API_ROOT}/2/connections",
            json={"uuids": ids},
        )
        return int((ended.get("data") or {}).get("successful_kills") or 0)

    async def list_rules(self) -> list[dict]:
        body = await self._json("GET", X_RULES_URL)
        return [
            rule for rule in body.get("data", [])
            if isinstance(rule, dict)
        ]

    async def recent_originals(
        self,
        user_id: str,
        *,
        max_results: int = 10,
    ) -> dict:
        """Fetch a bounded reach sample without opening another stream."""
        bounded = max(5, min(100, int(max_results)))
        return await self._json(
            "GET",
            f"{X_API_ROOT}/2/users/{str(user_id)}/tweets",
            params={
                "max_results": bounded,
                "exclude": "retweets,replies",
                "tweet.fields": (
                    "created_at,public_metrics,referenced_tweets,note_tweet"
                ),
            },
        )

    async def ensure_canonical_rules(self, handles: list[str]) -> list[RuleSpec]:
        expected = canonical_rule_specs(
            handles,
            self.cfg.x_detection.rule_namespace,
            reply_handles=(
                self.cfg.x_detection.reply_source_handles
                if self.cfg.x_detection.include_self_contained_replies
                else []
            ),
            reply_keywords=self.cfg.x_detection.reply_rule_keywords,
            original_keywords=self.cfg.x_detection.original_rule_keywords,
            discovery_rules=(
                self.cfg.x_detection.dynamic_discovery_rules
                if self.cfg.x_detection.dynamic_discovery_enabled
                else []
            ),
        )
        if not expected:
            raise XStreamError("no target handles were configured")

        existing = await self.list_rules()
        namespace_prefix = f"{self.cfg.x_detection.rule_namespace}-"
        unmanaged = [
            rule for rule in existing
            if not str(rule.get("tag") or "").startswith(namespace_prefix)
        ]
        if unmanaged:
            tags = ", ".join(
                str(rule.get("tag") or rule.get("id") or "untagged")
                for rule in unmanaged[:5]
            )
            raise XStreamError(
                "official X app contains unmanaged stream rules; "
                f"remove or account for them first: {tags}"
            )

        current = {
            (str(rule.get("tag") or ""), str(rule.get("value") or ""))
            for rule in existing
        }
        wanted = {(rule.tag, rule.value) for rule in expected}
        if current == wanted and len(existing) == len(expected):
            return expected

        managed_ids = [
            str(rule.get("id"))
            for rule in existing
            if rule.get("id")
        ]
        if managed_ids:
            await self._json(
                "POST",
                X_RULES_URL,
                json={"delete": {"ids": managed_ids}},
            )
        await self._json(
            "POST",
            X_RULES_URL,
            json={
                "add": [
                    {"value": rule.value, "tag": rule.tag}
                    for rule in expected
                ]
            },
        )
        installed = await self.list_rules()
        installed_pairs = {
            (str(rule.get("tag") or ""), str(rule.get("value") or ""))
            for rule in installed
        }
        if installed_pairs != wanted or len(installed) != len(expected):
            raise XStreamError("official X canonical rule verification failed")
        return expected

    async def events(self) -> AsyncIterator[dict]:
        params = {
            "tweet.fields": (
                "created_at,author_id,referenced_tweets,lang,note_tweet,"
                "attachments,entities"
            ),
            # The parent is returned inside the same production stream event.
            # This gives terse target replies enough conversational context
            # without a second websocket or a follow-up lookup request.
            "expansions": (
                "author_id,referenced_tweets.id,"
                "referenced_tweets.id.author_id,attachments.media_keys"
            ),
            "media.fields": "media_key,type,alt_text",
            "user.fields": (
                "username,name,description,public_metrics,verified,verified_type"
            ),
        }
        try:
            async with self.client.stream(
                "GET",
                X_STREAM_URL,
                headers=self.headers,
                params=params,
            ) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    detail = ""
                    try:
                        body = json.loads(raw.decode("utf-8", errors="replace"))
                        detail = str(
                            body.get("detail") or body.get("title") or ""
                        )[:180]
                    except Exception:
                        pass
                    suffix = f": {detail}" if detail else ""
                    message = (
                        "official X stream failed HTTP "
                        f"{response.status_code}{suffix}"
                    )
                    if response.status_code == 402:
                        raise XCreditsDepleted(message)
                    if response.status_code == 429:
                        raise XRateLimitError(
                            message,
                            retry_at=float(
                                response.headers.get("x-rate-limit-reset") or 0
                            ),
                        )
                    raise XStreamError(message)
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = parse_stream_event(payload)
                    if event is not None:
                        yield event
        except httpx.TransportError as exc:
            raise XStreamError(
                "official X stream transport interrupted"
            ) from exc
