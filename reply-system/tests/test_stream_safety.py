"""Failure-mode tests for the metered TwitterAPI.io boundary."""
import asyncio
import os
import sqlite3
import tempfile
import time
from dataclasses import replace

import pytest

import bot.stream as stream_module

from bot.config import DetectionBudget
from bot.costguard import CostCircuitOpen, DeliveryGuard
from bot.llm_budget import GenerationBudgetOpen, LlmCostGuard, MeteredAnthropic
from bot.config import GenerationBudget
from bot.state import State
from bot.stream import (
    ProviderError,
    RuleRecord,
    RuleSyncError,
    activate_staged_rules,
    build_rule_specs,
    build_rule_values,
    clear_all_rules_with_api,
    deactivate_all_rules_with_api,
    open_tweet_websocket,
    reconcile_rules,
    stage_rules,
)


class FakeApi:
    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.add_calls = 0
        self.update_calls = []
        self.delete_calls = []
        self.fail_add_at = None
        self.fail_delete = False
        self.monitored_users = []

    async def get_rules(self):
        return list(self.rules)

    async def add_rule(self, spec):
        self.add_calls += 1
        if self.fail_add_at == self.add_calls:
            raise ProviderError("synthetic add failure")
        rule = RuleRecord(f"r{self.add_calls}", spec.tag, spec.value, spec.interval_seconds, 0)
        self.rules.append(rule)
        return rule

    async def set_rule_active(self, rule, active):
        self.update_calls.append((rule.rule_id, active))
        self.rules = [
            replace(existing, is_effect=int(active))
            if existing.rule_id == rule.rule_id else existing
            for existing in self.rules
        ]

    async def delete_rule(self, rule_id):
        self.delete_calls.append(rule_id)
        if self.fail_delete:
            raise ProviderError("synthetic delete failure")
        self.rules = [rule for rule in self.rules if rule.rule_id != rule_id]

    async def get_monitored_users(self):
        return list(self.monitored_users)


def run(coro):
    return asyncio.run(coro)


def state():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return State(path)


def test_llm_lane_migration_is_idempotent_after_an_alter_race(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE llm_usage(
            id INTEGER PRIMARY KEY,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            cost_microusd INTEGER NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL,
            month TEXT NOT NULL
        )"""
    )
    connection.execute(
        """INSERT INTO llm_usage(
            model,input_tokens,output_tokens,cache_write_tokens,
            cache_read_tokens,cost_microusd,at,day,month
        ) VALUES ('claude-sonnet-5',1,1,0,0,100,0,'2026-07-27','2026-07')"""
    )
    connection.commit()
    connection.close()

    original_connect = sqlite3.connect
    injected = False

    class RaceConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def execute(self, sql, *args):
            nonlocal injected
            if sql.startswith("ALTER TABLE llm_usage") and not injected:
                injected = True
                other = original_connect(path)
                other.execute(
                    "ALTER TABLE llm_usage "
                    "ADD COLUMN lane TEXT NOT NULL DEFAULT 'legacy'"
                )
                other.commit()
                other.close()
            return self.wrapped.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def connect(*args, **kwargs):
        return RaceConnection(original_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", connect)
    migrated = State(path)
    # The seeded row belongs to 2026-07; query that month, not the wall clock.
    july = 1785110400.0  # 2026-07-27T00:00:00Z
    assert migrated.llm_cost_microusd_month_for_lane("legacy", now=july) == 100


def tweet(tweet_id="1", *, author="target", created=None, reply=False, retweet=False):
    return {
        "id": tweet_id,
        "text": "a sufficiently substantive tweet for the delivery guard to inspect",
        "author": {"username": author},
        "created_at_epoch": time.time() if created is None else created,
        "is_reply": reply,
        "is_retweet": retweet,
        "_twitterapi_rule_id": "r1",
    }


def test_rule_values_validate_and_deduplicate_handles():
    values = build_rule_values(["@Target", "target", "second"])
    assert sum(value.count("from:") for value in values) == 2
    assert all(value.endswith("-is:reply -is:retweet") for value in values)
    with pytest.raises(ValueError):
        build_rule_values(["not a valid handle"])


def test_exact_rule_set_is_reused_without_recreation():
    specs = build_rule_specs(["target", "second"])
    existing = [
        RuleRecord(f"r{i}", spec.tag, spec.value, spec.interval_seconds, 1)
        for i, spec in enumerate(specs)
    ]
    api = FakeApi(existing)
    result = run(reconcile_rules(api, ["target", "second"]))
    assert result == existing
    assert api.add_calls == 0
    assert api.delete_calls == []


def test_staging_reuses_rules_but_leaves_them_inactive():
    specs = build_rule_specs(["target", "second"])
    existing = [
        RuleRecord(f"r{i}", spec.tag, spec.value, spec.interval_seconds, 1)
        for i, spec in enumerate(specs)
    ]
    api = FakeApi(existing)
    staged = run(stage_rules(api, ["target", "second"]))
    assert all(rule.is_effect == 0 for rule in staged)
    assert api.add_calls == 0
    assert api.delete_calls == []


def test_activation_is_separate_and_normal_stop_preserves_rules():
    specs = build_rule_specs(["target", "second"])
    api = FakeApi([
        RuleRecord(f"r{i}", spec.tag, spec.value, spec.interval_seconds, 0)
        for i, spec in enumerate(specs)
    ])
    active = run(activate_staged_rules(api, api.rules))
    assert all(rule.is_effect == 1 for rule in active)
    assert run(deactivate_all_rules_with_api(api)) == len(active)
    assert len(api.rules) == len(active)
    assert all(rule.is_effect == 0 for rule in api.rules)
    assert api.delete_calls == []


def test_ready_websocket_does_not_mask_daemon_body_errors(monkeypatch):
    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def recv(self):
            return '{"event_type":"connected"}'

    monkeypatch.setattr(
        stream_module.websockets,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    async def scenario():
        async with open_tweet_websocket("test-key"):
            raise RuntimeError("daemon-body-failure")

    with pytest.raises(RuntimeError, match="daemon-body-failure"):
        run(scenario())


def test_inactive_exact_rule_set_is_activated_and_reverified():
    specs = build_rule_specs(["target", "second"])
    existing = [
        RuleRecord(f"r{i}", spec.tag, spec.value, spec.interval_seconds, 0)
        for i, spec in enumerate(specs)
    ]
    api = FakeApi(existing)
    result = run(reconcile_rules(api, ["target", "second"]))
    assert all(rule.is_effect == 1 for rule in result)
    assert api.add_calls == 0


def test_partial_rule_install_rolls_back_to_zero():
    handles = [f"account{i}" for i in range(40)]
    api = FakeApi()
    api.fail_add_at = 2
    with pytest.raises(RuleSyncError, match="rolled back"):
        run(reconcile_rules(api, handles))
    assert api.rules == []


def test_failed_deletion_is_never_reported_as_success():
    spec = build_rule_specs(["target"])[0]
    api = FakeApi([RuleRecord("r1", spec.tag, spec.value, 5, 1)])
    api.fail_delete = True
    with pytest.raises(RuleSyncError, match="residue=r1"):
        run(clear_all_rules_with_api(api, max_sweeps=2))
    assert api.rules[0].is_effect == 0


def test_duplicate_replay_opens_cost_circuit():
    st = state()
    limits = replace(
        DetectionBudget(),
        max_duplicate_deliveries=1,
        duplicate_ratio_min_deliveries=100,
    )
    guard = DeliveryGuard(limits, st, ["target"], starting_balance_credits=100_000)
    guard.observe(tweet("1"))
    guard.observe(tweet("1"))
    with pytest.raises(CostCircuitOpen, match="duplicate replay"):
        guard.observe(tweet("1"))


def test_source_filter_and_backfill_fail_closed():
    now = time.time()
    st = state()
    source_limits = replace(DetectionBudget(), max_non_original_deliveries=0)
    source_guard = DeliveryGuard(
        source_limits, st, ["target"], starting_balance_credits=100_000
    )
    with pytest.raises(CostCircuitOpen, match="source filter failure"):
        source_guard.observe(tweet("reply", reply=True), now=now)

    st2 = state()
    stale_limits = replace(
        DetectionBudget(),
        max_stale_deliveries=0,
        bootstrap_grace_seconds=0,
    )
    stale_guard = DeliveryGuard(
        stale_limits, st2, ["target"], starting_balance_credits=100_000
    )
    with pytest.raises(CostCircuitOpen, match="backfill anomaly"):
        stale_guard.observe(tweet("old", created=now - 600), now=now)


def test_bounded_startup_backfill_is_metered_but_not_treated_as_live_replay():
    now = time.time()
    st = state()
    limits = replace(
        DetectionBudget(),
        bootstrap_grace_seconds=30,
        max_bootstrap_stale_deliveries=3,
        max_stale_deliveries=0,
        max_deliveries_per_minute=1,
    )
    guard = DeliveryGuard(
        limits,
        st,
        ["target"],
        starting_balance_credits=100_000,
        started_at=now,
    )
    for index in range(3):
        snapshot = guard.observe(
            tweet(str(index), created=now - 600),
            now=now + index,
        )
    assert snapshot.bootstrap_stale == 3
    assert snapshot.stale == 0
    assert snapshot.classification == "bootstrap_stale"
    assert st.provider_credits_today(now) == 45

    with pytest.raises(CostCircuitOpen, match="startup backfill exceeded bound"):
        guard.observe(tweet("4", created=now - 600), now=now + 3)


def test_stale_delivery_after_bootstrap_still_fails_closed():
    now = time.time()
    st = state()
    limits = replace(
        DetectionBudget(),
        bootstrap_grace_seconds=5,
        max_stale_deliveries=0,
    )
    guard = DeliveryGuard(
        limits,
        st,
        ["target"],
        starting_balance_credits=100_000,
        started_at=now,
    )
    with pytest.raises(CostCircuitOpen, match="backfill anomaly"):
        guard.observe(tweet("late", created=now - 600), now=now + 6)


def test_reconnect_replay_is_metered_and_dropped_without_duplicate_trip():
    now = time.time()
    st = state()
    limits = replace(
        DetectionBudget(),
        max_duplicate_deliveries=0,
        max_stale_deliveries=0,
        max_reconnect_replay_deliveries=2,
    )
    guard = DeliveryGuard(
        limits,
        st,
        ["target"],
        starting_balance_credits=100_000,
        started_at=now,
    )
    guard.observe(tweet("fresh"), now=now)
    guard.begin_connection(now=now + 10)

    duplicate = guard.observe(tweet("fresh"), now=now + 11)
    stale = guard.observe(
        tweet("old", created=now - 600),
        now=now + 12,
    )
    assert duplicate.classification == "connection_replay"
    assert stale.classification == "connection_replay"
    assert stale.connection_replay == 2
    assert stale.duplicates == 0
    assert stale.stale == 0

    with pytest.raises(CostCircuitOpen, match="reconnect replay exceeded"):
        guard.observe(tweet("older", created=now - 600), now=now + 13)


def test_reconnect_rate_is_bounded_without_buying_new_rule_lifecycle():
    now = time.time()
    st = state()
    limits = replace(DetectionBudget(), max_reconnects_per_hour=2)
    guard = DeliveryGuard(
        limits,
        st,
        ["target"],
        starting_balance_credits=100_000,
        started_at=now,
    )
    guard.begin_connection(now=now + 1)
    guard.begin_connection(now=now + 2)
    with pytest.raises(CostCircuitOpen, match="reconnect cap exceeded"):
        guard.begin_connection(now=now + 3)
    assert st.provider_paid_starts_today(now) == 0


def test_unknown_author_is_not_hidden_inside_startup_backfill():
    now = time.time()
    st = state()
    limits = replace(
        DetectionBudget(),
        max_unknown_author_deliveries=0,
        bootstrap_grace_seconds=120,
    )
    guard = DeliveryGuard(
        limits,
        st,
        ["target"],
        starting_balance_credits=100_000,
        started_at=now,
    )
    with pytest.raises(CostCircuitOpen, match="unknown authors"):
        guard.observe(
            tweet("intruder", author="other", created=now - 600),
            now=now + 1,
        )


def test_delivery_credit_caps_survive_process_restart():
    st = state()
    limits = replace(DetectionBudget(), daily_credit_cap=29, monthly_credit_cap=1000)
    first = DeliveryGuard(limits, st, ["target"], starting_balance_credits=100_000)
    first.observe(tweet("1"))

    second = DeliveryGuard(limits, st, ["target"], starting_balance_credits=100_000)
    with pytest.raises(CostCircuitOpen, match="daily detection cap"):
        second.observe(tweet("2"))
    assert st.provider_credits_today() == 30


def test_unexplained_provider_balance_drop_opens_circuit():
    st = state()
    limits = replace(
        DetectionBudget(),
        max_unexplained_credit_drop=30,
        startup_settlement_seconds=0,
    )
    guard = DeliveryGuard(limits, st, ["target"], starting_balance_credits=100_000,
                          started_at=time.time() - 1000)
    guard.observe(tweet("1"))
    guard.verify_balance(100_000)  # establish the steady-state baseline
    guard.observe(tweet("2"))
    with pytest.raises(CostCircuitOpen, match="unexplained provider burn"):
        guard.verify_balance(99_950)  # 50 drop - 15 observed = 35 unexplained


def test_startup_settlement_is_bounded_before_strict_reconciliation():
    st = state()
    limits = replace(
        DetectionBudget(),
        startup_settlement_seconds=600,
        max_startup_session_debit_credits=2_000,
    )
    guard = DeliveryGuard(
        limits,
        st,
        ["target"],
        starting_balance_credits=100_000,
        started_at=time.time(),
    )
    guard.verify_balance(99_000)
    with pytest.raises(CostCircuitOpen, match="startup provider debit"):
        guard.verify_balance(97_999)


def test_authoritative_balance_cap_catches_unclassified_burn():
    st = state()
    st.record_provider_balance(100_000)
    limits = replace(DetectionBudget(), daily_credit_cap=999)
    guard = DeliveryGuard(
        limits, st, ["target"], starting_balance_credits=100_000
    )
    with pytest.raises(CostCircuitOpen, match="authoritative daily"):
        guard.verify_balance(99_000)


def test_balance_debits_survive_restart_and_topup_does_not_erase_spend():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    now = time.time()
    first = State(path)
    first.record_provider_balance(100_000, now=now)
    first.record_provider_balance(99_500, now=now + 1)
    first.close()

    second = State(path)
    second.record_provider_balance(100_000, now=now + 2)  # recharge
    second.record_provider_balance(99_500, now=now + 3)
    assert second.provider_balance_debits_today(now + 3) == 1_000


def test_paid_start_reservation_survives_restart():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    first = State(path)
    assert first.reserve_provider_paid_start(1)
    first.close()

    second = State(path)
    assert not second.reserve_provider_paid_start(1)
    assert second.provider_paid_starts_today() == 1


def test_llm_costs_use_actual_usage_and_persist():
    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        input_usd_per_million=2.0,
        output_usd_per_million=10.0,
        cache_write_usd_per_million=2.5,
        cache_read_usd_per_million=0.2,
    )
    guard = LlmCostGuard(limits, st)
    cost = guard.record(
        "claude-sonnet-5",
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 50,
        },
    )
    assert cost == 510
    assert st.llm_cost_microusd_today() == 510
    assert st.llm_cost_microusd_month() == 510


def test_llm_budget_and_model_pricing_fail_closed():
    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=50_000,
        reply_daily_reserve_microusd=0,
        reply_monthly_reserve_microusd=0,
        max_request_microusd=50_000,
    )
    guard = LlmCostGuard(limits, st)
    guard.preflight("claude-sonnet-5")
    st.record_llm_usage(
        model="claude-sonnet-5",
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_microusd=1,
    )
    with pytest.raises(GenerationBudgetOpen, match="daily generation reserve"):
        guard.preflight("claude-sonnet-5")
    with pytest.raises(GenerationBudgetOpen, match="unpriced model"):
        guard.preflight("different-model")


def test_llm_budget_prices_the_explicit_low_cost_reply_model():
    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=20_000,
        max_request_microusd=20_000,
    )
    guard = LlmCostGuard(limits, st)
    model = "claude-haiku-4-5-20251001"
    guard.preflight(model)
    cost = guard.record(model, {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 1000,
    })
    assert cost == 4850


def test_llm_budget_uses_model_specific_request_reserve():
    st = state()
    model = "claude-haiku-4-5-20251001"
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=470_000,
        reply_daily_reserve_microusd=400_000,
        reply_monthly_reserve_microusd=11_500_000,
        max_request_microusd=20_000,
        alternate_models={
            model: {
                "input_usd_per_million": 1.0,
                "output_usd_per_million": 5.0,
                "cache_write_usd_per_million": 1.25,
                "cache_read_usd_per_million": 0.1,
                "max_request_microusd": 7_000,
            }
        },
    )
    st.record_llm_usage(
        model=model,
        lane="reply",
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_microusd=382_469,
    )
    guard = LlmCostGuard(limits, st, lane="reply")
    guard.preflight(model)
    with pytest.raises(GenerationBudgetOpen, match="7000 request reserve"):
        guard.record(
            model,
            {
                "input_tokens": 2_001,
                "output_tokens": 1_000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )


def test_original_posts_cannot_consume_reserved_reply_budget():
    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=650_000,
        reply_daily_reserve_microusd=300_000,
        monthly_microusd_cap=18_000_000,
        reply_monthly_reserve_microusd=9_000_000,
        max_request_microusd=20_000,
    )
    st.record_llm_usage(
        model="claude-sonnet-5",
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_microusd=340_001,
    )
    guard = LlmCostGuard(limits, st)
    with pytest.raises(GenerationBudgetOpen, match="daily original-post"):
        guard.preflight("claude-sonnet-5")
    guard.preflight("claude-haiku-4-5-20251001")


def test_reply_generation_cannot_consume_original_post_budget():
    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=650_000,
        reply_daily_reserve_microusd=300_000,
        monthly_microusd_cap=18_000_000,
        reply_monthly_reserve_microusd=9_000_000,
        max_request_microusd=20_000,
    )
    reply_model = "claude-haiku-4-5-20251001"
    st.record_llm_usage(
        model=reply_model,
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_microusd=285_000,
    )
    guard = LlmCostGuard(limits, st)
    with pytest.raises(GenerationBudgetOpen, match="daily reply-generation lane"):
        guard.preflight(reply_model)
    guard.preflight("claude-sonnet-5")


def test_explicit_lanes_do_not_misclassify_legacy_sonnet_reply_spend():
    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=470_000,
        reply_daily_reserve_microusd=400_000,
        monthly_microusd_cap=13_500_000,
        reply_monthly_reserve_microusd=11_500_000,
        max_request_microusd=20_000,
    )
    st.record_llm_usage(
        model="claude-sonnet-5",
        lane="legacy",
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_microusd=3_700_000,
        now=time.time() - 86_400,
    )
    original = LlmCostGuard(limits, st, lane="original")
    original.preflight("claude-sonnet-5")
    original.record(
        "claude-sonnet-5",
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )
    assert st.llm_cost_microusd_month_for_lane("original") == 400
    assert st.llm_cost_microusd_month_for_lane("reply") == 0


def test_metered_anthropic_exposes_budget_preflight_before_stream_open():
    class FakeClient:
        messages = object()

    st = state()
    limits = replace(
        GenerationBudget(),
        pricing_valid_through="2999-12-31",
        daily_microusd_cap=50_000,
        reply_daily_reserve_microusd=0,
        reply_monthly_reserve_microusd=0,
        max_request_microusd=50_000,
    )
    client = MeteredAnthropic(FakeClient(), limits, st)
    client.preflight("claude-sonnet-5")
    st.record_llm_usage(
        model="claude-sonnet-5",
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_microusd=1,
    )
    with pytest.raises(GenerationBudgetOpen, match="daily generation reserve"):
        client.preflight("claude-sonnet-5")


def test_out_of_credit_refusal_closes_generation_like_a_budget():
    """Oct 5-14 2026: the Anthropic credit ran out and the bot kept paying for
    90 X reads a day while every decision failed. The refusal now raises a
    GenerationBudgetOpen, which pauses paid reads until the UTC reset."""
    from bot.llm_budget import ProviderCreditExhausted, provider_credit_exhausted

    class CreditRefusal(Exception):
        status_code = 400

    class Messages:
        def __init__(self, error):
            self.error = error

        def create(self, **kwargs):
            raise self.error

    class FakeClient:
        def __init__(self, error):
            self.messages = Messages(error)

    st = state()
    limits = replace(GenerationBudget(), pricing_valid_through="2999-12-31", daily_microusd_cap=50_000,
                     reply_daily_reserve_microusd=0, reply_monthly_reserve_microusd=0, max_request_microusd=50_000)
    refusal = CreditRefusal("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
                            "'message': 'Your credit balance is too low to access the Anthropic API. "
                            "Please go to Plans & Billing to upgrade or purchase credits.'}}")
    client = MeteredAnthropic(FakeClient(refusal), limits, st)
    with pytest.raises(ProviderCreditExhausted, match="console.anthropic.com") as raised:
        client.messages.create(model="claude-sonnet-5", max_tokens=10, messages=[])
    assert isinstance(raised.value, GenerationBudgetOpen)
    assert provider_credit_exhausted(refusal)
    assert float(st.runtime_setting("provider_credit_exhausted_at", "0")) > 0, "the watchdog can see the refusal"
    # Any other provider error is unchanged.
    other = CreditRefusal("Error code: 400 - prompt is too long")
    with pytest.raises(CreditRefusal):
        MeteredAnthropic(FakeClient(other), limits, st).messages.create(model="claude-sonnet-5", max_tokens=10, messages=[])
    assert not provider_credit_exhausted(other)
