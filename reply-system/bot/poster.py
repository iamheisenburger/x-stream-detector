"""Publish through X's official API with OAuth 1.0a user authentication.

The production boundary deliberately does not drive x.com or consume browser
cookies. A network ambiguity is fatal and is never retried automatically,
because a retry could create a duplicate public post.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import date
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .config import Config
from .state import State

X_CREATE_URL = "https://api.x.com/2/tweets"
X_ME_URL = "https://api.x.com/2/users/me"
# Oct 23 2026: a file name ("robots.txt", "sitemap.xml", "llms.txt") ends in no
# domain X links, so it is not priced as a link post; SEO posts need those names.
_URL_LIKE_RE = re.compile(
    r"(?i)(?:https?://\S+|www\.\S+|"
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?!(?:txt|xml|json|html?|php|csv|pdf|js|css|png|jpe?g|gif|svg|webp)\b)[a-z]{2,63}(?:/\S*)?)"
)


class PublishError(RuntimeError):
    """The official publishing boundary failed or returned an ambiguous result."""


class PublishBudgetOpen(PublishError):
    """A configured official-X write budget or pricing guard was breached."""


def _encode(value: object) -> str:
    return quote(str(value), safe="~-._")


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path or "/", "", ""))


def oauth1_authorization(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """Build an RFC 5849 HMAC-SHA1 Authorization header for a JSON request."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time()) if timestamp is None else int(timestamp)),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    query = []
    raw_query = urlsplit(url).query
    if raw_query:
        from urllib.parse import parse_qsl

        query = parse_qsl(raw_query, keep_blank_values=True)
    params = sorted(
        [(_encode(key), _encode(value)) for key, value in [*query, *oauth.items()]]
    )
    normalized = "&".join(f"{key}={value}" for key, value in params)
    signature_base = "&".join(
        (_encode(method.upper()), _encode(_base_url(url)), _encode(normalized))
    )
    signing_key = f"{_encode(consumer_secret)}&{_encode(access_token_secret)}"
    digest = hmac.new(
        signing_key.encode(),
        signature_base.encode(),
        hashlib.sha1,
    ).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(
        f'{_encode(key)}="{_encode(value)}"' for key, value in sorted(oauth.items())
    )


def _credentials(cfg: Config) -> tuple[str, str, str, str]:
    values = (
        cfg.x_api_key,
        cfg.x_api_secret,
        cfg.x_access_token,
        cfg.x_access_token_secret,
    )
    if not all(values):
        raise PublishError(
            "official X API credentials are incomplete; expected X_API_KEY, "
            "X_API_SECRET, X_ACCESS_TOKEN, and X_ACCESS_TOKEN_SECRET"
        )
    return values


def _check_pricing(cfg: Config) -> None:
    try:
        valid_through = date.fromisoformat(cfg.publishing.pricing_valid_through)
    except ValueError as exc:
        raise PublishBudgetOpen("invalid official X pricing review date") from exc
    if date.today() > valid_through:
        raise PublishBudgetOpen(
            "official X write pricing must be reviewed before publishing resumes"
        )


def _create_cost_microusd(cfg: Config, text: str) -> int:
    if _URL_LIKE_RE.search(text):
        return cfg.publishing.create_with_url_microusd
    return cfg.publishing.create_microusd


def _preflight_budget(cfg: Config, state: State, reserve: int) -> None:
    if not cfg.publishing.enabled:
        raise PublishBudgetOpen("official X publishing is disabled")
    _check_pricing(cfg)
    if state.publisher_cost_microusd_today() + reserve > cfg.publishing.daily_microusd_cap:
        raise PublishBudgetOpen("daily official X write budget exhausted")
    if state.publisher_cost_microusd_month() + reserve > cfg.publishing.monthly_microusd_cap:
        raise PublishBudgetOpen("monthly official X write budget exhausted")


async def _request(
    cfg: Config,
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    key, secret, token, token_secret = _credentials(cfg)
    header = oauth1_authorization(
        method,
        url,
        consumer_key=key,
        consumer_secret=secret,
        access_token=token,
        access_token_secret=token_secret,
    )
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15)
    try:
        return await client.request(
            method,
            url,
            headers={
                "Authorization": header,
                "Content-Type": "application/json",
                "User-Agent": "Pentra-X/1.0",
            },
            json=json_body,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise PublishError(
            "official X request outcome is ambiguous; refusing automatic retry"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()


async def verify_account(cfg: Config, *, client: httpx.AsyncClient | None = None) -> bool:
    """Verify the configured user token once before paid detection activates."""
    if not cfg.publishing.enabled:
        return False
    response = await _request(cfg, "GET", X_ME_URL, client=client)
    if response.status_code != 200:
        return False
    data = response.json().get("data", {}) or {}
    expected = str(getattr(cfg.publishing, "expected_username", "") or "").lstrip("@").lower()
    # A token generated while signed in to a different X account must never post.
    if expected and str(data.get("username") or "").lower() != expected:
        return False
    return bool(data.get("id"))


async def _publish(
    cfg: Config,
    state: State,
    text: str,
    *,
    reply_to: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    if cfg.dry_run:
        label = f"REPLY to {reply_to}" if reply_to else "POST"
        print(f"[DRY_RUN] would {label}: {text!r}")
        return True
    if reply_to and cfg.publishing.require_ai_reply_approval:
        if not cfg.x_ai_reply_approval_reference:
            raise PublishError(
                "AI reply approval reference is missing; automated replies remain disabled"
            )
    create_cost_microusd = _create_cost_microusd(cfg, text)
    _preflight_budget(cfg, state, create_cost_microusd)
    payload: dict = {"text": text}
    endpoint = "create_reply" if reply_to else "create_post"
    intent_key = (
        f"reply:{reply_to}"
        if reply_to
        else f"post:{hashlib.sha256(text.encode()).hexdigest()}"
    )
    if not state.reserve_publisher_intent(
        intent_key=intent_key,
        endpoint=endpoint,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
    ):
        raise PublishError(
            f"public action was already attempted; refusing duplicate: {intent_key}"
        )
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    try:
        response = await _request(
            cfg,
            "POST",
            X_CREATE_URL,
            json_body=payload,
            client=client,
        )
    except PublishError:
        # A transport failure is deliberately left pending because the remote
        # outcome is ambiguous. A future run must not retry it automatically.
        raise
    try:
        body = response.json()
    except Exception as exc:
        state.mark_publisher_intent_failed(intent_key=intent_key)
        raise PublishError(
            f"official X create returned non-JSON HTTP {response.status_code}"
        ) from exc
    post_id = str(body.get("data", {}).get("id") or "")
    if response.status_code not in (200, 201) or not post_id:
        state.mark_publisher_intent_failed(intent_key=intent_key)
        detail = body.get("detail") or body.get("title") or body.get("errors") or body
        raise PublishError(
            f"official X create failed HTTP {response.status_code}: {detail}"
        )
    state.complete_publisher_intent(
        intent_key=intent_key,
        endpoint=endpoint,
        remote_post_id=post_id,
        cost_microusd=create_cost_microusd,
    )
    return True


async def post(
    cfg: Config,
    state: State,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    return await _publish(cfg, state, text, client=client)


async def reply(
    cfg: Config,
    state: State,
    tweet_id: str,
    author: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    if cfg.reply_consent_for(author) is None:
        raise PublishError(
            f"no auditable automated-reply consent for @{author.lstrip('@')}"
        )
    return await _publish(cfg, state, text, reply_to=tweet_id, client=client)
