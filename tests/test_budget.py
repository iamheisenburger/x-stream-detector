import calendar

import pytest

from xstream.budget import BudgetClosed, ReadLedger
from xstream.settings import Budget

NOON = float(calendar.timegm((2026, 10, 27, 12, 0, 0)))


def ledger(tmp_path, **budget):
    return ReadLedger(str(tmp_path / "reads.db"), Budget(**budget))


def test_a_post_is_billed_once_per_utc_day(tmp_path):
    book = ledger(tmp_path)
    assert book.record(["1", "2", "1"], now=NOON).charged == 2
    assert book.record(["1"], now=NOON + 60).charged == 0
    assert book.today_microusd(NOON) == 10_000
    assert book.record(["1"], now=NOON + 86_400).charged == 1, "X bills it again on a new UTC day"
    assert book.month_microusd(NOON + 86_400) == 15_000


def test_the_daily_cap_closes_the_stream(tmp_path):
    book = ledger(tmp_path, daily_cap_usd=0.01)
    book.assert_can_connect(NOON)
    assert not book.record(["1"], now=NOON).exhausted
    assert book.record(["2"], now=NOON).exhausted
    with pytest.raises(BudgetClosed):
        book.assert_can_connect(NOON)
    book.assert_can_connect(NOON + 86_400)


def test_the_monthly_cap_closes_the_stream(tmp_path):
    book = ledger(tmp_path, monthly_cap_usd=0.01)
    book.record(["1"], now=NOON)
    book.record(["2"], now=NOON + 86_400)
    with pytest.raises(BudgetClosed):
        book.assert_can_connect(NOON + 2 * 86_400)


def test_an_unchecked_price_refuses_to_connect(tmp_path):
    book = ledger(tmp_path, pricing_valid_through="2026-10-26")
    with pytest.raises(BudgetClosed, match="rate card"):
        book.assert_can_connect(NOON)
