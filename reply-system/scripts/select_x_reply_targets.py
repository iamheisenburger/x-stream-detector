#!/usr/bin/env python3
"""Select the live reply-monitoring slate from the measured high-reach pool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The daily timer runs this file by path (Oct 19-20 2026: `from bot...` failed
# with ModuleNotFoundError there, so no daily selection ran).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.reply_formula import MIN_REACH_FLOOR  # noqa: E402


# Draft-per-read prior carried over from the reference reply system's measured
# history. It only matters until Pentra's own state.db has reads and drafts;
# LIVE_PRIOR_STRENGTH sets how quickly Pentra's own evidence takes over.
LIVE_PRIOR_RATE = 94 / 573
LIVE_PRIOR_STRENGTH = 39
TIER_WEIGHTS = {"A": 0.90, "B": 0.75, "C": 0.55, "D": 0.38, "E": 0.22}
TIER_ORDER = ["E", "D", "C", "B", "A"]
CATEGORY_MINIMUM = 4
# Company and product accounts are never reply targets (Oct 20 2026): replies
# under them earned the fewest impressions, their audience is developers
# watching launches, and a brand never likes or answers a reply, one of the
# reply-ranking inputs X documents. Lowercase handles.
BRAND_ACCOUNTS = frozenset({
    "a16z", "abacusai", "aiatmeta", "alibaba_qwen", "anthropicai", "antigravity",
    "box", "cerebras", "chatgpt", "claudedevs", "cline", "cognition", "cursor_ai",
    "figma", "flexport", "gemini_notebook", "geminiapp", "github", "google",
    "googleai", "googleaistudio", "googledeepmind", "kling_ai", "nousresearch",
    "nvidia", "ollama", "openai", "openaidevs", "openclaw", "palantirtech",
    "perplexity_ai", "spacexai", "ultralinx", "vercel", "vercel_dev", "xai",
    "ycombinator",
})
# AI-industry people are not reply targets either (Oct 20 2026): our replies
# under the AI and automation category earned a median of 34 impressions (14
# settled replies, 4 likes) against 132 under business accounts and 1,821 under
# @nikitabier, and their followers are researchers and developers, not the
# founders, marketers and business owners Pentra sells to. Lowercase handles.
AI_INDUSTRY_ACCOUNTS = frozenset({
    # AI-lab staff and researchers
    "alexandr_wang", "amandaaskell", "bcherny", "demishassabis", "gdb", "ibab",
    "jxmnop", "karpathy", "officiallogank", "pushmeet", "rasbt", "sama",
    "sebastienbubeck", "thsottiaux", "victortaelin",
    # megacap and enterprise CEOs
    "alighodsi", "aravsrinivas", "benioff", "danroberts0101", "finkd",
    "jensenhuang", "levie", "pgelsinger", "sundarpichai",
    # developer tools and developer educators
    "addyosmani", "akshay_pachaar", "gergelyorosz", "kirat_tw", "leerob",
    "mattpocockuk", "mitchellh", "nickscamara_", "nutlope", "rauchg", "simonw",
    "steipete", "theo", "williambryk", "yuchenj_uw",
    # AI-industry investors and analysts
    "bgurley", "deedydas", "dylan522p", "gavinsbaker", "omooretweets",
    "venturetwins", "vkhosla",
    # other fields: medicine, crypto, robotics, nuclear, policy, research podcasts
    "adcock_brett", "dwarkesh_sp", "erictopol", "iohk_charles", "isaiah_p_taylor",
    "shai_wininger", "sriramk",
})
NOT_REPLY_TARGETS = BRAND_ACCOUNTS | AI_INDUSTRY_ACCOUNTS
# The rest of that category (creators who teach business owners to use AI,
# builders who write for founders) keeps at most this share of a day's reads;
# founders, business, marketing, SEO and product people get the rest.
CATEGORY_READ_SHARE = {"AI and automation": 0.25}
PRIMARY_MIN_MEDIAN_VIEWS = 100_000
MIN_REACH_SAMPLE_POSTS = 3
REPLY_LANE_CATEGORIES = {
    "SEO and content",
    "General business",
    "AI and automation",
    "Product and growth",
    "SaaS and founders",
    "Sales and marketing",
}


@dataclass(frozen=True)
class LiveYield:
    reads: int = 0
    drafts: int = 0

    @property
    def posterior(self) -> float:
        return (
            self.drafts + LIVE_PRIOR_RATE * LIVE_PRIOR_STRENGTH
        ) / (self.reads + LIVE_PRIOR_STRENGTH)


def load_live_yield(state_db: Path) -> dict[str, LiveYield]:
    if not state_db.exists():
        return {}

    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        reads = (
            {
                str(handle).lower(): int(count)
                for handle, count in connection.execute(
                    "SELECT lower(author), COUNT(*) "
                    "FROM x_stream_reads GROUP BY lower(author)"
                )
            }
            if "x_stream_reads" in tables
            else {}
        )
        drafts = (
            {
                str(handle).lower(): int(count)
                for handle, count in connection.execute(
                    "SELECT lower(author), COUNT(*) "
                    "FROM reply_drafts GROUP BY lower(author)"
                )
            }
            if "reply_drafts" in tables
            else {}
        )
    finally:
        connection.close()

    return {
        handle: LiveYield(reads.get(handle, 0), drafts.get(handle, 0))
        for handle in reads.keys() | drafts.keys()
    }


def deterministic_jitter(handle: str, selection_date: str) -> float:
    digest = hashlib.sha256(f"{selection_date}:{handle.lower()}".encode()).digest()
    unit = int.from_bytes(digest[:8], "big") / (2**64 - 1)
    return (unit - 0.5) * 1.5


def live_yield_score(value: LiveYield) -> float:
    return min(1.0, value.posterior / (LIVE_PRIOR_RATE * 2.5))


def adjusted_score(
    target: dict[str, Any],
    live: LiveYield,
    selection_date: str,
) -> float:
    components = target.get("scoreComponents") or {}
    if "opportunity" in components:
        # REPLY-GUY-FORMULA.md: opportunity per reply leads; own yield learns.
        score = 100 * (
            0.60 * float(components.get("opportunity", 0))
            + 0.15 * float(components.get("cadence", 0))
            + 0.10 * float(components.get("nicheFit", 0))
            + 0.15 * live_yield_score(live)
        )
    elif components:
        score = 100 * (
            0.36 * float(components.get("medianViews", 0))
            + 0.18 * float(components.get("reliableViewFloor", 0))
            + 0.14 * float(components.get("cadence", 0))
            + 0.05 * float(components.get("nicheFit", 0))
            + 0.12 * float(components.get("replyCompetition", 0))
            + 0.15 * live_yield_score(live)
        )
    else:
        score = float(target.get("score", 0))
    return score + deterministic_jitter(str(target["handle"]), selection_date)


def expected_reads(target: dict[str, Any], keyword_scoped: bool = False) -> float:
    """Posts per day the stream will deliver (and bill) for this account."""
    if keyword_scoped and target.get("onTopicPostsPerDay") is not None:
        observed = float(target.get("onTopicPostsPerDay") or 0)
    else:
        observed = float(
            target.get("postsPerDay7")
            or target.get("activeWindowPostsPerDay")
            or 0
        )
    return max(0.14, observed * 1.10)


# Assumed draft rate on keyword-matched reads, for projection only; the live
# posterior per account replaces it as state.db fills.
KEYWORD_SCOPED_YIELD_ASSUMPTION = 0.40


def activate_all_accounts(
    targets: list[dict[str, Any]],
    live_by_handle: dict[str, LiveYield],
    selection_date: str,
) -> list[dict[str, Any]]:
    """Rank the full pool while leaving every account eligible during shifts."""
    ranked: list[dict[str, Any]] = []
    for target in targets:
        item = dict(target)
        handle = str(item["handle"]).lower()
        live = live_by_handle.get(handle, LiveYield())
        item["liveReads"] = live.reads
        item["liveDrafts"] = live.drafts
        item["liveDraftPosterior"] = live.posterior
        item["adjustedScore"] = adjusted_score(item, live, selection_date)
        item["expectedReadsPerDay"] = expected_reads(item)
        item["tier"] = "A"
        ranked.append(item)
    ranked.sort(key=lambda row: row["adjustedScore"], reverse=True)
    return ranked


def reply_lane_targets(
    targets: list[dict[str, Any]],
    min_median_views: float = PRIMARY_MIN_MEDIAN_VIEWS,
) -> list[dict[str, Any]]:
    """Keep only proven high-reach business-adjacent accounts.

    This is a defense-in-depth gate. Ranking output is not trusted to make a
    low-reach account safe: every live target must independently have a median
    of at least 100K views across at least three recent original posts. There
    are deliberately no secondary or volume exceptions to this contract.
    """
    def clears_reach_gate(target: dict[str, Any]) -> bool:
        median_views = float(target.get("medianViews") or 0)
        sample_posts = int(
            target.get("samplePosts")
            or target.get("sample_posts")
            or 0
        )
        return (
            median_views >= min_median_views
            and sample_posts >= MIN_REACH_SAMPLE_POSTS
        )

    return [
        target
        for target in targets
        if str(target.get("category") or "") in REPLY_LANE_CATEGORIES
        and str(target.get("handle") or "").lstrip("@").lower() not in NOT_REPLY_TARGETS
        and clears_reach_gate(target)
    ]


def select_accounts(
    targets: list[dict[str, Any]],
    live_by_handle: dict[str, LiveYield],
    daily_read_target: float,
    selection_date: str,
    keyword_scoped: bool = False,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for target in targets:
        item = dict(target)
        handle = str(item["handle"]).lower()
        live = live_by_handle.get(handle, LiveYield())
        item["liveReads"] = live.reads
        item["liveDrafts"] = live.drafts
        item["liveDraftPosterior"] = live.posterior
        item["adjustedScore"] = adjusted_score(item, live, selection_date)
        item["expectedReadsPerDay"] = expected_reads(item, keyword_scoped)
        ranked.append(item)

    ranked.sort(key=lambda row: row["adjustedScore"], reverse=True)
    selected: list[dict[str, Any]] = []
    selected_handles: set[str] = set()
    expected_total = 0.0
    category_reads: dict[str, float] = {}

    def fits(row: dict[str, Any]) -> bool:
        cost = row["expectedReadsPerDay"]
        category = str(row.get("category") or "Other")
        share = CATEGORY_READ_SHARE.get(category)
        return expected_total + cost <= daily_read_target and (
            share is None
            or category_reads.get(category, 0.0) + cost <= share * daily_read_target
        )

    def take(row: dict[str, Any]) -> None:
        nonlocal expected_total
        category = str(row.get("category") or "Other")
        selected.append(row)
        selected_handles.add(str(row["handle"]).lower())
        expected_total += row["expectedReadsPerDay"]
        category_reads[category] = category_reads.get(category, 0.0) + row["expectedReadsPerDay"]

    categories = sorted({str(row.get("category") or "Other") for row in ranked})
    category_rows = {
        category: [
            row
            for row in ranked
            if str(row.get("category") or "Other") == category
        ]
        for category in categories
    }
    for index in range(CATEGORY_MINIMUM):
        for category in categories:
            rows = category_rows[category]
            if index >= len(rows):
                continue
            row = rows[index]
            if fits(row):
                take(row)

    for row in ranked:
        if str(row["handle"]).lower() in selected_handles:
            continue
        if fits(row):
            take(row)

    selected.sort(key=lambda row: row["adjustedScore"], reverse=True)
    if not selected:
        raise ValueError("daily target selector produced an empty target set")
    return selected


def assign_tiers(
    selected: list[dict[str, Any]],
    daily_generation_target: float,
) -> None:
    for row in selected:
        row["tier"] = "E"

    expected_generations = sum(
        row["expectedReadsPerDay"] * TIER_WEIGHTS["E"] for row in selected
    )
    for row in selected:
        for next_tier in TIER_ORDER[1:]:
            current_tier = str(row["tier"])
            added = row["expectedReadsPerDay"] * (
                TIER_WEIGHTS[next_tier] - TIER_WEIGHTS[current_tier]
            )
            if expected_generations + added > daily_generation_target:
                break
            row["tier"] = next_tier
            expected_generations += added


# A selection under half of the live target list is refused unless
# --allow-shrink is passed (Oct 20 2026: a bad pool rebuild left 5 accounts,
# and a working selector would have cut the stream from 67 targets to 5).
MAX_SHRINK = 0.5


def collapse_refusal(current: int, selected: int) -> str | None:
    if current >= 10 and selected < current * MAX_SHRINK:
        return (f"refusing to cut the live targets from {current} to {selected}; "
                "check the pool, or pass --allow-shrink")
    return None


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def build_report(
    pool: dict[str, Any],
    selected: list[dict[str, Any]],
    daily_read_capacity: int,
    daily_read_target: float,
    daily_generation_target: float,
    selection_date: str,
    config_changed: bool,
    daily_goal: int = 50,
    keyword_scoped: bool = False,
    min_median_views: float = PRIMARY_MIN_MEDIAN_VIEWS,
) -> dict[str, Any]:
    expected_market_reads = sum(
        row["expectedReadsPerDay"] for row in selected
    )
    expected_read_count = min(daily_read_capacity, expected_market_reads)
    expected_generations = min(
        daily_generation_target,
        expected_read_count,
    )
    evidence_point_estimate = sum(
        row["expectedReadsPerDay"] * row["liveDraftPosterior"]
        for row in selected
    )
    required_yield = (
        daily_goal / daily_read_capacity if daily_read_capacity else 1.0
    )
    return {
        "selectionDate": selection_date,
        "configChanged": config_changed,
        "masterPoolSize": len(pool.get("targets") or []),
        "activeTargetCount": len(selected),
        "dailyPaidReadCapacity": daily_read_capacity,
        "dailyPaidReadTarget": daily_read_target,
        "expectedPaidReads": round(expected_read_count, 2),
        "expectedMarketReadsBeforeCap": round(expected_market_reads, 2),
        "dailyGenerationTarget": daily_generation_target,
        "expectedGenerationCalls": round(expected_generations, 2),
        "hardReachGate": {
            "minimumMedianViews": min_median_views,
            "minimumSamplePosts": MIN_REACH_SAMPLE_POSTS,
            "categories": sorted(REPLY_LANE_CATEGORIES),
            "categoryReadShare": CATEGORY_READ_SHARE,
            "rejectedFromMasterPool": len(pool.get("targets") or []) - len(selected),
        },
        "categoryReads": {
            category: round(sum(
                row["expectedReadsPerDay"]
                for row in selected
                if str(row.get("category") or "Other") == category
            ), 2)
            for category in sorted(
                {str(row.get("category") or "Other") for row in selected}
            )
        },
        "keywordScopedRules": keyword_scoped,
        "projectedStrongDrafts": {
            "evidencePointEstimate": round(evidence_point_estimate, 2),
            "atKeywordScopedYieldAssumption": (
                round(expected_read_count * KEYWORD_SCOPED_YIELD_ASSUMPTION, 2)
                if keyword_scoped
                else None
            ),
            "observedBaselineRate": round(LIVE_PRIOR_RATE, 4),
            "dailyGoal": daily_goal,
            "requiredAcceptanceRate": round(required_yield, 4),
            "note": (
                "The daily goal is the approval ceiling, not a statistical "
                "projection. The selector monitors only the measured "
                "high-reach pool; the paid-read and draft caps remain hard."
            ),
        },
        "categoryCounts": {
            category: sum(
                1
                for row in selected
                if str(row.get("category") or "Other") == category
            )
            for category in sorted(
                {str(row.get("category") or "Other") for row in selected}
            )
        },
        "tiers": {
            tier: sum(1 for row in selected if row["tier"] == tier)
            for tier in reversed(TIER_ORDER)
        },
        "targets": [
            {
                "handle": row["handle"],
                "tier": row["tier"],
                "category": row.get("category"),
                "source": row.get("source"),
                "adjustedScore": round(row["adjustedScore"], 2),
                "expectedReadsPerDay": round(row["expectedReadsPerDay"], 2),
                "medianViews": round(float(row.get("medianViews") or 0)),
                "postsPerDay7": row.get("postsPerDay7"),
                "liveReads": row["liveReads"],
                "liveDrafts": row["liveDrafts"],
                "liveDraftPosterior": round(row["liveDraftPosterior"], 4),
            }
            for row in selected
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pool",
        type=Path,
        default=Path("research/output/pentra-x-reply-targets.json"),
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state-db", type=Path, default=Path("state.db"))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("research/output/active-x-reply-targets.json"),
    )
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).date().isoformat(),
    )
    parser.add_argument("--read-utilization", type=float, default=1.0)
    parser.add_argument("--daily-generation-target", type=float, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-shrink", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pool = json.loads(args.pool.read_text())
    config = json.loads(args.config.read_text())
    x_budget = config["x_detection"]
    daily_read_capacity = int(
        x_budget["daily_microusd_cap"] // x_budget["post_read_microusd"]
    )
    daily_read_target = daily_read_capacity * args.read_utilization
    live_by_handle = load_live_yield(args.state_db)

    keyword_scoped = bool(x_budget.get("original_rule_keywords"))
    min_median = max(float(MIN_REACH_FLOOR), float(
        pool.get("reachFloor")
        or x_budget.get("target_min_median_views")
        or PRIMARY_MIN_MEDIAN_VIEWS
    ))
    selected = select_accounts(
        reply_lane_targets(pool["targets"], min_median),
        live_by_handle,
        daily_read_target,
        args.date,
        keyword_scoped=keyword_scoped,
    )
    refusal = None if args.allow_shrink else collapse_refusal(
        len(config.get("targets") or []), len(selected)
    )
    if refusal:
        raise SystemExit(refusal)
    for row in selected:
        row["tier"] = "A"
    next_targets = [
        {"handle": row["handle"], "tier": row["tier"]} for row in selected
    ]
    config_changed = config.get("targets") != next_targets
    report = build_report(
        pool,
        selected,
        daily_read_capacity,
        daily_read_target,
        args.daily_generation_target,
        args.date,
        config_changed,
        daily_goal=int(
            (config.get("human_approval") or {}).get("daily_candidate_cap") or 50
        ),
        keyword_scoped=keyword_scoped,
        min_median_views=min_median,
    )
    atomic_write_json(args.report, report)

    if not args.dry_run:
        if config_changed:
            config["targets"] = next_targets
            atomic_write_json(args.config, config)

    print(json.dumps({key: value for key, value in report.items() if key != "targets"}))


if __name__ == "__main__":
    main()
