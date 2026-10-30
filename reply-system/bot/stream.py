"""Fail-closed TwitterAPI.io rule management and tweet ingestion.

TwitterAPI.io bills when a rule is active, before local deduplication. Rule
management is therefore a money-moving boundary: every mutation is verified,
partial changes are rolled back, and websocket reconnects never mutate the paid
rule set.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import certifi
import httpx
import websockets

_SSL = ssl.create_default_context(cafile=certifi.where())
BASE = "https://api.twitterapi.io"
WS_URL = "wss://ws.twitterapi.io/twitter/tweet/websocket"
MAX_RULE_VALUE = 255
RULE_SUFFIX = " -is:reply -is:retweet"
INNER_BUDGET = MAX_RULE_VALUE - len(RULE_SUFFIX) - 2
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DEFAULT_RULE_NAMESPACE = "pentra-v1"


class ProviderError(RuntimeError):
    """TwitterAPI.io returned an invalid or unsuccessful response."""


class RuleSyncError(ProviderError):
    """The provider rule set could not be made exactly canonical."""


class StreamDisconnected(RuntimeError):
    """A websocket connection ended and may be re-established in-process."""


class StreamIntegrityError(RuntimeError):
    """The provider delivered data outside the rules owned by this daemon."""


@dataclass(frozen=True)
class RuleSpec:
    tag: str
    value: str
    interval_seconds: float


@dataclass(frozen=True)
class RuleRecord:
    rule_id: str
    tag: str
    value: str
    interval_seconds: float
    is_effect: int | None = None

    @classmethod
    def from_json(cls, raw: dict) -> "RuleRecord":
        rid = str(raw.get("rule_id") or raw.get("id") or "")
        if not rid:
            raise ProviderError(f"rule without rule_id: {raw!r}")
        active = raw.get("is_effect", raw.get("isEffect", raw.get("active")))
        if isinstance(active, bool):
            active = int(active)
        elif active is not None:
            active = int(active)
        return cls(
            rule_id=rid,
            tag=str(raw.get("tag") or ""),
            value=str(raw.get("value") or ""),
            interval_seconds=float(raw.get("interval_seconds") or 0),
            is_effect=active,
        )

    @property
    def spec(self) -> RuleSpec:
        return RuleSpec(self.tag, self.value, self.interval_seconds)


@dataclass(frozen=True)
class MonitoredUser:
    id_for_user: str
    user_name: str

    @classmethod
    def from_json(cls, raw: dict) -> "MonitoredUser":
        identifier = str(raw.get("id_for_user") or "")
        if not identifier:
            raise ProviderError(f"monitored user without id_for_user: {raw!r}")
        return cls(
            id_for_user=identifier,
            user_name=str(
                raw.get("x_user_name") or raw.get("x_user_screen_name") or ""
            ),
        )


def normalize_handles(handles: list[str]) -> list[str]:
    """Validate and deduplicate handles without changing their configured order."""
    clean: list[str] = []
    seen: set[str] = set()
    for raw in handles:
        handle = str(raw).strip().lstrip("@")
        if not HANDLE_RE.fullmatch(handle):
            raise ValueError(f"invalid X handle: {raw!r}")
        key = handle.lower()
        if key not in seen:
            seen.add(key)
            clean.append(handle)
    return clean


def build_rule_values(handles: list[str]) -> list[str]:
    """Pack unique handles into originals-only rules under the 255-char limit."""
    groups: list[str] = []
    cur = ""
    for handle in normalize_handles(handles):
        clause = f"from:{handle}"
        candidate = clause if not cur else f"{cur} OR {clause}"
        if len(candidate) > INNER_BUDGET:
            if not cur:
                raise ValueError(f"handle cannot fit in a filter rule: {handle!r}")
            groups.append(cur)
            cur = clause
        else:
            cur = candidate
    if cur:
        groups.append(cur)
    return [f"({group}){RULE_SUFFIX}" for group in groups]


def build_rule_specs(
    handles: list[str],
    interval_seconds: float = 5,
    namespace: str = DEFAULT_RULE_NAMESPACE,
) -> list[RuleSpec]:
    values = build_rule_values(handles)
    fingerprint = hashlib.sha256("\n".join(values).encode()).hexdigest()[:10]
    return [
        RuleSpec(f"{namespace}-{fingerprint}-{i:02d}", value, float(interval_seconds))
        for i, value in enumerate(values)
    ]


class TwitterApi:
    """Strict, testable wrapper around the metered provider control plane."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ProviderError("TWITTERAPI_IO_KEY is missing")
        self.api_key = api_key
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "TwitterApi":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self

    async def __aexit__(self, *_exc):
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        if self._client is None:
            raise RuntimeError("TwitterApi must be used as an async context manager")
        response = await self._client.request(
            method,
            f"{BASE}{path}",
            headers={"x-api-key": self.api_key},
            json=json_body,
        )
        try:
            body = response.json()
        except Exception as exc:
            raise ProviderError(
                f"{method} {path} returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                f"{method} {path} failed HTTP {response.status_code}: "
                f"{body.get('message') or body.get('msg') or 'unknown error'}"
            )
        if body.get("status") == "error" or body.get("error"):
            raise ProviderError(
                f"{method} {path} failed: "
                f"{body.get('message') or body.get('msg') or body.get('error')}"
            )
        return body

    async def get_rules(self) -> list[RuleRecord]:
        body = await self._request("GET", "/oapi/tweet_filter/get_rules")
        rules = body.get("rules")
        if not isinstance(rules, list):
            raise ProviderError("get_rules response omitted the rules array")
        return [RuleRecord.from_json(rule) for rule in rules]

    async def add_rule(self, spec: RuleSpec) -> RuleRecord:
        body = await self._request(
            "POST",
            "/oapi/tweet_filter/add_rule",
            json_body={
                "tag": spec.tag,
                "value": spec.value,
                "interval_seconds": spec.interval_seconds,
            },
        )
        if body.get("status") not in (None, "success"):
            raise ProviderError(f"add_rule did not succeed: {body!r}")
        rid = str(body.get("rule_id") or "")
        if not rid:
            raise ProviderError("add_rule response omitted rule_id")
        return RuleRecord(rid, spec.tag, spec.value, spec.interval_seconds, 0)

    async def set_rule_active(self, rule: RuleRecord, active: bool) -> None:
        body = await self._request(
            "POST",
            "/oapi/tweet_filter/update_rule",
            json_body={
                "rule_id": rule.rule_id,
                "tag": rule.tag,
                "value": rule.value,
                "interval_seconds": rule.interval_seconds,
                "is_effect": int(active),
            },
        )
        if body.get("status") != "success":
            raise ProviderError(f"update_rule did not confirm success: {body!r}")

    async def delete_rule(self, rule_id: str) -> None:
        body = await self._request(
            "DELETE",
            "/oapi/tweet_filter/delete_rule",
            json_body={"rule_id": rule_id},
        )
        if body.get("status") != "success":
            raise ProviderError(f"delete_rule did not confirm success: {body!r}")

    async def get_balance_credits(self) -> int:
        body = await self._request("GET", "/oapi/my/info")
        try:
            return int(body["recharge_credits"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("account response omitted recharge_credits") from exc

    async def get_monitored_users(self) -> list[MonitoredUser]:
        body = await self._request(
            "GET", "/oapi/x_user_stream/get_user_to_monitor_tweet"
        )
        users = body.get("data")
        if not isinstance(users, list):
            raise ProviderError("monitored-user response omitted the data array")
        return [MonitoredUser.from_json(user) for user in users]

    async def remove_monitored_user(self, id_for_user: str) -> None:
        body = await self._request(
            "POST",
            "/oapi/x_user_stream/remove_user_to_monitor_tweet",
            json_body={"id_for_user": id_for_user},
        )
        if body.get("status") != "success":
            raise ProviderError(
                f"remove monitored user did not confirm success: {body!r}"
            )

    async def assert_no_monitored_users(self) -> None:
        """Reject the provider's separate, unscoped Stream subscription lane."""
        users = await self.get_monitored_users()
        if users:
            names = ", ".join(user.user_name or user.id_for_user for user in users)
            raise RuleSyncError(
                "provider has separate monitored-user subscriptions; "
                f"remove them before enabling custom rules: {names}"
            )


def _specs_match(records: list[RuleRecord], expected: list[RuleSpec]) -> bool:
    key = lambda spec: (spec.tag, spec.value, float(spec.interval_seconds))
    return (
        len(records) == len(expected)
        and sorted((r.spec for r in records), key=key) == sorted(expected, key=key)
    )


async def clear_all_rules_with_api(api: TwitterApi, *, max_sweeps: int = 4) -> int:
    """Deactivate, delete, and verify every rule. Any residue is a hard error."""
    deleted_ids: set[str] = set()
    last_error: Exception | None = None
    for _ in range(max_sweeps):
        rules = await api.get_rules()
        if not rules:
            return len(deleted_ids)

        # Stop billing first. Continue to deletion even if one deactivate call fails.
        for rule in rules:
            try:
                await api.set_rule_active(rule, False)
            except Exception as exc:
                last_error = exc

        for rule in rules:
            try:
                await api.delete_rule(rule.rule_id)
                deleted_ids.add(rule.rule_id)
            except Exception as exc:
                last_error = exc

    residue = await api.get_rules()
    if residue:
        ids = ", ".join(rule.rule_id for rule in residue)
        detail = f"; last provider error: {last_error}" if last_error else ""
        raise RuleSyncError(f"failed to clear provider rules; residue={ids}{detail}")
    return len(deleted_ids)


async def clear_all_rules(api_key: str) -> int:
    async with TwitterApi(api_key) as api:
        return await clear_all_rules_with_api(api)


async def deactivate_all_rules_with_api(api: TwitterApi) -> int:
    """Stop billing while preserving the canonical rules for the next start."""
    rules = await api.get_rules()
    changed = 0
    for rule in rules:
        if rule.is_effect != 0:
            await api.set_rule_active(rule, False)
            changed += 1
    verified = await api.get_rules()
    if len(verified) != len(rules) or any(rule.is_effect != 0 for rule in verified):
        raise RuleSyncError("provider rules could not be verified inactive")
    return changed


async def stage_rules(
    api: TwitterApi,
    handles: list[str],
    *,
    interval_seconds: float = 5,
    namespace: str = DEFAULT_RULE_NAMESPACE,
) -> list[RuleRecord]:
    """Install exactly one canonical rule set and leave every rule inactive."""
    expected = build_rule_specs(handles, interval_seconds, namespace)
    existing = await api.get_rules()

    if _specs_match(existing, expected):
        await deactivate_all_rules_with_api(api)
        staged = await api.get_rules()
        if not _specs_match(staged, expected):
            raise RuleSyncError("reused provider rules changed while staging")
        return staged

    if existing:
        await clear_all_rules_with_api(api)
    if await api.get_rules():
        raise RuleSyncError("provider rule set was not empty before staging")

    try:
        for spec in expected:
            await api.add_rule(spec)
        staged = await api.get_rules()
        if not _specs_match(staged, expected):
            raise RuleSyncError("inactive staged rules did not exactly match canonical rules")
        if any(rule.is_effect != 0 for rule in staged):
            raise RuleSyncError("a newly staged provider rule was unexpectedly active")
        return staged
    except Exception as exc:
        try:
            await clear_all_rules_with_api(api)
        except Exception as rollback_exc:
            raise RuleSyncError(
                f"rule staging failed ({exc}); rollback also failed ({rollback_exc})"
            ) from rollback_exc
        raise RuleSyncError(f"rule staging failed and was rolled back: {exc}") from exc


async def activate_staged_rules(
    api: TwitterApi,
    staged: list[RuleRecord],
) -> list[RuleRecord]:
    """Activate a previously verified canonical set after the listener is ready."""
    current = await api.get_rules()
    expected = [rule.spec for rule in staged]
    if not _specs_match(current, expected):
        raise RuleSyncError("provider rules changed between staging and activation")
    try:
        for rule in current:
            if rule.is_effect != 1:
                await api.set_rule_active(rule, True)
        verified = await api.get_rules()
        if not _specs_match(verified, expected):
            raise RuleSyncError("provider rules changed during activation")
        if any(rule.is_effect != 1 for rule in verified):
            raise RuleSyncError("a canonical provider rule remained inactive")
        return verified
    except Exception as exc:
        try:
            await deactivate_all_rules_with_api(api)
        except Exception as rollback_exc:
            raise RuleSyncError(
                f"rule activation failed ({exc}); deactivation also failed ({rollback_exc})"
            ) from rollback_exc
        raise RuleSyncError(f"rule activation failed and was deactivated: {exc}") from exc


async def reconcile_rules(
    api: TwitterApi,
    handles: list[str],
    *,
    interval_seconds: float = 5,
    namespace: str = DEFAULT_RULE_NAMESPACE,
) -> list[RuleRecord]:
    """Compatibility helper: stage canonically, then activate."""
    staged = await stage_rules(
        api,
        handles,
        interval_seconds=interval_seconds,
        namespace=namespace,
    )
    return await activate_staged_rules(api, staged)


async def sync_rules(
    api_key: str,
    handles: list[str],
    interval_seconds: float = 5,
) -> list[tuple[str, str]]:
    """Compatibility wrapper for callers outside the daemon."""
    async with TwitterApi(api_key) as api:
        records = await reconcile_rules(api, handles, interval_seconds=interval_seconds)
        return [(record.rule_id, record.value) for record in records]


@asynccontextmanager
async def open_tweet_websocket(api_key: str):
    """Open and verify a websocket before it is allowed to consume deliveries."""
    ready = False
    try:
        async with websockets.connect(
            WS_URL,
            additional_headers={"x-api-key": api_key},
            ping_interval=40,
            ping_timeout=30,
            open_timeout=20,
            close_timeout=10,
            ssl=_SSL,
        ) as ws:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = message.get("event_type") or message.get("type")
                if kind == "ping":
                    continue
                if kind != "connected":
                    raise StreamIntegrityError(
                        f"unexpected event before websocket readiness: {kind!r}"
                    )
                break
            ready = True
            yield ws
    except (StreamIntegrityError, asyncio.CancelledError):
        raise
    except Exception as exc:
        if ready:
            raise
        raise StreamDisconnected(
            f"websocket setup ended: {type(exc).__name__}: {exc}"
        ) from exc


async def iter_owned_tweets(
    ws,
    *,
    expected_rule_ids: set[str],
    expected_rule_tags: set[str],
) -> AsyncIterator[dict]:
    """Yield only events belonging to the active canonical rule set."""
    try:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            kind = message.get("event_type") or message.get("type")
            if kind in ("connected", "ping"):
                continue
            if kind not in ("tweet", None):
                raise StreamIntegrityError(f"unexpected websocket event type: {kind!r}")

            rule_id = str(message.get("rule_id") or "")
            rule_tag = str(message.get("rule_tag") or "")
            if rule_id not in expected_rule_ids or rule_tag not in expected_rule_tags:
                raise StreamIntegrityError(
                    f"unowned delivery rule_id={rule_id!r} rule_tag={rule_tag!r}"
                )

            tweets = message.get("tweets") or (
                [message.get("tweet")] if message.get("tweet") else []
            )
            for raw_tweet in tweets:
                if not raw_tweet:
                    continue
                tweet = dict(raw_tweet)
                tweet["_twitterapi_rule_id"] = rule_id
                tweet["_twitterapi_rule_tag"] = rule_tag
                tweet["_twitterapi_delivered_at_ms"] = message.get("timestamp")
                yield tweet
    except (StreamIntegrityError, asyncio.CancelledError):
        raise
    except Exception as exc:
        raise StreamDisconnected(f"websocket ended: {type(exc).__name__}: {exc}") from exc
    raise StreamDisconnected("websocket closed normally")


async def stream_tweets(
    api_key: str,
    *,
    expected_rule_ids: set[str],
    expected_rule_tags: set[str],
) -> AsyncIterator[dict]:
    """Compatibility wrapper for a pre-existing active rule set."""
    async with open_tweet_websocket(api_key) as ws:
        async for tweet in iter_owned_tweets(
            ws,
            expected_rule_ids=expected_rule_ids,
            expected_rule_tags=expected_rule_tags,
        ):
            yield tweet
