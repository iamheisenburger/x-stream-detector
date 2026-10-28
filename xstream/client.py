"""Official X API v2 filtered-stream client: app auth, idempotent rules, the stream."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from .events import parse_stream_event
from .rules import RuleSpec
from .settings import Settings

X_API_ROOT = "https://api.x.com"
X_TOKEN_URL = f"{X_API_ROOT}/oauth2/token"
X_RULES_URL = f"{X_API_ROOT}/2/tweets/search/stream/rules"
X_STREAM_URL = f"{X_API_ROOT}/2/tweets/search/stream"
X_CONNECTIONS_URL = f"{X_API_ROOT}/2/connections"

STREAM_PARAMS = {
    "tweet.fields": "created_at,author_id,referenced_tweets,lang,note_tweet,attachments,entities",
    # The quoted or replied-to post arrives inside the same event, so no
    # follow-up lookup is needed for context.
    "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id,attachments.media_keys",
    "media.fields": "media_key,type,alt_text",
    "user.fields": "username,name,description,public_metrics,verified,verified_type",
}


class StreamError(RuntimeError):
    """The stream or rule management failed."""


class CreditsDepleted(StreamError):
    """X answered 402: the developer account has no usable credits."""


class RateLimited(StreamError):
    """X answered 429."""

    def __init__(self, message: str, *, retry_at: float = 0.0):
        super().__init__(message)
        self.retry_at = max(0.0, float(retry_at or 0.0))
        # "This stream is currently at the maximum allowed connection limit":
        # X still counts the connection that just dropped. It is released
        # within about a minute, long before the 15-minute rate window resets.
        self.connection_limit = "connection limit" in message.lower()


def _error(status: int, detail: str, what: str, retry_at: float) -> StreamError:
    message = f"X {what} failed HTTP {status}" + (f": {detail}" if detail else "")
    if status == 402:
        return CreditsDepleted(message)
    if status == 429:
        return RateLimited(message, retry_at=retry_at)
    return StreamError(message)


def _detail(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
        return str(body.get("detail") or body.get("title") or "")[:180]
    except Exception:
        return ""


class XStreamClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._bearer = settings.x_bearer_token

    async def __aenter__(self) -> "XStreamClient":
        if self._client is None:
            timeout = self.settings.request_timeout_seconds
            # X sends a keep-alive newline every 20 seconds. Silence for 90
            # seconds is a dead connection: reconnect instead of going blind.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=timeout, read=90.0, write=timeout, pool=timeout)
            )
        if not self._bearer:
            self._bearer = await self._create_bearer()
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("XStreamClient must be used as an async context manager")
        return self._client

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer}"}

    async def _create_bearer(self) -> str:
        """App-only auth: the stream never touches a logged-in X account."""
        if not self.settings.x_api_key or not self.settings.x_api_secret:
            raise StreamError("set X_BEARER_TOKEN, or X_API_KEY and X_API_SECRET")
        response = await self.client.post(
            X_TOKEN_URL,
            auth=httpx.BasicAuth(self.settings.x_api_key, self.settings.x_api_secret),
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            raise StreamError(f"X app authentication failed HTTP {response.status_code}")
        token = str(response.json().get("access_token") or "")
        if not token:
            raise StreamError("X app authentication returned no bearer token")
        return token

    async def _json(self, method: str, url: str, **kwargs) -> dict:
        response = await self.client.request(method, url, headers=self.headers, **kwargs)
        if not 200 <= response.status_code < 300:
            raise _error(
                response.status_code,
                _detail(response.content),
                "request",
                float(response.headers.get("x-rate-limit-reset") or 0),
            )
        return response.json()

    async def list_rules(self) -> list[dict]:
        body = await self._json("GET", X_RULES_URL)
        return [rule for rule in body.get("data") or [] if isinstance(rule, dict)]

    async def sync_rules(self, expected: list[RuleSpec]) -> list[RuleSpec]:
        """Make the app's rules exactly `expected`. Unchanged rules are left
        alone, so a restart costs no rule writes."""
        if not expected:
            raise StreamError("no rules to install")
        existing = await self.list_rules()
        prefix = f"{self.settings.rule_namespace}-"
        unmanaged = [rule for rule in existing if not str(rule.get("tag") or "").startswith(prefix)]
        if unmanaged:
            # Their posts would be delivered and billed on this stream too.
            tags = ", ".join(str(r.get("tag") or r.get("id") or "untagged") for r in unmanaged[:5])
            raise StreamError(f"the X app has rules this tool does not manage; remove them first: {tags}")
        wanted = {(rule.tag, rule.value) for rule in expected}
        current = {(str(r.get("tag") or ""), str(r.get("value") or "")) for r in existing}
        if current == wanted and len(existing) == len(expected):
            return expected
        ids = [str(rule["id"]) for rule in existing if rule.get("id")]
        if ids:
            await self._json("POST", X_RULES_URL, json={"delete": {"ids": ids}})
        await self._json("POST", X_RULES_URL, json={"add": [{"value": r.value, "tag": r.tag} for r in expected]})
        installed = await self.list_rules()
        if {(str(r.get("tag") or ""), str(r.get("value") or "")) for r in installed} != wanted or len(
            installed
        ) != len(expected):
            raise StreamError("X rule verification failed after the update")
        return expected

    async def end_stale_connections(self) -> int:
        """End stream connections X still counts for this app. Call only while
        this process holds no stream: one X never noticed closing can answer
        every reconnect with the connection-limit 429 for days."""
        listed = await self._json(
            "GET",
            X_CONNECTIONS_URL,
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
        ended = await self._json("DELETE", X_CONNECTIONS_URL, json={"uuids": ids})
        return int((ended.get("data") or {}).get("successful_kills") or 0)

    async def events(self) -> AsyncIterator[dict]:
        """Yield each matching post as X pushes it."""
        try:
            async with self.client.stream("GET", X_STREAM_URL, headers=self.headers, params=STREAM_PARAMS) as response:
                if response.status_code != 200:
                    raise _error(
                        response.status_code,
                        _detail(await response.aread()),
                        "stream",
                        float(response.headers.get("x-rate-limit-reset") or 0),
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue  # keep-alive
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = parse_stream_event(payload)
                    if event is not None:
                        yield event
        except httpx.TransportError as exc:
            raise StreamError("X stream transport interrupted") from exc
