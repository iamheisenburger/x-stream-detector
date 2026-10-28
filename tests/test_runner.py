import asyncio
import calendar

import pytest

from xstream.budget import BudgetClosed, ReadLedger
from xstream.client import RateLimited
from xstream.events import parse_stream_event
from xstream.runner import (
    Detector,
    interruption_delay,
    rate_limit_delay,
    seconds_until_close,
    seconds_until_open,
)
from xstream.settings import Budget, Settings

NOON = float(calendar.timegm((2026, 10, 27, 12, 0, 0)))


def at(hour: int, minute: int = 0) -> float:
    return float(calendar.timegm((2026, 10, 27, hour, minute, 0)))


def post(post_id: str, created: float = NOON - 4.9) -> dict:
    stamp = f"2026-10-27T{int(created % 86_400 // 3600):02d}:{int(created % 3600 // 60):02d}:{created % 60:06.3f}Z"
    return parse_stream_event({
        "data": {"id": post_id, "author_id": "1", "text": f"post {post_id}", "created_at": stamp},
        "includes": {"users": [{"id": "1", "username": "alice"}]},
    })


class FakeClient:
    def __init__(self, events, *, hang=False):
        self._events = events
        self.hang = hang
        self.synced = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def sync_rules(self, specs):
        self.synced.append(specs)
        return specs

    async def events(self):
        for event in self._events:
            yield event
        if self.hang:
            await asyncio.sleep(3600)


def detector(tmp_path, fake, clock=lambda: NOON, **settings):
    budget = settings.pop("budget", Budget())
    cfg = Settings(handles=["alice"], budget=budget, **settings).validate()
    return Detector(cfg, ReadLedger(str(tmp_path / "r.db"), budget), client_factory=lambda s: fake, clock=clock)


def test_connection_limit_429s_back_off_but_never_past_the_reset():
    limit = RateLimited("X stream failed HTTP 429: maximum allowed connection limit", retry_at=NOON + 900)
    assert [rate_limit_delay(limit, now=NOON, streak=s) for s in range(6)] == [30, 60, 120, 240, 480, 480]
    assert rate_limit_delay(RateLimited("Too Many Requests", retry_at=NOON + 900), now=NOON, streak=0) == 905
    assert interruption_delay(Exception("X stream transport interrupted"), 1) == 10


def test_active_hours_including_a_window_across_midnight():
    assert seconds_until_open(at(11), (12, 23)) == 3600
    assert seconds_until_open(at(12, 30), (12, 23)) == 0
    assert seconds_until_open(at(23, 30), (12, 23)) == 12.5 * 3600
    assert seconds_until_close(at(12, 30), (12, 23)) == 10.5 * 3600
    assert seconds_until_open(at(2), (20, 4)) == 0 and seconds_until_close(at(2), (20, 4)) == 2 * 3600
    assert seconds_until_open(at(10), (20, 4)) == 10 * 3600
    assert seconds_until_open(at(10), None) == 0 and seconds_until_close(at(10), None) is None


def test_a_session_books_each_post_then_hands_it_over(tmp_path):
    fake = FakeClient([post("1"), post("2"), post("1")])
    d = detector(tmp_path, fake)
    seen = []

    async def on_post(event):
        seen.append(event["id"])

    async def go():
        reason = await d.session(on_post)
        await asyncio.sleep(0)
        return reason

    assert asyncio.run(go()) == "stream-ended"
    assert seen == ["1", "2", "1"]
    assert d.ledger.reads_today(NOON) == 2, "a redelivered post is not billed twice"
    assert fake.synced == [d.specs]


def test_the_read_cap_disconnects_the_stream(tmp_path):
    fake = FakeClient([post("1"), post("2"), post("3")])
    d = detector(tmp_path, fake, budget=Budget(daily_cap_usd=0.01))
    seen = []

    async def on_post(event):
        seen.append(event["id"])

    async def go():
        reason = await d.session(on_post)
        await asyncio.sleep(0)
        return reason

    assert asyncio.run(go()) == "budget"
    assert seen == ["1", "2"]
    with pytest.raises(BudgetClosed):
        asyncio.run(d.session(on_post))


def test_a_failing_handler_never_stops_the_stream(tmp_path):
    d = detector(tmp_path, FakeClient([post("1"), post("2")]))
    seen = []

    async def on_post(event):
        seen.append(event["id"])
        raise RuntimeError("webhook down")

    async def go():
        reason = await d.session(on_post)
        await asyncio.sleep(0)
        return reason

    assert asyncio.run(go()) == "stream-ended" and seen == ["1", "2"]


def test_the_stream_disconnects_when_active_hours_end(tmp_path):
    d = detector(tmp_path, FakeClient([post("1")], hang=True), clock=lambda: at(13) - 0.05, active_hours_utc=(12, 13))

    async def on_post(event):
        return None

    assert asyncio.run(asyncio.wait_for(d.session(on_post), 5)) == "window-closed"


def test_reconnects_past_the_hourly_ceiling_are_held_off(tmp_path):
    clock = [NOON]
    d = detector(tmp_path, FakeClient([]), clock=lambda: clock[0], max_reconnects_per_hour=3)
    waits = []
    for _ in range(4):
        waits.append(d._reconnect_wait())
        clock[0] += 60
    assert waits[:3] == [0, 0, 0] and waits[3] == 3600 - 180
