"""Configuration loading for the X growth daemon.

Secrets come from .env; tunable behaviour comes from config.json so it can be
edited without touching code. Anything safety-related (caps, ramp, gaps) lives
in config.json with conservative defaults.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_env() -> dict:
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH):
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
            if m:
                env[m.group(1)] = m.group(2)
    # Real environment always overrides the file, including keys that are not
    # present in the local .env (for example systemd EnvironmentFile overrides).
    env.update(os.environ)
    return env


@dataclass
class Pacing:
    # Suspension safety lives here. Defaults are deliberately timid.
    active_hours_utc: tuple = (13, 2)          # window [start, end); wraps midnight
    daily_cap_ramp: list = field(default_factory=lambda: [5, 8, 12, 20, 30, 40, 50])
    ramp_day_length: int = 7                     # advance one ramp step per this many days
    min_gap_seconds: int = 600                   # floor between any two actions
    max_gap_jitter_seconds: int = 900            # extra 0..N random added to the gap
    per_account_cooldown_seconds: int = 21600    # 6h: don't reply to same account too often
    max_tweet_age_seconds: int = 120             # hard source-to-reply deadline
    selectivity_skip_rate: float = 0.6           # random skip among otherwise-eligible
    standalone_posts_per_day: int = 1
    standalone_min_gap_seconds: int = 3600
    # A rejected original-post draft used to retry every minute and consume the
    # model budget intended for timely reply drafts. Bound attempts per schedule
    # slot so a quality rejection stays a rejection instead of becoming a spend
    # loop.
    standalone_generation_attempts_per_slot: int = 2
    min_tweet_chars: int = 40


@dataclass
class DetectionBudget:
    # Legacy TwitterAPI.io detector. Kept only so old audit tooling can read its
    # ledger; production reply discovery must leave this disabled.
    enabled: bool = False
    allow_billable_detection_in_dry_run: bool = False
    monthly_credit_cap: int = 1_000_000
    daily_credit_cap: int = 40_000
    credits_per_delivery: int = 15
    minimum_balance_credits: int = 30_000
    balance_check_seconds: int = 60
    max_unexplained_credit_drop: int = 300
    max_paid_starts_per_day: int = 1
    startup_settlement_seconds: int = 600
    max_startup_session_debit_credits: int = 12_000
    max_deliveries_per_minute: int = 120
    max_duplicate_deliveries: int = 3
    duplicate_ratio_min_deliveries: int = 20
    max_duplicate_ratio: float = 0.15
    max_non_original_deliveries: int = 2
    max_stale_deliveries: int = 2
    max_unknown_author_deliveries: int = 1
    max_delivery_age_seconds: int = 120
    bootstrap_grace_seconds: int = 120
    max_bootstrap_stale_deliveries: int = 250
    reconnect_grace_seconds: int = 120
    max_reconnect_replay_deliveries: int = 250
    max_reconnects_per_hour: int = 3
    # TwitterAPI.io retains a disconnected websocket slot for up to 90s.
    reconnect_backoff_seconds: int = 95
    # Conservative reserves measured end-to-end on 2026-07-15. Provider
    # control-plane debits settle late, and cleanup can appear in the next run.
    credits_per_rule_activation: int = 280
    credits_per_rule_cleanup: int = 130
    max_control_plane_setup_credits: int = 3_000
    max_control_plane_cleanup_credits: int = 1_500
    rule_namespace: str = "pentra-v1"


@dataclass
class XDetectionBudget:
    """Official X filtered-stream read controls.

    X bills one post read per unique post per UTC day. The local ledger mirrors
    that rule and closes the stream before another delivery can exceed either
    cap. A dedicated app and canonical rule namespace keep all reads
    attributable to this service.
    """

    enabled: bool = False
    monthly_microusd_cap: int = 20_250_000
    daily_microusd_cap: int = 675_000
    post_read_microusd: int = 5_000
    # One-day accounting baseline for a separately identified incident. This
    # never reduces the monthly ledger and is ignored on every other UTC day.
    incident_daily_baseline_day: str = ""
    incident_daily_baseline_microusd: int = 0
    # Reads booked by measurement tasks (the posted-reply tracker, reach
    # audits; author "measure:<handle>") are a lane with their own daily cap,
    # so they never take the stream's daily room (Oct 19 2026: 132
    # measurement reads closed the stream for the day). The monthly cap still
    # counts every read.
    measurement_daily_microusd_cap: int = 300_000
    # Spread the stream's daily reads over the shift window instead of
    # spending them by early evening (bot/x_read_budget.read_pace_wait_seconds).
    read_pacing: bool = True
    read_pace_burst: int = 10
    pricing_valid_through: str = "2026-10-31"
    rule_namespace: str = "pentra-replies-v1"
    request_timeout_seconds: int = 30
    reconnect_max_seconds: int = 60
    max_reconnects_per_hour: int = 12
    # Optional second source class on the same canonical websocket. Only
    # explicitly listed, reach-verified authors are eligible, and X-edge
    # keywords plus local self-containment checks prevent conversational noise
    # from consuming generation budget.
    include_self_contained_replies: bool = False
    reply_source_handles: list[str] = field(default_factory=list)
    # Preserve room for the measured ~64 daily originals. Once this many
    # reply/thread sources have been read, the runner removes only the reply
    # rules and continues monitoring originals on the same websocket slot.
    reply_source_daily_read_cap: int = 70
    reply_rule_keywords: list[str] = field(default_factory=lambda: [
        "AI", "software", "product", "business", "founder", "startup",
        "customer", "sales", "marketing", "growth", "agent", "model",
        "API", "developer", "code", "coding", "team", "workflow",
        "design", "pricing", "revenue", "website", "app", "platform",
        "automation", "enterprise", "open source", "engineering",
        "research", "reasoning", "inference", "benchmark", "evaluation",
        "database", "deployment", "launch", "ship", "release", "hiring",
        "user", "conversion", "distribution", "feature", "integration",
        "migrate", "performance", "latency", "token", "cost", "available",
    ])
    # A fixed author roster cannot supply fifty strong posts every day. These
    # topical rules run on the SAME official X filtered-stream connection and
    # discover additional authors. An outside author is never sent to Claude
    # or Telegram until DynamicAuthorVerifier has freshly proven both reach
    # and account-level relevance from recent original posts.
    # Optional topic clause for ORIGINAL-post rules. Empty keeps author-only
    # rules (every original post is read and billed). With keywords, X only
    # delivers, and bills, originals that contain one of them.
    original_rule_keywords: list[str] = field(default_factory=list)
    # Measured reach gate for the target pool (median views per original post).
    target_min_median_views: int = 100_000
    dynamic_discovery_enabled: bool = False
    dynamic_discovery_rules: list[str] = field(default_factory=list)
    dynamic_discovery_min_median_views: int = 100_000
    dynamic_discovery_min_sample_posts: int = 3
    dynamic_discovery_max_sample_posts: int = 12
    dynamic_discovery_min_post_age_seconds: int = 21_600
    dynamic_discovery_max_post_age_days: int = 30
    dynamic_discovery_cache_ttl_seconds: int = 604_800
    dynamic_discovery_reject_ttl_seconds: int = 86_400
    dynamic_discovery_daily_author_audit_cap: int = 200
    dynamic_discovery_min_topical_ratio: float = 0.5
    dynamic_discovery_max_forbidden_ratio: float = 0.1
    dynamic_discovery_request_timeout_seconds: int = 12
    dynamic_discovery_official_sample_fetch_size: int = 10


@dataclass
class GenerationBudget:
    # X reads + model calls + writes must stay beneath the owner's monthly
    # ceiling (checked by check_monthly_ceiling). Rates are explicit because a
    # stale pricing assumption must never silently overspend.
    enabled: bool = True
    monthly_microusd_cap: int = 13_500_000
    daily_microusd_cap: int = 470_000
    # Hard-isolate the reply and original-post lanes. These values are both the
    # amount protected for replies and the maximum replies may consume. The
    # remainder is reserved exclusively for original posts.
    reply_daily_reserve_microusd: int = 400_000
    reply_monthly_reserve_microusd: int = 11_500_000
    # Same one-day treatment for an identified reply-lane incident. Monthly
    # spend remains fully counted; only today's operating circuit gets a clean
    # post-incident allowance.
    reply_incident_daily_baseline_day: str = ""
    reply_incident_daily_baseline_microusd: int = 0
    # Haiku 4.5 retires from Oct 15 2026. Its name stays listed so this
    # month's reply spend on it still counts toward the reply reserve.
    reply_model_names: list[str] = field(default_factory=lambda: [
        "claude-haiku-5-5",
        "claude-haiku-4-5-20251001",
    ])
    max_request_microusd: int = 20_000
    priced_model: str = "claude-sonnet-5"
    pricing_valid_through: str = "2026-10-31"
    input_usd_per_million: float = 2.0
    output_usd_per_million: float = 10.0
    cache_write_usd_per_million: float = 2.5
    cache_read_usd_per_million: float = 0.2
    alternate_models: dict = field(default_factory=lambda: {
        # Priced at the over-100K-prompt tier (reply prompts are far smaller),
        # so the budget never under-counts.
        "claude-haiku-5-5": {
            "input_usd_per_million": 0.5,
            "output_usd_per_million": 2.5,
            "cache_write_usd_per_million": 0.625,
            "cache_read_usd_per_million": 0.05,
            "max_request_microusd": 7_000,
        },
        "claude-haiku-4-5-20251001": {
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 5.0,
            "cache_write_usd_per_million": 1.25,
            "cache_read_usd_per_million": 0.1,
        }
    })


@dataclass
class PublishingBudget:
    """Official X API write controls.

    X's pay-per-use API currently charges per successful write. Keeping this
    budget separate from detection and generation makes the owner's monthly
    ceiling enforceable from the durable ledger.
    """

    enabled: bool = False
    monthly_microusd_cap: int = 1_000_000
    daily_microusd_cap: int = 50_000
    create_microusd: int = 15_000
    create_with_url_microusd: int = 200_000
    pricing_valid_through: str = "2026-10-31"
    require_ai_reply_approval: bool = True
    # Original posts through the official API are published without a human
    # tap. They stay off until the account owner opts in explicitly.
    original_posts_authorized: bool = False
    # Without that authorization, the posts service drafts the same evidence-
    # bound originals and sends each to Telegram; a person opens it in the X
    # app and presses Post. No X API write, no X write permission needed.
    original_post_drafts: bool = True
    # Authorized posts go to Telegram as drafts for the owner to read and post
    # himself until he is happy with them (Oct 20 2026: the v1 posts read as
    # release notes). No X API write while this is on.
    original_posts_review_first: bool = False
    # The X username the write token must belong to (checked at startup).
    expected_username: str = ""


@dataclass
class HumanApproval:
    """Human-in-the-loop delivery for outbound reply drafts.

    Telegram carries the notification only. The final public action always
    happens in X's own composer after the operator presses Post.
    """

    enabled: bool = False
    daily_candidate_cap: int = 40
    # Ramp approval drafts with pacing.daily_cap_ramp from started_at_iso, so a
    # quiet account grows its reply volume gradually up to daily_candidate_cap.
    ramp_with_pacing: bool = False
    min_candidate_gap_seconds: int = 5
    per_account_cooldown_seconds: int = 1800
    max_pending_candidates: int = 40
    candidate_ttl_seconds: int = 120
    # How old a source post may be when its draft is sent, and until when the
    # draft stays valid. 0 keeps the single hard pacing.max_tweet_age_seconds
    # deadline. A person who clears drafts in batches needs a wider window than
    # the detection freshness limit.
    approval_window_seconds: int = 0
    # Observe-only: write and store drafts exactly as in production, but do
    # not send them to Telegram. Used to measure speed and draft rate.
    observe_only: bool = False
    # Observe-only with an end: ISO time after which drafts go to Telegram
    # again without another deploy. Empty = no timed observation.
    observe_only_until_iso: str = ""
    # Append every delivered post (id, author, text, times) to this JSONL file
    # so prompt changes can be replayed against real posts. Empty = off.
    capture_posts_path: str = ""
    # Optional second alert for every draft, independent of Telegram's own
    # notifications: an ntfy.sh push to this (unguessable) topic. Tapping it
    # opens the draft in the X app. Empty disables it.
    phone_push_topic: str = ""
    request_timeout_seconds: int = 15
    require_operator_active: bool = True
    default_shift_minutes: int = 1_440
    max_shift_minutes: int = 1_440
    telegram_poll_seconds: int = 20
    # Synchronous Claude SDK calls must not stop the filtered-stream reader.
    # Zero preserves the simple serial path for tests and low-volume installs;
    # production can opt into a small bounded worker pool.
    generation_worker_threads: int = 0
    generation_pending_queue: int = 12
    # Daily window for /always monitoring, [start_hour, end_hour) in UTC (may
    # wrap midnight, e.g. [20, 4]). The paid stream runs only inside it, so the
    # day's read budget lands while the person who posts the drafts is awake
    # (Oct 2026: every posted reply was between 12:00 and 23:00 UTC; drafts
    # outside it expired unposted). Empty = around the clock. A timed /on
    # shift ignores the window.
    shift_hours_utc: list = field(default_factory=list)

    def _window(self) -> tuple[int, int] | None:
        hours = list(self.shift_hours_utc or [])
        if len(hours) != 2:
            return None
        start, end = (int(h) % 24 for h in hours)
        return None if start == end else (start, end)

    def in_shift_window(self, now: float | None = None) -> bool:
        window = self._window()
        if window is None:
            return True
        start, end = window
        hour = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def seconds_until_window_change(self, now: float | None = None) -> float | None:
        """Seconds until the window next opens (outside) or closes (inside)."""
        window = self._window()
        if window is None:
            return None
        now = time.time() if now is None else now
        current = datetime.fromtimestamp(now, timezone.utc)
        target_hour = window[1] if self.in_shift_window(now) else window[0]
        target = current.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return max(1.0, target.timestamp() - now)

    def observing(self, now: float | None = None) -> bool:
        """True while drafts are stored instead of sent to Telegram."""
        if self.observe_only:
            return True
        until = str(self.observe_only_until_iso or "").strip()
        if not until:
            return False
        try:
            end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            return False
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (time.time() if now is None else now) < end.timestamp()


@dataclass
class Config:
    env: dict
    targets: list                      # [{"handle": str, "tier": "A".."E"}]
    pacing: Pacing
    dry_run: bool
    model: str
    started_at_iso: str
    tier_weight: dict                  # tier -> keep probability
    stream_interval_seconds: int
    detection: DetectionBudget = field(default_factory=DetectionBudget)
    x_detection: XDetectionBudget = field(default_factory=XDetectionBudget)
    generation: GenerationBudget = field(default_factory=GenerationBudget)
    publishing: PublishingBudget = field(default_factory=PublishingBudget)
    human_approval: HumanApproval = field(default_factory=HumanApproval)
    reply_consents: dict = field(default_factory=dict)
    # Replies are numerous, time-sensitive, and human-reviewed. Keep their
    # inexpensive model explicit while retaining the stronger primary model
    # for the two daily original-post slots.
    reply_model: str = "claude-haiku-5-5"
    # Owner-review reply prompt: "v1" (reference) or "v2" (reply-guy voice:
    # short statements, fewer interview questions, worked examples).
    reply_prompt_version: str = "v1"
    # Original posts: "v1" (build receipts about Pentra itself) or "v2"
    # (posts for a founder who has never heard of Pentra; stances.PLAYBOOK).
    post_prompt_version: str = "v1"
    # News posts (Oct 24 2026, bot/news.py): one post a day at most on an
    # official Google Search change, from Google's own text.
    news_posts: bool = False
    # The owner's absolute monthly spend across every paid lane. Zero means
    # no combined ceiling was configured.
    monthly_ceiling_microusd: int = 0

    @property
    def handles(self) -> list:
        return [t["handle"] for t in self.targets]

    def weight_for(self, handle: str) -> float:
        h = handle.lstrip("@").lower()
        for t in self.targets:
            if t["handle"].lower() == h:
                return self.tier_weight.get(t.get("tier", "E"), 0.3)
        return 0.3                      # unknown author (shouldn't happen) — skip most

    @property
    def anthropic_key(self) -> str:
        return self.env.get("X_ANTHROPIC_API_KEY", "")

    @property
    def twitterapi_key(self) -> str:
        return self.env.get("TWITTERAPI_IO_KEY", "")

    @property
    def x_api_key(self) -> str:
        return self.env.get("X_API_KEY", "")

    @property
    def x_api_secret(self) -> str:
        return self.env.get("X_API_SECRET", "")

    @property
    def x_access_token(self) -> str:
        return self.env.get("X_ACCESS_TOKEN", "")

    @property
    def x_access_token_secret(self) -> str:
        return self.env.get("X_ACCESS_TOKEN_SECRET", "")

    @property
    def x_bearer_token(self) -> str:
        return self.env.get("X_BEARER_TOKEN", "").strip()

    @property
    def x_ai_reply_approval_reference(self) -> str:
        return self.env.get("X_AI_REPLY_APPROVAL_REFERENCE", "").strip()

    @property
    def telegram_bot_token(self) -> str:
        return self.env.get("TELEGRAM_BOT_TOKEN", "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return self.env.get("TELEGRAM_CHAT_ID", "").strip()

    def reply_consent_for(self, handle: str) -> dict | None:
        """Return an auditable active consent record for an X handle."""
        normalized = handle.lstrip("@").lower()
        record = next(
            (
                value
                for key, value in self.reply_consents.items()
                if key.lstrip("@").lower() == normalized
            ),
            None,
        )
        if not isinstance(record, dict):
            return None
        if record.get("status") != "active":
            return None
        if record.get("scope") != "automated_ai_replies":
            return None
        if not str(record.get("evidence_reference") or "").strip():
            return None
        if not str(record.get("granted_at") or "").strip():
            return None
        return record

    @property
    def consented_reply_handles(self) -> list[str]:
        return [
            handle
            for handle in self.handles
            if self.reply_consent_for(handle) is not None
        ]


class MonthlyCeilingExceeded(ValueError):
    pass


def check_monthly_ceiling(cfg: "Config") -> int:
    """Refuse a config whose lane caps add up to more than the owner's ceiling."""
    total = (
        int(cfg.x_detection.monthly_microusd_cap)
        + int(cfg.generation.monthly_microusd_cap)
        + int(cfg.publishing.monthly_microusd_cap)
    )
    if cfg.monthly_ceiling_microusd and total > cfg.monthly_ceiling_microusd:
        raise MonthlyCeilingExceeded(
            f"lane caps total ${total / 1e6:.2f}/month, above the owner's "
            f"${cfg.monthly_ceiling_microusd / 1e6:.2f} ceiling"
        )
    return total


def load() -> Config:
    env = load_env()
    raw = {}
    if os.path.exists(CONFIG_PATH):
        raw = json.load(open(CONFIG_PATH))
    p = raw.get("pacing", {})
    pacing = Pacing(**{k: (tuple(v) if k == "active_hours_utc" else v)
                       for k, v in p.items() if k in Pacing.__dataclass_fields__})
    d = raw.get("detection", {})
    detection = DetectionBudget(**{
        k: v for k, v in d.items() if k in DetectionBudget.__dataclass_fields__
    })
    xd = raw.get("x_detection", {})
    x_detection = XDetectionBudget(**{
        k: v for k, v in xd.items() if k in XDetectionBudget.__dataclass_fields__
    })
    g = raw.get("generation", {})
    generation = GenerationBudget(**{
        k: v for k, v in g.items() if k in GenerationBudget.__dataclass_fields__
    })
    w = raw.get("publishing", {})
    publishing = PublishingBudget(**{
        k: v for k, v in w.items() if k in PublishingBudget.__dataclass_fields__
    })
    h = raw.get("human_approval", {})
    human_approval = HumanApproval(**{
        k: v for k, v in h.items() if k in HumanApproval.__dataclass_fields__
    })
    cfg = Config(
        env=env,
        targets=raw.get("targets", []),
        pacing=pacing,
        dry_run=raw.get("dry_run", True),          # SAFE DEFAULT: never posts until flipped
        model=raw.get("model", "claude-sonnet-5"),
        started_at_iso=raw.get("started_at_iso", ""),
        tier_weight=raw.get("tier_weight",
                            {"A": 0.9, "B": 0.75, "C": 0.55, "D": 0.38, "E": 0.22}),
        stream_interval_seconds=raw.get("stream_interval_seconds", 5),
        detection=detection,
        x_detection=x_detection,
        generation=generation,
        publishing=publishing,
        human_approval=human_approval,
        reply_consents=raw.get("reply_consents", {}),
        reply_model=raw.get("reply_model", "claude-haiku-5-5"),
        reply_prompt_version=str(raw.get("reply_prompt_version", "v1")),
        post_prompt_version=str(raw.get("post_prompt_version", "v1")),
        news_posts=bool(raw.get("news_posts", False)),
        monthly_ceiling_microusd=int(
            round(float(raw.get("monthly_ceiling_usd", 0)) * 1_000_000)
        ),
    )
    check_monthly_ceiling(cfg)
    return cfg
