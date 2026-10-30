"""Failure-mode tests for the official X publishing boundary."""
import asyncio
import os
import tempfile

import httpx
import pytest

from bot.config import Config, Pacing, PublishingBudget
from bot.poster import PublishBudgetOpen, PublishError, oauth1_authorization, post, reply
from bot.state import State


def state():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return State(path)


def cfg(*, approval: str = "x-approval-reference") -> Config:
    return Config(
        env={
            "X_API_KEY": "consumer-key",
            "X_API_SECRET": "consumer-secret",
            "X_ACCESS_TOKEN": "access-token",
            "X_ACCESS_TOKEN_SECRET": "access-secret",
            "X_AI_REPLY_APPROVAL_REFERENCE": approval,
        },
        targets=[],
        pacing=Pacing(),
        dry_run=False,
        model="unused",
        started_at_iso="",
        tier_weight={},
        stream_interval_seconds=5,
        publishing=PublishingBudget(
            enabled=True,
            pricing_valid_through="2999-12-31",
        ),
        reply_consents={
            "alice": {
                "status": "active",
                "scope": "automated_ai_replies",
                "granted_at": "2026-07-01T00:00:00Z",
                "evidence_reference": "https://x.com/alice/status/1",
            }
        },
    )


def run(coro):
    return asyncio.run(coro)


def test_oauth_header_is_deterministic_for_fixed_nonce_and_time():
    kwargs = {
        "consumer_key": "key",
        "consumer_secret": "secret",
        "access_token": "token",
        "access_token_secret": "token-secret",
        "nonce": "fixed-nonce",
        "timestamp": 1_700_000_000,
    }
    first = oauth1_authorization("POST", "https://api.x.com/2/tweets", **kwargs)
    second = oauth1_authorization("POST", "https://api.x.com/2/tweets", **kwargs)
    assert first == second
    assert 'oauth_nonce="fixed-nonce"' in first
    assert "oauth_signature=" in first


def test_successful_reply_records_payload_budget_and_idempotency():
    st = state()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["authorization"].startswith("OAuth ")
        assert request.read().decode() == (
            '{"text":"useful reply","reply":{"in_reply_to_tweet_id":"source-1"}}'
        )
        return httpx.Response(201, json={"data": {"id": "remote-1"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert run(reply(cfg(), st, "source-1", "alice", "useful reply", client=client))
    finally:
        run(client.aclose())

    assert st.publisher_cost_microusd_today() == 15_000
    assert st.publisher_intent_status("reply:source-1") == "succeeded"


def test_missing_ai_reply_approval_fails_before_network():
    st = state()
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"data": {"id": "should-not-exist"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PublishError, match="approval reference"):
            run(
                reply(
                    cfg(approval=""),
                    st,
                    "source-1",
                    "alice",
                    "useful reply",
                    client=client,
                )
            )
    finally:
        run(client.aclose())
    assert not called


def test_missing_recipient_consent_fails_before_network():
    st = state()
    no_consent = cfg()
    no_consent.reply_consents = {}
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"data": {"id": "should-not-exist"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PublishError, match="no auditable"):
            run(
                reply(
                    no_consent,
                    st,
                    "source-1",
                    "alice",
                    "useful reply",
                    client=client,
                )
            )
    finally:
        run(client.aclose())
    assert not called


def test_publisher_budget_fails_before_network():
    st = state()
    limited = cfg()
    limited.publishing.daily_microusd_cap = 0

    with pytest.raises(PublishBudgetOpen, match="daily"):
        run(reply(limited, st, "source-1", "alice", "useful reply"))
    assert st.publisher_intent_status("reply:source-1") is None


def test_url_post_uses_url_rate_in_budget_and_ledger():
    st = state()
    url_cfg = cfg()
    url_cfg.publishing.daily_microusd_cap = 200_000
    text = "Read the guide at https://pentra.dev/blog"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"data": {"id": "remote-url-1"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert run(post(url_cfg, st, text, client=client))
    finally:
        run(client.aclose())

    assert st.publisher_cost_microusd_today() == 200_000


def test_url_post_budget_fails_before_network():
    st = state()
    limited = cfg()
    limited.publishing.daily_microusd_cap = 199_999
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"data": {"id": "should-not-exist"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PublishBudgetOpen, match="daily"):
            run(
                post(
                    limited,
                    st,
                    "Read pentra.dev for details",
                    client=client,
                )
            )
    finally:
        run(client.aclose())
    assert not called


def test_ambiguous_transport_failure_is_never_retried():
    st = state()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("outcome unknown", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PublishError, match="ambiguous"):
            run(reply(cfg(), st, "source-1", "alice", "useful reply", client=client))
        assert st.publisher_intent_status("reply:source-1") == "pending"
        with pytest.raises(PublishError, match="already attempted"):
            run(reply(cfg(), st, "source-1", "alice", "useful reply", client=client))
    finally:
        run(client.aclose())
    assert calls == 1


def test_verify_account_refuses_a_token_for_a_different_x_account():
    import asyncio
    import httpx
    from types import SimpleNamespace
    from bot import poster

    def make_cfg(expected):
        return SimpleNamespace(
            publishing=SimpleNamespace(enabled=True, expected_username=expected),
            x_api_key="k", x_api_secret="s", x_access_token="t", x_access_token_secret="ts",
        )

    async def run(expected, username):
        async def handler(request):
            return httpx.Response(200, json={"data": {"id": "1", "username": username}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await poster.verify_account(make_cfg(expected), client=client)
        finally:
            await client.aclose()

    assert asyncio.run(run("pentradev", "PentraDev")) is True
    assert asyncio.run(run("pentradev", "someone_else")) is False
