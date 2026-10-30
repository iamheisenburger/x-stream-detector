import asyncio
import contextlib
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.config import Config, Pacing, XDetectionBudget
from bot.state import State
from bot.x_read_budget import XReadBudget, XReadBudgetOpen
from bot.x_stream import (
    MAX_RULE_VALUE,
    OfficialXStream,
    XCreditsDepleted,
    XRateLimitError,
    XStreamError,
    build_reply_rule_values,
    build_rule_values,
    canonical_rule_specs,
    parse_stream_event,
)


def make_config(handles: list[str]) -> Config:
    return Config(
        env={"X_API_KEY": "key", "X_API_SECRET": "secret"},
        targets=[{"handle": handle, "tier": "A"} for handle in handles],
        pacing=Pacing(),
        dry_run=False,
        model="test",
        started_at_iso="2026-07-17T00:00:00+00:00",
        tier_weight={"A": 1.0},
        stream_interval_seconds=5,
        x_detection=XDetectionBudget(
            enabled=True,
            pricing_valid_through="2099-12-31",
        ),
    )


def make_state() -> State:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return State(path)


def test_one_hundred_targets_are_packed_into_canonical_original_rules():
    handles = [f"Account{i}" for i in range(100)]
    values = build_rule_values(handles)
    assert len(values) < 9
    assert all(len(value) <= MAX_RULE_VALUE for value in values)
    assert sum(value.count("from:") for value in values) == 100
    assert all("-is:reply -is:retweet lang:en" in value for value in values)


def test_two_hundred_targets_fit_canonical_original_rules():
    handles = [f"Account{i}" for i in range(200)]
    values = build_rule_values(handles)
    assert len(values) <= 12
    assert all(len(value) <= MAX_RULE_VALUE for value in values)
    assert sum(value.count("from:") for value in values) == 200
    assert all("-is:reply -is:retweet lang:en" in value for value in values)


def test_stream_rules_preserve_all_originals_from_qualified_authors():
    value = build_rule_values(["FounderAccount"])[0]
    assert "from:founderaccount" in value
    assert "-is:reply -is:retweet lang:en" in value
    assert "AI OR" not in value


def test_self_contained_reply_rules_share_the_canonical_rule_set():
    reply_values = build_reply_rule_values(
        [f"Builder{i}" for i in range(35)],
        ["AI", "software", "product", "open source"],
    )
    assert reply_values
    assert all(len(value) <= MAX_RULE_VALUE for value in reply_values)
    assert sum(value.count("from:") for value in reply_values) == 35
    assert all("is:reply -is:retweet lang:en" in value for value in reply_values)
    assert all('"open source"' in value for value in reply_values)


def test_reply_rules_can_capture_every_reply_from_the_curated_subset():
    reply_values = build_reply_rule_values(
        [f"Builder{i}" for i in range(35)],
        [],
    )
    assert reply_values
    assert all(len(value) <= MAX_RULE_VALUE for value in reply_values)
    assert sum(value.count("from:") for value in reply_values) == 35
    assert all("is:reply -is:retweet lang:en" in value for value in reply_values)


def test_canonical_specs_append_reply_lane_without_another_stream():
    specs = canonical_rule_specs(
        ["OriginalA", "OriginalB"],
        "pentra",
        reply_handles=["OriginalA"],
        reply_keywords=["software", "product"],
    )
    assert len(specs) == 2
    assert "-is:reply" in specs[0].value
    assert "is:reply" in specs[1].value
    assert [spec.tag for spec in specs] == ["pentra-001", "pentra-002"]


def test_dynamic_discovery_rules_append_to_same_canonical_stream():
    discovery_rule = (
        '("AI agent" OR SaaS) followers_count:250000..2147483647 '
        '-is:retweet -is:reply lang:en'
    )
    specs = canonical_rule_specs(
        ["OriginalA"],
        "pentra",
        discovery_rules=[discovery_rule],
    )
    assert len(specs) == 2
    assert specs[1].value == discovery_rule
    assert specs[1].tag == "pentra-discovery-001"


def test_stream_payload_normalizes_for_existing_skip_gate():
    created = "2026-07-17T15:30:00.000Z"
    event = parse_stream_event(
        {
            "data": {
                "id": "123",
                "text": "A useful original post about turning website traffic into revenue.",
                "author_id": "42",
                "created_at": created,
                "lang": "en",
            },
            "includes": {"users": [{"id": "42", "username": "PentraTarget"}]},
            "matching_rules": [{"id": "1", "tag": "pentra-replies-v1-001"}],
        }
    )
    assert event is not None
    assert event["dynamic_discovery_match"] is False
    assert event["author"]["id"] == "42"
    assert event["id"] == "123"
    assert event["author"]["username"] == "PentraTarget"
    assert not event["is_reply"]
    assert not event["is_retweet"]
    expected = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    assert event["created_at_epoch"] == expected


def test_stream_payload_prefers_complete_note_tweet_text():
    event = parse_stream_event(
        {
            "data": {
                "id": "long-123",
                "text": "This long product post is truncated mid-sentence…",
                "note_tweet": {
                    "text": (
                        "This long product post contains the full workflow, "
                        "the intended audience, and the actual tradeoff."
                    )
                },
                "author_id": "42",
                "created_at": "2026-07-17T15:30:00.000Z",
                "lang": "en",
            },
            "includes": {"users": [{"id": "42", "username": "Builder"}]},
        }
    )
    assert event is not None
    assert event["text"].endswith("the actual tradeoff.")
    assert "truncated" not in event["text"]


def test_stream_payload_preserves_x_supplied_link_and_media_context():
    event = parse_stream_event(
        {
            "data": {
                "id": "context-123",
                "text": "https://t.co/example",
                "author_id": "42",
                "created_at": "2026-07-17T15:30:00.000Z",
                "lang": "en",
                "entities": {"urls": [{
                    "expanded_url": "https://example.com/launch",
                    "title": "A new agent review workflow",
                    "description": "Operators inspect flagged actions before deployment.",
                }]},
                "attachments": {"media_keys": ["3_123"]},
            },
            "includes": {
                "users": [{"id": "42", "username": "Builder"}],
                "media": [{
                    "media_key": "3_123",
                    "type": "photo",
                    "alt_text": "A dashboard separating queued and approved actions.",
                }],
            },
        }
    )
    assert event is not None
    assert event["link_context"] == [{
        "url": "https://example.com/launch",
        "title": "A new agent review workflow",
        "description": "Operators inspect flagged actions before deployment.",
    }]
    assert event["media_context"][0]["alt_text"].startswith("A dashboard")


def test_stream_payload_includes_replied_to_post_and_author_context():
    event = parse_stream_event(
        {
            "data": {
                "id": "reply-123",
                "text": "That rollout boundary is the interesting part.",
                "author_id": "42",
                "created_at": "2026-07-17T15:30:00.000Z",
                "lang": "en",
                "referenced_tweets": [
                    {"type": "replied_to", "id": "parent-456"}
                ],
            },
            "includes": {
                "users": [
                    {"id": "42", "username": "Builder"},
                    {"id": "84", "username": "ProductTeam"},
                ],
                "tweets": [
                    {
                        "id": "parent-456",
                        "author_id": "84",
                        "text": "truncated parent text",
                        "note_tweet": {
                            "text": (
                                "We are rolling the agent out to support teams "
                                "before opening the workflow to every department."
                            )
                        },
                    }
                ],
            },
        }
    )
    assert event is not None
    assert event["is_reply"]
    assert event["parent_id"] == "parent-456"
    assert event["parent_author"] == "ProductTeam"
    assert event["parent_text"].startswith("We are rolling")
    assert "truncated" not in event["parent_text"]


def test_stream_payload_includes_quoted_post_and_author_context():
    event = parse_stream_event(
        {
            "data": {
                "id": "quote-123",
                "text": "This changes the onboarding tradeoff.",
                "author_id": "42",
                "created_at": "2026-07-17T15:30:00.000Z",
                "lang": "en",
                "referenced_tweets": [
                    {"type": "quoted", "id": "quoted-456"}
                ],
            },
            "includes": {
                "users": [
                    {"id": "42", "username": "Builder"},
                    {"id": "84", "username": "ProductTeam"},
                ],
                "tweets": [
                    {
                        "id": "quoted-456",
                        "author_id": "84",
                        "text": "truncated quoted text",
                        "note_tweet": {
                            "text": (
                                "The product now imports an existing workflow "
                                "before asking the customer to configure it."
                            )
                        },
                    }
                ],
            },
        }
    )
    assert event is not None
    assert not event["is_reply"]
    assert event["quote_id"] == "quoted-456"
    assert event["quote_author"] == "ProductTeam"
    assert event["quote_text"].startswith("The product now imports")
    assert "truncated" not in event["quote_text"]


def test_filtered_stream_requests_parent_context_on_the_same_connection():
    payload = {
        "data": {
            "id": "reply-123",
            "text": "A useful reply.",
            "author_id": "42",
            "created_at": "2026-07-17T15:30:00.000Z",
            "lang": "en",
        },
        "includes": {"users": [{"id": "42", "username": "Builder"}]},
    }

    class OneEventResponse:
        status_code = 200

        async def aiter_lines(self):
            yield __import__("json").dumps(payload)

    class OneEventClient:
        def __init__(self):
            self.params = None

        @contextlib.asynccontextmanager
        async def stream(self, *_args, **kwargs):
            self.params = kwargs["params"]
            yield OneEventResponse()

    async def consume():
        cfg = make_config(["Builder"])
        client = OneEventClient()
        api = OfficialXStream(cfg, client=client)
        api._bearer = "test-bearer"
        events = [event async for event in api.events()]
        return client.params, events

    params, events = asyncio.run(consume())
    assert len(events) == 1
    assert set(params["expansions"].split(",")) == {
        "author_id",
        "referenced_tweets.id",
        "referenced_tweets.id.author_id",
        "attachments.media_keys",
    }
    assert "entities" in params["tweet.fields"].split(",")
    assert "attachments" in params["tweet.fields"].split(",")
    assert params["media.fields"] == "media_key,type,alt_text"


def test_official_read_ledger_deduplicates_within_utc_day_and_stops_at_cap():
    state = make_state()
    cfg = XDetectionBudget(
        enabled=True,
        daily_microusd_cap=10_000,
        monthly_microusd_cap=50_000,
        post_read_microusd=5_000,
        pricing_valid_through="2099-12-31",
    )
    budget = XReadBudget(cfg, state)
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc).timestamp()

    first = budget.record(post_id="1", author="alice", now=now)
    duplicate = budget.record(post_id="1", author="alice", now=now + 1)
    second = budget.record(post_id="2", author="bob", now=now + 2)

    assert first.charged
    assert not duplicate.charged
    assert second.charged and second.exhausted
    assert state.x_stream_cost_microusd_today(now) == 10_000
    with pytest.raises(XReadBudgetOpen):
        budget.assert_can_connect(now + 3)


def test_canonical_rule_check_is_idempotent():
    handles = [f"account{i}" for i in range(100)]
    cfg = make_config(handles)
    expected = canonical_rule_specs(handles, cfg.x_detection.rule_namespace)
    current = [
        {"id": str(index), "tag": rule.tag, "value": rule.value}
        for index, rule in enumerate(expected, start=1)
    ]
    api = OfficialXStream(cfg)
    api.list_rules = AsyncMock(return_value=current)
    api._json = AsyncMock()

    installed = asyncio.run(api.ensure_canonical_rules(handles))

    assert installed == expected
    api._json.assert_not_awaited()


def test_stream_transport_reset_becomes_retryable_x_stream_error():
    class InterruptedResponse:
        status_code = 200

        async def aiter_lines(self):
            raise httpx.RemoteProtocolError("incomplete chunked read")
            yield  # pragma: no cover

    class InterruptedClient:
        @contextlib.asynccontextmanager
        async def stream(self, *_args, **_kwargs):
            yield InterruptedResponse()

    async def consume():
        cfg = make_config(["account"])
        api = OfficialXStream(cfg, client=InterruptedClient())
        api._bearer = "test-bearer"
        async for _event in api.events():
            pass

    with pytest.raises(XStreamError, match="transport interrupted"):
        asyncio.run(consume())


def test_stream_credits_depleted_is_typed_and_preserves_provider_detail():
    class CreditsResponse:
        status_code = 402
        headers = {}

        async def aread(self):
            return b'{"title":"CreditsDepleted","detail":"no usable credits"}'

    class CreditsClient:
        @contextlib.asynccontextmanager
        async def stream(self, *_args, **_kwargs):
            yield CreditsResponse()

    async def consume():
        api = OfficialXStream(make_config(["account"]), client=CreditsClient())
        api._bearer = "test-bearer"
        async for _event in api.events():
            pass

    with pytest.raises(XCreditsDepleted, match="no usable credits"):
        asyncio.run(consume())


def test_stream_rate_limit_exposes_provider_reset_time():
    class RateLimitResponse:
        status_code = 429
        headers = {"x-rate-limit-reset": "1787140800"}

        async def aread(self):
            return b'{"detail":"Too Many Requests"}'

    class RateLimitClient:
        @contextlib.asynccontextmanager
        async def stream(self, *_args, **_kwargs):
            yield RateLimitResponse()

    async def consume():
        api = OfficialXStream(make_config(["account"]), client=RateLimitClient())
        api._bearer = "test-bearer"
        async for _event in api.events():
            pass

    with pytest.raises(XRateLimitError) as caught:
        asyncio.run(consume())
    assert caught.value.retry_at == 1787140800


def test_connection_limit_429_retries_in_seconds_not_after_the_rate_window():
    from bot.replies_daemon import reconnect_delay_after_interruption, stream_rate_limit_delay

    now = 1_790_000_000.0
    zombie = XRateLimitError(
        "official X stream failed HTTP 429: This stream is currently at the maximum allowed connection limit.",
        retry_at=now + 905,
    )
    assert zombie.connection_limit is True
    delays = [stream_rate_limit_delay(zombie, now=now, connection_limit_streak=n) for n in range(6)]
    assert delays == [30.0, 60.0, 120.0, 240.0, 480.0, 480.0]
    # Never later than X's own reset.
    soon = XRateLimitError("HTTP 429: maximum allowed connection limit", retry_at=now + 100)
    assert stream_rate_limit_delay(soon, now=now, connection_limit_streak=4) == 105.0
    # A genuine rate limit still waits for the reset.
    genuine = XRateLimitError("official X stream failed HTTP 429: Too Many Requests", retry_at=now + 905)
    assert genuine.connection_limit is False
    assert stream_rate_limit_delay(genuine, now=now, connection_limit_streak=0) == 910.0
    # A dropped transport waits long enough for X to release the old connection.
    assert reconnect_delay_after_interruption(XStreamError("official X stream transport interrupted"), 1) == 10.0
    assert reconnect_delay_after_interruption(XStreamError("official X stream transport interrupted"), 32) == 32.0
    assert reconnect_delay_after_interruption(XStreamError("canonical rule verification failed"), 2) == 2.0


def test_stale_stream_connections_are_listed_and_ended_for_this_app_only():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params), request.content.decode() or None))
        assert request.headers["authorization"] == "Bearer test-bearer"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": "ghost-1", "endpoint_name": "filtered_stream", "connected_at": "2026-10-01T10:25:27.000Z"},
                {"id": "other-2", "endpoint_name": "sample_stream"},
            ], "meta": {"result_count": 2}})
        return httpx.Response(200, json={"data": {"successful_kills": 1, "failed_kills": 0,
            "results": [{"uuid": "ghost-1", "success": True}]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = OfficialXStream(make_config(["account"]), client=client)
            api._bearer = "test-bearer"
            return await api.end_stale_stream_connections()

    assert asyncio.run(run()) == 1
    assert seen[0][:2] == ("GET", "/2/connections")
    assert seen[0][2]["status"] == "active" and seen[0][2]["endpoints"] == "filtered_stream"
    # Only the filtered-stream connection is ended, by its id.
    import json as _json
    assert seen[1][:2] == ("DELETE", "/2/connections") and _json.loads(seen[1][3]) == {"uuids": ["ghost-1"]}


def test_no_active_connection_means_nothing_is_ended():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = OfficialXStream(make_config(["account"]), client=client)
            api._bearer = "test-bearer"
            return await api.end_stale_stream_connections()

    assert asyncio.run(run()) == 0
    assert calls == ["GET"]


def test_stale_connection_check_runs_after_x_had_time_to_release_a_dropped_connection():
    from bot.replies_daemon import STALE_CONNECTION_CHECK_STREAK, stream_rate_limit_delay

    now = 1_790_000_000.0
    zombie = XRateLimitError("HTTP 429: This stream is currently at the maximum allowed connection limit.", retry_at=now + 905)
    waited = sum(stream_rate_limit_delay(zombie, now=now, connection_limit_streak=n) for n in range(STALE_CONNECTION_CHECK_STREAK))
    # X releases a just-dropped connection within about a minute; the check comes after 90 seconds, not hours.
    assert 60 <= waited <= 180


def test_measurement_reads_have_their_own_daily_lane():
    """Oct 19 2026: 132 measurement reads closed the reply stream for the day."""
    state = make_state()
    cfg = XDetectionBudget(
        enabled=True,
        daily_microusd_cap=10_000,
        measurement_daily_microusd_cap=10_000,
        monthly_microusd_cap=50_000,
        post_read_microusd=5_000,
        pricing_valid_through="2099-12-31",
        incident_daily_baseline_day="2026-10-19",
        incident_daily_baseline_microusd=5_000,
    )
    stream, measure = XReadBudget(cfg, state), XReadBudget(cfg, state, lane="measurement")
    now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc).timestamp()
    measure.record(post_id="m1", author="sama", now=now)
    measure.record(post_id="m2", author="@karpathy", now=now)
    with pytest.raises(XReadBudgetOpen, match="measurement"):
        measure.assert_can_connect(now)
    assert measure.snapshot(now).today_microusd == 10_000, "the incident baseline never applies to measurement"
    assert stream.assert_can_connect(now).today_microusd == 0, "measurement leaves the stream's daily room alone"
    stream.record(post_id="s1", author="alice", now=now)
    stream.record(post_id="s2", author="bob", now=now)
    assert stream.snapshot(now).today_microusd == 5_000, "the stream keeps its one-day baseline"
    assert stream.snapshot(now).month_microusd == 20_000, "the monthly cap counts every read"
    authors = {row[0] for row in state.db.execute("SELECT author FROM x_stream_reads")}
    assert authors == {"measure:sama", "measure:karpathy", "alice", "bob"}
    with pytest.raises(ValueError):
        XReadBudget(cfg, state, lane="other")


def test_reads_are_paced_over_the_shift_window():
    """Oct 2026: healthy days spent the 90-read cap by ~19:00 UTC and the rest
    of the 12:00-23:00 window had no drafts."""
    from bot.x_read_budget import read_pace_wait_seconds
    at = lambda h, m=0: datetime(2026, 10, 19, h, m, tzinfo=timezone.utc).timestamp()
    pace = dict(daily_reads=90, window=(12, 23), burst=10)
    assert read_pace_wait_seconds(now=at(12, 30), reads_used=9, **pace) == 0.0, "the burst runs ahead of an even pace"
    wait = read_pace_wait_seconds(now=at(12, 30), reads_used=20, **pace)
    assert 45 * 60 < wait < 55 * 60, wait  # 11 reads past the burst at ~8.2 reads/hour (80.7 min) minus 30 min elapsed
    assert read_pace_wait_seconds(now=at(18), reads_used=55, **pace) == 0.0, "on pace by evening"
    assert read_pace_wait_seconds(now=at(23, 30), reads_used=89, **pace) == 0.0, "outside the window nothing is paced"
    assert read_pace_wait_seconds(now=at(13), reads_used=80, daily_reads=90, window=None, burst=10) == 0.0
    # A window across midnight (22:00-06:00).
    assert read_pace_wait_seconds(now=at(1), reads_used=10, daily_reads=80, window=(22, 6), burst=5) == 0.0
    assert read_pace_wait_seconds(now=at(1), reads_used=60, daily_reads=80, window=(22, 6), burst=5) > 0
    assert read_pace_wait_seconds(now=at(13), reads_used=40, **pace) >= 60, "a pause is at least a minute"
