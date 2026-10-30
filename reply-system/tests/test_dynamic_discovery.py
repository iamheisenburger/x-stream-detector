import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from bot.config import Config, Pacing, XDetectionBudget
from bot.dynamic_discovery import DynamicAuthorVerifier, measure_author
from bot.state import State


NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc).timestamp()


def _cfg() -> Config:
    return Config(
        env={"TWITTERAPI_IO_KEY": "secret-test-key"},
        targets=[],
        pacing=Pacing(),
        dry_run=False,
        model="test",
        started_at_iso="2026-08-19T00:00:00+00:00",
        tier_weight={},
        stream_interval_seconds=5,
        x_detection=XDetectionBudget(
            dynamic_discovery_enabled=True,
            dynamic_discovery_min_post_age_seconds=3600,
            dynamic_discovery_max_post_age_days=30,
        ),
    )


def _stamp(days_ago: int) -> str:
    return datetime.fromtimestamp(
        NOW - days_ago * 86_400,
        timezone.utc,
    ).strftime("%a %b %d %H:%M:%S %z %Y")


def _body(views=(120_000, 200_000, 350_000), *, bio="SaaS founder") -> dict:
    texts = [
        "We shipped an AI agent workflow for software teams.",
        "SaaS pricing improves when customer value is measurable.",
        "Founders should treat product distribution as part of building.",
    ]
    return {
        "tweets": [
            {
                "id": str(index),
                "createdAt": _stamp(index),
                "viewCount": views[index - 1],
                "text": texts[index - 1],
                "isReply": False,
                "author": {
                    "userName": "builder",
                    "followers": 500_000,
                    "description": bio,
                },
            }
            for index in range(1, 4)
        ]
    }


def _state() -> State:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return State(path)


def test_measure_author_requires_fresh_100k_median_and_relevant_originals():
    result = measure_author(
        handle="Builder",
        body=_body(),
        cfg=_cfg(),
        now=NOW,
    )
    assert result["status"] == "qualified"
    assert result["median_views"] == 200_000
    assert result["sample_posts"] == 3
    assert result["topical_posts"] == 3


def test_measure_author_rejects_sub_100k_account():
    result = measure_author(
        handle="SmallBuilder",
        body=_body((40_000, 70_000, 99_999)),
        cfg=_cfg(),
        now=NOW,
    )
    assert result["status"] == "rejected"
    assert "below 100K" in result["reason"]


def test_measure_author_rejects_news_profile_even_with_business_keywords():
    result = measure_author(
        handle="BusinessNews",
        body=_body(bio="Breaking news organization covering software business"),
        cfg=_cfg(),
        now=NOW,
    )
    assert result["status"] == "rejected"
    assert "forbidden" in result["reason"]


def test_verifier_caches_one_provider_audit_and_preserves_evidence():
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers.get("X-API-Key") == "secret-test-key"
        return httpx.Response(200, json=_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    state = _state()
    verifier = DynamicAuthorVerifier(_cfg(), state, client=client)

    async def run():
        first = await verifier.verify("Builder", now=NOW)
        second = await verifier.verify("builder", now=NOW + 60)
        await client.aclose()
        return first, second

    first, second = asyncio.run(run())
    assert first["status"] == second["status"] == "qualified"
    assert calls == 1
    assert state.dynamic_qualified_author_count(NOW + 60) == 1
    assert len(second["evidence"]) == 3


def test_official_verifier_uses_same_client_sample_and_records_every_post_read():
    class Api:
        calls = 0

        async def recent_originals(self, user_id, *, max_results):
            self.calls += 1
            assert user_id == "42"
            assert max_results == 10
            provider_rows = _body()["tweets"]
            return {
                "data": [
                    {
                        "id": row["id"],
                        "created_at": datetime.fromtimestamp(
                            NOW - index * 86_400,
                            timezone.utc,
                        ).isoformat(),
                        "text": row["text"],
                        "public_metrics": {
                            "impression_count": row["viewCount"],
                        },
                    }
                    for index, row in enumerate(provider_rows, 1)
                ]
            }

    api = Api()
    state = _state()
    verifier = DynamicAuthorVerifier(_cfg(), state)
    read_ids = []

    def record_read(*, post_id, author):
        read_ids.append((post_id, author))
        return SimpleNamespace(exhausted=False)

    verdict = asyncio.run(
        verifier.verify_official(
            "Builder",
            user_id="42",
            stream_author={
                "username": "Builder",
                "name": "Builder",
                "description": "SaaS founder",
                "public_metrics": {"followers_count": 500_000},
            },
            api=api,
            record_read=record_read,
            now=NOW,
        )
    )
    assert verdict["status"] == "qualified"
    assert verdict["median_views"] == 200_000
    assert api.calls == 1
    assert read_ids == [("1", "builder"), ("2", "builder"), ("3", "builder")]
