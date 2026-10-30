"""Actual Claude token-cost accounting with a fail-closed monthly ceiling."""
from __future__ import annotations

import time
from datetime import date
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .config import GenerationBudget
from .state import State


class GenerationBudgetOpen(RuntimeError):
    """The configured model pricing or generation budget is no longer safe."""


class ProviderCreditExhausted(GenerationBudgetOpen):
    """The Anthropic account has no API credit left. Every further decision
    would fail the same way, so callers stop paying for X reads (like any
    closed budget) until the daily reset instead of reading posts they cannot
    answer. Oct 5-14 2026: 9 days of paid reads with zero drafts."""


CREDIT_EXHAUSTED_MARKERS = ("credit balance is too low", "purchase credits")
CREDIT_EXHAUSTED_SETTING = "provider_credit_exhausted_at"


def provider_credit_exhausted(exc: BaseException) -> bool:
    """True for the provider's out-of-credit refusal (HTTP 400,
    invalid_request_error, "Your credit balance is too low ...")."""
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)
    return (status in (None, 400, 402)) and any(marker in text for marker in CREDIT_EXHAUSTED_MARKERS)


class LlmCostGuard:
    def __init__(
        self,
        limits: GenerationBudget,
        state: State,
        *,
        lane: str | None = None,
    ):
        self.limits = limits
        self.state = state
        self.lane = lane

    def _lane_for(self, model: str) -> str:
        if self.lane in {"reply", "original"}:
            return self.lane
        return (
            "reply"
            if model in set(self.limits.reply_model_names)
            else "original"
        )

    def _rates(self, model: str) -> tuple[float, float, float, float]:
        if model == self.limits.priced_model:
            return (
                self.limits.input_usd_per_million,
                self.limits.output_usd_per_million,
                self.limits.cache_write_usd_per_million,
                self.limits.cache_read_usd_per_million,
            )
        alternate = self.limits.alternate_models.get(model)
        required = (
            "input_usd_per_million",
            "output_usd_per_million",
            "cache_write_usd_per_million",
            "cache_read_usd_per_million",
        )
        if not isinstance(alternate, dict) or any(
            key not in alternate for key in required
        ):
            raise GenerationBudgetOpen(
                f"unpriced model {model!r}; expected an explicitly priced model"
            )
        return tuple(float(alternate[key]) for key in required)

    def _request_reserve(self, model: str) -> int:
        """Return the fail-closed request reserve for this specific model.

        Reply generation uses Haiku while original posts use Sonnet. Charging
        both lanes the Sonnet-sized reserve caused reply monitoring to pause
        even when there was enough room for another measured Haiku request.
        Alternate models may therefore declare a smaller, still-conservative
        reserve; the global reserve remains the default.
        """
        alternate = self.limits.alternate_models.get(model)
        if isinstance(alternate, dict) and "max_request_microusd" in alternate:
            try:
                reserve = int(alternate["max_request_microusd"])
            except (TypeError, ValueError) as exc:
                raise GenerationBudgetOpen(
                    f"invalid request reserve for model {model!r}"
                ) from exc
            if reserve <= 0 or reserve > self.limits.max_request_microusd:
                raise GenerationBudgetOpen(
                    f"unsafe request reserve {reserve} for model {model!r}; "
                    f"expected 1..{self.limits.max_request_microusd} microUSD"
                )
            return reserve
        return self.limits.max_request_microusd

    def preflight(self, model: str) -> None:
        if not self.limits.enabled:
            return
        self._rates(model)
        if date.today() > date.fromisoformat(self.limits.pricing_valid_through):
            raise GenerationBudgetOpen(
                "configured Claude pricing has expired; review rates before generating"
            )
        today = self.state.llm_cost_microusd_today()
        month = self.state.llm_cost_microusd_month()
        reserve = self._request_reserve(model)
        lane = self._lane_for(model)
        if self.lane in {"reply", "original"}:
            reply_today = self.state.llm_cost_microusd_today_for_lane("reply")
            reply_month = self.state.llm_cost_microusd_month_for_lane("reply")
            original_today = self.state.llm_cost_microusd_today_for_lane(
                "original"
            )
            original_month = self.state.llm_cost_microusd_month_for_lane(
                "original"
            )
            incident_baseline = 0
            if (
                self.limits.reply_incident_daily_baseline_day
                == date.today().isoformat()
            ):
                incident_baseline = max(
                    0,
                    int(
                        self.limits.reply_incident_daily_baseline_microusd
                        or 0
                    ),
                )
            reply_today = max(0, reply_today - incident_baseline)
            today = max(0, today - incident_baseline)
        else:
            # Compatibility for callers that have not selected a lane. New
            # production daemons always use explicit lanes; this model-based
            # fallback keeps legacy data and standalone guard usage bounded.
            reply_models = set(self.limits.reply_model_names)
            reply_today = sum(
                self.state.llm_cost_microusd_today_for_model(reply_model)
                for reply_model in reply_models
            )
            reply_month = sum(
                self.state.llm_cost_microusd_month_for_model(reply_model)
                for reply_model in reply_models
            )
            original_today = max(0, today - reply_today)
            original_month = max(0, month - reply_month)
        protect_reply_pool = bool(
            self.limits.reply_daily_reserve_microusd
            or self.limits.reply_monthly_reserve_microusd
        )
        if lane == "reply" and protect_reply_pool:
            if reply_today + reserve > self.limits.reply_daily_reserve_microusd:
                raise GenerationBudgetOpen(
                    "daily reply-generation lane exhausted: "
                    f"{reply_today} + {reserve} > "
                    f"{self.limits.reply_daily_reserve_microusd} microUSD"
                )
            if reply_month + reserve > self.limits.reply_monthly_reserve_microusd:
                raise GenerationBudgetOpen(
                    "monthly reply-generation lane exhausted: "
                    f"{reply_month} + {reserve} > "
                    f"{self.limits.reply_monthly_reserve_microusd} microUSD"
                )
        elif protect_reply_pool:
            original_daily_cap = max(
                0,
                self.limits.daily_microusd_cap
                - self.limits.reply_daily_reserve_microusd,
            )
            original_monthly_cap = max(
                0,
                self.limits.monthly_microusd_cap
                - self.limits.reply_monthly_reserve_microusd,
            )
            if original_today + reserve > original_daily_cap:
                raise GenerationBudgetOpen(
                    "daily original-post generation lane exhausted: "
                    f"{original_today} + {reserve} > "
                    f"{original_daily_cap} microUSD"
                )
            if original_month + reserve > original_monthly_cap:
                raise GenerationBudgetOpen(
                    "monthly original-post generation lane exhausted: "
                    f"{original_month} + {reserve} > "
                    f"{original_monthly_cap} microUSD"
                )
        if today + reserve > self.limits.daily_microusd_cap:
            raise GenerationBudgetOpen(
                f"daily generation reserve exhausted: {today} + {reserve} > "
                f"{self.limits.daily_microusd_cap} microUSD"
            )
        if month + reserve > self.limits.monthly_microusd_cap:
            raise GenerationBudgetOpen(
                f"monthly generation reserve exhausted: {month} + {reserve} > "
                f"{self.limits.monthly_microusd_cap} microUSD"
            )

    @staticmethod
    def _usage_value(usage: Any, field: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(field) or 0)
        return int(getattr(usage, field, 0) or 0)

    def record(self, model: str, usage: Any) -> int:
        input_tokens = self._usage_value(usage, "input_tokens")
        output_tokens = self._usage_value(usage, "output_tokens")
        cache_write = self._usage_value(usage, "cache_creation_input_tokens")
        cache_read = self._usage_value(usage, "cache_read_input_tokens")
        input_rate, output_rate, cache_write_rate, cache_read_rate = self._rates(model)
        rates = (
            (input_tokens, input_rate),
            (output_tokens, output_rate),
            (cache_write, cache_write_rate),
            (cache_read, cache_read_rate),
        )
        # tokens * USD/MTok is numerically equal to microUSD.
        cost = sum(Decimal(tokens) * Decimal(str(rate)) for tokens, rate in rates)
        cost_microusd = int(cost.to_integral_value(rounding=ROUND_CEILING))
        self.state.record_llm_usage(
            model=model,
            lane=self._lane_for(model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            cost_microusd=cost_microusd,
        )
        request_reserve = self._request_reserve(model)
        if cost_microusd > request_reserve:
            raise GenerationBudgetOpen(
                f"one model request cost {cost_microusd} microUSD, above the "
                f"{request_reserve} request reserve for {model}"
            )
        return cost_microusd


class _MeteredMessages:
    def __init__(self, messages: Any, guard: LlmCostGuard):
        self._messages = messages
        self._guard = guard

    def create(self, *args, **kwargs):
        model = str(kwargs.get("model") or "")
        self._guard.preflight(model)
        try:
            response = self._messages.create(*args, **kwargs)
        except Exception as exc:
            if provider_credit_exhausted(exc):
                # The outside watchdog reads this to tell the owner at once.
                try:
                    self._guard.state.set_runtime_setting(CREDIT_EXHAUSTED_SETTING, time.time())
                except Exception:
                    pass
                raise ProviderCreditExhausted(
                    "the Anthropic API credit is used up. Add credit (and turn on "
                    "auto-reload) at console.anthropic.com > Plans & Billing; "
                    "monitoring resumes by itself after the UTC daily reset"
                ) from exc
            raise
        self._guard.record(model, response.usage)
        return response


class MeteredAnthropic:
    """Small duck-typed wrapper used by the existing generation pipeline."""

    def __init__(
        self,
        client: Any,
        limits: GenerationBudget,
        state: State,
        *,
        lane: str | None = None,
    ):
        self._guard = LlmCostGuard(limits, state, lane=lane)
        self.messages = _MeteredMessages(client.messages, self._guard)

    def preflight(self, model: str) -> None:
        """Check generation capacity before opening a metered upstream stream."""
        self._guard.preflight(model)
