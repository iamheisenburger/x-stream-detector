"""Settings: a JSON file for what to watch, environment variables for secrets."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Mapping

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


@dataclass
class Budget:
    # X pay-per-use price per post read (Oct 2026). X bills a post once per
    # UTC day, however many times it is delivered.
    post_read_usd: float = 0.005
    daily_cap_usd: float = 0.45
    monthly_cap_usd: float = 15.0
    # The stream refuses to connect after this date until the price above has
    # been checked against X's rate card again.
    pricing_valid_through: str = "2026-12-31"

    @property
    def post_read_microusd(self) -> int:
        return round(self.post_read_usd * 1_000_000)

    @property
    def daily_cap_microusd(self) -> int:
        return round(self.daily_cap_usd * 1_000_000)

    @property
    def monthly_cap_microusd(self) -> int:
        return round(self.monthly_cap_usd * 1_000_000)


@dataclass
class Output:
    # Every detected post is appended here as one JSON line ("" turns it off).
    jsonl_path: str = "posts.jsonl"
    # Optional: POST each post as JSON, signed with WEBHOOK_SECRET if set.
    webhook_url: str = ""
    # Optional: send each post to TELEGRAM_CHAT_ID with a one-tap reply link.
    telegram: bool = False


@dataclass
class Settings:
    handles: list[str]
    # Only posts containing one of these words are delivered, and billed.
    keywords: list[str] = field(default_factory=list)
    # Raw X rule strings added as-is, e.g. '"search console" -is:retweet lang:en'.
    extra_rules: list[str] = field(default_factory=list)
    rule_namespace: str = "xsd"
    lang: str = "en"
    include_replies: bool = False
    # [start, end] UTC hours, e.g. [12, 23] = 12:00 to 23:00. The stream is
    # disconnected outside it, so nothing is read or billed. None = always on.
    active_hours_utc: tuple[int, int] | None = None
    ledger_path: str = "reads.db"
    budget: Budget = field(default_factory=Budget)
    output: Output = field(default_factory=Output)
    request_timeout_seconds: float = 30.0
    reconnect_max_seconds: float = 60.0
    max_reconnects_per_hour: int = 12
    # Secrets, from the environment only.
    x_api_key: str = ""
    x_api_secret: str = ""
    x_bearer_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_secret: str = ""

    def validate(self) -> "Settings":
        handles = [str(h).lstrip("@").strip() for h in self.handles if str(h).strip()]
        bad = [h for h in handles if not HANDLE_RE.match(h)]
        if bad:
            raise ValueError(f"not valid X handles: {', '.join(bad[:5])}")
        if not handles and not self.extra_rules:
            raise ValueError("set at least one handle or extra rule")
        self.handles = handles
        if self.active_hours_utc is not None:
            start, end = (int(h) for h in self.active_hours_utc)
            if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
                raise ValueError("active_hours_utc must be two different hours between 0 and 23")
            self.active_hours_utc = (start, end)
        if self.budget.post_read_microusd <= 0 or self.budget.daily_cap_microusd <= 0:
            raise ValueError("budget prices and caps must be above zero")
        try:
            date.fromisoformat(self.budget.pricing_valid_through)
        except ValueError as exc:
            raise ValueError("budget.pricing_valid_through must be a YYYY-MM-DD date") from exc
        if not re.match(r"^[A-Za-z0-9_-]{1,40}$", self.rule_namespace):
            raise ValueError("rule_namespace may use letters, digits, - and _ only")
        return self


def _known(cls, raw: dict, where: str) -> dict:
    names = {f.name for f in fields(cls)}
    values = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    unknown = sorted(set(values) - names)
    if unknown:
        raise ValueError(f"unknown setting(s) in {where}: {', '.join(unknown)}")
    return values


_SECRETS = {
    "x_api_key": "X_API_KEY",
    "x_api_secret": "X_API_SECRET",
    "x_bearer_token": "X_BEARER_TOKEN",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "webhook_secret": "WEBHOOK_SECRET",
}


def load(path: str, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    raw = json.loads(Path(path).read_text())
    if any(name in raw for name in _SECRETS):
        raise ValueError("secrets belong in environment variables, not the config file")
    values = _known(Settings, raw, path)
    values["budget"] = Budget(**_known(Budget, raw.get("budget") or {}, "budget"))
    values["output"] = Output(**_known(Output, raw.get("output") or {}, "output"))
    if values.get("active_hours_utc") is not None:
        values["active_hours_utc"] = tuple(values["active_hours_utc"])
    for name, var in _SECRETS.items():
        values[name] = str(env.get(var) or "").strip()
    return Settings(**values).validate()
