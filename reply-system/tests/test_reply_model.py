"""The reply model must be priced and counted in the reply lane. Both Haiku
4.5 and Haiku 5.5 are priced so the reply model can be switched by config
(Oct 15: replies went back to Haiku 4.5; on Haiku 5.5 the grounding check
vetoed every draft for 17 hours)."""
import json
from pathlib import Path

from bot.config import GenerationBudget

ROOT = Path(__file__).resolve().parent.parent
PRICE_KEYS = {"input_usd_per_million", "output_usd_per_million",
              "cache_write_usd_per_million", "cache_read_usd_per_million"}


def _check(reply_model: str, generation: dict) -> None:
    assert reply_model in {"claude-haiku-5-5", "claude-haiku-4-5-20251001"}
    assert reply_model in generation["reply_model_names"]
    assert {"claude-haiku-5-5", "claude-haiku-4-5-20251001"} <= set(generation["alternate_models"])
    for model in ("claude-haiku-5-5", "claude-haiku-4-5-20251001"):
        assert PRICE_KEYS <= set(generation["alternate_models"][model])
    # Never cheaper than the published prices.
    assert generation["alternate_models"]["claude-haiku-5-5"]["input_usd_per_million"] >= 0.10
    assert generation["alternate_models"]["claude-haiku-4-5-20251001"]["input_usd_per_million"] >= 1.0
    # This month's Haiku 4.5 reply spend still counts toward the reply lane.
    assert "claude-haiku-4-5-20251001" in generation["reply_model_names"]


def test_deployed_config_prices_and_counts_reply_model():
    raw = json.loads((ROOT / "config.json").read_text())
    _check(raw["reply_model"], raw["generation"])


def test_code_defaults_price_and_count_reply_model():
    from bot.config import Config
    default_reply = Config.__dataclass_fields__["reply_model"].default
    g = GenerationBudget()
    _check(default_reply, {"reply_model_names": g.reply_model_names, "alternate_models": g.alternate_models})


def test_truncated_tool_call_is_discarded_and_limits_leave_room():
    from types import SimpleNamespace
    from bot import generate
    block = SimpleNamespace(type="tool_use", name="verify_reply", input={"verdict": "safe"})
    assert generate._tool_input(SimpleNamespace(content=[block], stop_reason="tool_use"), "verify_reply") == {"verdict": "safe"}
    assert generate._tool_input(SimpleNamespace(content=[block], stop_reason="max_tokens"), "verify_reply") is None
    assert generate._output_tokens(180) >= 260
