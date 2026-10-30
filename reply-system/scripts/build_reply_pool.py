#!/usr/bin/env python3
"""Turn research nominations plus official-X audit output into the reply pool.

No network and no spend: this only reads files.

  handles  print the candidate handles still worth auditing (minus exclusions)
  build    merge candidates + audit outputs into the selector's pool schema
  merge    add a new audit to the existing pool (use this after an audit: most
           measurements live only in the pool, so build would drop them)

Every pool target is measured, never assumed: an account enters the pool only
with an audit row, and the selector independently re-checks the 100K median /
3-post reach gate. Accounts watched by another brand the same owner runs are
excluded, so two accounts operated by one person never converge on the same
conversations.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.reply_formula import (  # noqa: E402
    ReplyFormula,
    choose_reach_floor,
    gate_failure,
    opportunity,
    opportunity_component,
)
from bot.topics import matches_keywords  # noqa: E402
from scripts.select_x_reply_targets import (  # noqa: E402
    MIN_REACH_SAMPLE_POSTS,
    PRIMARY_MIN_MEDIAN_VIEWS,
    REPLY_LANE_CATEGORIES,
    atomic_write_json,
)


HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# Below this share of on-topic original posts, a big account is mostly a
# stream of posts Pentra has nothing useful to add to; reads would be waste.
MIN_NICHE_FIT = 0.35
# Above this share of politics/finance/sport posts the account is off-lane.
MAX_FORBIDDEN_RATIO = 0.2
CADENCE_SWEET_SPOT = 3.0    # original posts/day that give steady openings
CADENCE_FIREHOSE = 15.0     # beyond this most reads never become drafts


def _key(handle: str) -> str:
    return handle.lstrip("@").strip().casefold()


def load_candidates(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text())
    out: dict[str, dict[str, Any]] = {}
    for row in raw.get("candidates") or []:
        handle = str(row.get("handle") or "").lstrip("@").strip()
        category = str(row.get("category") or "")
        if not HANDLE_RE.match(handle):
            raise ValueError(f"invalid handle in candidates: {handle!r}")
        if category not in REPLY_LANE_CATEGORIES:
            raise ValueError(f"{handle}: category {category!r} is not a reply lane")
        if _key(handle) in out:
            raise ValueError(f"duplicate candidate handle: {handle}")
        out[_key(handle)] = {**row, "handle": handle, "category": category}
    return out


def load_exclusions(paths: list[Path]) -> set[str]:
    """Handles to keep out: a plain list, or another service's config.json."""
    excluded: set[str] = set()
    for path in paths:
        text = path.read_text()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            excluded.update(
                _key(line) for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            continue
        rows = raw.get("targets", []) if isinstance(raw, dict) else raw
        for row in rows:
            handle = row.get("handle") if isinstance(row, dict) else row
            if handle:
                excluded.add(_key(str(handle)))
    return excluded


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def posts_per_day(row: dict[str, Any], audit: dict[str, Any]) -> float:
    """Original posts per day across the audit window.

    When a handle filled its query page, the sample is truncated: the rate is
    then measured over the span the returned posts actually cover.
    """
    if row.get("posts_per_day") is not None:
        return round(float(row["posts_per_day"]), 3)
    posts = row.get("posts") or []
    start = _parse_time(str(audit.get("window_start") or ""))
    end = _parse_time(str(audit.get("window_end") or ""))
    if not posts or not start or not end or end <= start:
        return 0.0
    window_days = (end - start).total_seconds() / 86_400
    page = int(audit.get("max_results") or 0)
    truncated = audit.get("mode") == "per_handle" and page and len(posts) >= page
    if truncated:
        times = [t for t in (_parse_time(p.get("created_at", "")) for p in posts) if t]
        if times:
            span_days = (end - min(times)).total_seconds() / 86_400
            window_days = max(span_days, 1 / 24)
    return round(len(posts) / window_days, 3)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def on_topic_share(row: dict[str, Any], keywords: list[str]) -> float:
    """Share of sampled originals a keyword-scoped stream rule would deliver."""
    posts = row.get("posts") or []
    if not keywords:
        return 1.0
    if not posts:
        return 0.0
    hits = sum(matches_keywords(str(p.get("text") or ""), keywords) for p in posts)
    return round(hits / len(posts), 4)


def score_components(
    row: dict[str, Any],
    ppd: float,
    gate: float = PRIMARY_MIN_MEDIAN_VIEWS,
) -> dict[str, float]:
    views = sorted(int(p.get("views") or 0) for p in row.get("posts") or [])
    median = float(row.get("median_views") or 0)
    # The reach gate scores 0; 50x the gate scores 1, on a log scale.
    median_score = _clamp(
        (math.log10(max(median, 1)) - math.log10(gate)) / math.log10(50)
    )
    if views:
        floor = statistics.quantiles(views, n=4)[0] if len(views) >= 4 else views[0]
    else:
        floor = 0
    floor_score = _clamp(floor / gate)
    if ppd <= 0:
        cadence = 0.0
    elif ppd <= CADENCE_SWEET_SPOT:
        cadence = ppd / CADENCE_SWEET_SPOT
    else:
        cadence = _clamp(
            1 - (ppd - CADENCE_SWEET_SPOT) / (CADENCE_FIREHOSE - CADENCE_SWEET_SPOT)
        ) * 0.75 + 0.25
    replies = float(row.get("median_replies") or 0)
    # Fewer competing replies means a good reply is more likely to be seen.
    competition = _clamp(1 - math.log10(1 + replies) / 3)
    return {
        "medianViews": round(median_score, 4),
        "reliableViewFloor": round(floor_score, 4),
        "cadence": round(cadence, 4),
        "nicheFit": round(float(row.get("relevant_original_ratio") or 0), 4),
        "replyCompetition": round(competition, 4),
    }


def rejection_reason(row: dict[str, Any], gate: float = PRIMARY_MIN_MEDIAN_VIEWS) -> str:
    samples = int(row.get("sample_posts") or 0)
    if samples < MIN_REACH_SAMPLE_POSTS:
        return f"only {samples} original posts in the audit window"
    if float(row.get("median_views") or 0) < gate:
        return f"median views {round(float(row.get('median_views') or 0))} < {int(gate)}"
    forbidden = int(row.get("forbidden_posts") or 0) / samples
    if forbidden > MAX_FORBIDDEN_RATIO:
        return f"off-lane share {forbidden:.2f} > {MAX_FORBIDDEN_RATIO}"
    if float(row.get("relevant_original_ratio") or 0) < MIN_NICHE_FIT:
        return (
            f"on-topic share {float(row.get('relevant_original_ratio') or 0):.2f}"
            f" < {MIN_NICHE_FIT}"
        )
    return ""


def build_pool(
    candidates: dict[str, dict[str, Any]],
    audits: list[dict[str, Any]],
    excluded: set[str],
    now: datetime | None = None,
    keywords: list[str] | None = None,
    min_median_views: float = PRIMARY_MIN_MEDIAN_VIEWS,
) -> dict[str, Any]:
    keywords = list(keywords or [])
    gate = float(min_median_views)
    # The newest measurement of a handle wins.
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for audit in sorted(audits, key=lambda a: str(a.get("generated_at") or "")):
        for row in audit.get("rows") or []:
            latest[_key(str(row.get("handle") or ""))] = (row, audit)

    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    unmeasured: list[str] = []
    for key, candidate in sorted(candidates.items()):
        handle = candidate["handle"]
        if key in excluded:
            rejected.append({"handle": handle, "reason": "excluded: watched by another owned account"})
            continue
        if key not in latest:
            unmeasured.append(handle)
            continue
        row, audit = latest[key]
        reason = rejection_reason(row, gate)
        if reason:
            rejected.append({"handle": handle, "reason": reason})
            continue
        ppd = posts_per_day(row, audit)
        share = on_topic_share(row, keywords)
        on_topic_ppd = round(ppd * share, 3)
        if keywords and on_topic_ppd <= 0:
            rejected.append({
                "handle": handle,
                "reason": "no sampled post matches the stream keywords",
            })
            continue
        # With keyword-scoped rules, opportunity = posts the stream delivers.
        components = score_components(row, on_topic_ppd if keywords else ppd, gate)
        targets.append({
            "handle": handle,
            "category": candidate["category"],
            "medianViews": float(row["median_views"]),
            "samplePosts": int(row["sample_posts"]),
            "medianReplies": float(row.get("median_replies") or 0),
            "postsPerDay7": ppd,
            "onTopicShare": share,
            "onTopicPostsPerDay": on_topic_ppd,
            "score": round(100 * (
                0.36 * components["medianViews"]
                + 0.18 * components["reliableViewFloor"]
                + 0.14 * components["cadence"]
                + 0.05 * components["nicheFit"]
                + 0.12 * components["replyCompetition"]
            ), 2),
            "scoreComponents": components,
            "measuredAt": row.get("measured_at") or audit.get("generated_at"),
            "why": candidate.get("why", ""),
            "source": candidate.get("source", ""),
        })
    targets.sort(key=lambda t: t["score"], reverse=True)
    return {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "method": (
            "research nominations measured with the official X recent-search "
            f"audit; reach gate median >= {int(gate)} over >= 3 original posts"
        ),
        "keywordScoped": bool(keywords),
        "counts": {
            "candidates": len(candidates),
            "onTopicPostsPerDay": round(sum(t["onTopicPostsPerDay"] for t in targets), 2),
            "targets": len(targets),
            "rejected": len(rejected),
            "unmeasured": len(unmeasured),
            "byCategory": {
                category: sum(t["category"] == category for t in targets)
                for category in sorted(REPLY_LANE_CATEGORIES)
            },
        },
        "targets": targets,
        "rejected": rejected,
        "unmeasured": unmeasured,
    }


def formula_entry(
    candidate: dict[str, Any],
    row: dict[str, Any],
    audit: dict[str, Any],
    keywords: list[str],
    formula: ReplyFormula,
) -> tuple[dict[str, Any] | None, str]:
    """One audited account under REPLY-GUY-FORMULA.md's gates: (pool target,
    "") or (None, why it was rejected)."""
    samples = int(row.get("sample_posts") or 0)
    ppd = posts_per_day(row, audit)
    share = on_topic_share(row, keywords)
    off_lane = int(row.get("forbidden_posts") or 0) / samples if samples else 1.0
    reason = gate_failure(
        sample_posts=samples, posts_per_day=ppd, on_topic_share=share,
        off_lane_share=off_lane, formula=formula,
    )
    if not reason and float(row.get("median_views") or 0) < formula.lowest_floor:
        reason = f"median views {int(float(row.get('median_views') or 0))} below {int(formula.lowest_floor)}"
    if reason:
        return None, reason
    on_topic_ppd = round(ppd * share, 3)
    opp = opportunity(float(row["median_views"]), float(row.get("median_replies") or 0), formula)
    components = score_components(row, on_topic_ppd, formula.lowest_floor)
    components["opportunity"] = round(opportunity_component(opp), 4)
    components["nicheFit"] = round(share, 4)
    return {
        "handle": candidate["handle"],
        "category": candidate["category"],
        "medianViews": float(row["median_views"]),
        "samplePosts": samples,
        "medianReplies": float(row.get("median_replies") or 0),
        "postsPerDay7": ppd,
        "onTopicShare": share,
        "onTopicPostsPerDay": on_topic_ppd,
        "opportunityPerReply": round(opp),
        "dailyOpportunity": round(opp * on_topic_ppd),
        "score": round(100 * components["opportunity"], 2),
        "scoreComponents": components,
        "measuredAt": row.get("measured_at") or audit.get("generated_at"),
        "why": candidate.get("why", ""),
        "source": candidate.get("source", ""),
    }, ""


def merge_into_pool(
    pool: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    audits: list[dict[str, Any]],
    excluded: set[str],
    keywords: list[str],
    formula: ReplyFormula,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add newly audited accounts to the existing pool and keep every other
    entry as it is.

    Oct 20 2026: a full rebuild from the audit files on the server cut the pool
    from 155 targets to 5, because most measurements (the August audits) live
    only in the pool itself. Only the handles in these audits are judged again,
    with the same gates and the pool's own reach floor."""
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for audit in sorted(audits, key=lambda a: str(a.get("generated_at") or "")):
        for row in audit.get("rows") or []:
            latest[_key(str(row.get("handle") or ""))] = (row, audit)
    floor = max(float(pool.get("reachFloor") or 0), formula.lowest_floor)
    targets = [t for t in pool.get("targets") or [] if _key(str(t["handle"])) not in latest]
    rejected = [r for r in pool.get("rejected") or [] if _key(str(r["handle"])) not in latest]
    unmeasured = [h for h in pool.get("unmeasured") or [] if _key(str(h)) not in latest]
    added: list[str] = []
    for key, (row, audit) in sorted(latest.items()):
        candidate = candidates.get(key)
        if candidate is None:
            continue  # only research nominations enter the pool
        if key in excluded:
            rejected.append({"handle": candidate["handle"], "reason": "excluded (other brand or off-lane list)"})
            continue
        entry, reason = formula_entry(candidate, row, audit, keywords, formula)
        if entry is not None and entry["medianViews"] < floor:
            entry, reason = None, f"below today's reach floor {int(floor)}"
        if entry is None:
            rejected.append({"handle": candidate["handle"], "reason": reason})
            continue
        targets.append(entry)
        added.append(candidate["handle"])
    targets.sort(key=lambda t: float(t.get("opportunityPerReply") or 0), reverse=True)
    return {
        **pool,
        "merged_at": (now or datetime.now(timezone.utc)).isoformat(),
        "merged_added": added,
        "counts": {
            **(pool.get("counts") or {}),
            "candidates": len(candidates),
            "onTopicPostsPerDay": round(sum(float(t.get("onTopicPostsPerDay") or 0) for t in targets), 2),
            "targets": len(targets),
            "rejected": len(rejected),
            "unmeasured": len(unmeasured),
            "byCategory": {
                category: sum(t.get("category") == category for t in targets)
                for category in sorted(REPLY_LANE_CATEGORIES)
            },
        },
        "targets": targets,
        "rejected": rejected,
        "unmeasured": unmeasured,
    }


def build_formula_pool(
    candidates: dict[str, dict[str, Any]],
    audits: list[dict[str, Any]],
    excluded: set[str],
    keywords: list[str],
    formula: ReplyFormula,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply REPLY-GUY-FORMULA.md: gates, highest workable reach floor, ranking."""
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for audit in sorted(audits, key=lambda a: str(a.get("generated_at") or "")):
        for row in audit.get("rows") or []:
            latest[_key(str(row.get("handle") or ""))] = (row, audit)

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    unmeasured: list[str] = []
    for key, candidate in sorted(candidates.items()):
        handle = candidate["handle"]
        if key in excluded:
            rejected.append({"handle": handle, "reason": "excluded (other brand or off-lane list)"})
            continue
        if key not in latest:
            unmeasured.append(handle)
            continue
        row, audit = latest[key]
        entry, reason = formula_entry(candidate, row, audit, keywords, formula)
        if entry is None:
            rejected.append({"handle": handle, "reason": reason})
            continue
        eligible.append(entry)

    decision = choose_reach_floor(
        ((t["medianViews"], t["onTopicPostsPerDay"]) for t in eligible), formula,
    )
    targets = [t for t in eligible if t["medianViews"] >= decision["floor"]]
    for t in eligible:
        if t["medianViews"] < decision["floor"]:
            rejected.append({
                "handle": t["handle"],
                "reason": f"below today's reach floor {int(decision['floor'])}",
            })
    targets.sort(key=lambda t: t["opportunityPerReply"], reverse=True)
    views = [t["medianViews"] for t in targets]
    return {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "method": "REPLY-GUY-FORMULA.md: gates, highest reach floor meeting the volume target, ranked by opportunity per reply",
        "keywordScoped": bool(keywords),
        "reachFloor": decision["floor"],
        "formula": {
            **decision,
            "dailyDraftTarget": formula.daily_draft_target,
            "assumedDraftRate": formula.assumed_draft_rate,
            "averageMedianViews": round(sum(views) / len(views)) if views else 0,
        },
        "counts": {
            "candidates": len(candidates),
            "onTopicPostsPerDay": round(sum(t["onTopicPostsPerDay"] for t in targets), 2),
            "targets": len(targets),
            "rejected": len(rejected),
            "unmeasured": len(unmeasured),
            "byCategory": {
                category: sum(t["category"] == category for t in targets)
                for category in sorted(REPLY_LANE_CATEGORIES)
            },
        },
        "targets": targets,
        "rejected": rejected,
        "unmeasured": unmeasured,
    }


def audited_handles(audits: list[dict[str, Any]]) -> set[str]:
    return {
        _key(str(row.get("handle") or ""))
        for audit in audits
        for row in audit.get("rows") or []
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("handles", "build", "merge"):
        cmd = sub.add_parser(name)
        cmd.add_argument(
            "--candidates",
            type=Path,
            default=Path("research/candidates/pentra-candidates.json"),
        )
        cmd.add_argument("--audit", type=Path, action="extend", nargs="+", default=[])
        cmd.add_argument(
            "--exclude",
            type=Path,
            action="extend",
            nargs="+",
            default=[],
            help="Handle list or another service's config.json to keep out.",
        )
    for name in ("build", "merge"):
        sub.choices[name].add_argument(
            "--config",
            type=Path,
            default=Path("config.json"),
            help="Reads x_detection.original_rule_keywords for the on-topic estimate.",
        )
    sub.choices["build"].add_argument(
        "--output",
        type=Path,
        default=Path("research/output/pentra-x-reply-targets.json"),
    )
    sub.choices["merge"].add_argument(
        "--pool",
        type=Path,
        default=Path("research/output/pentra-x-reply-targets.json"),
    )
    sub.choices["merge"].add_argument("--output", type=Path, default=None, help="Defaults to --pool.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates = load_candidates(args.candidates)
    audits = [json.loads(path.read_text()) for path in args.audit]
    excluded = load_exclusions(args.exclude)
    if args.command == "handles":
        done = audited_handles(audits)
        for key, candidate in sorted(candidates.items()):
            if key not in excluded and key not in done:
                print(candidate["handle"])
        return
    keywords: list[str] = []
    gate = float(PRIMARY_MIN_MEDIAN_VIEWS)
    raw_cfg: dict[str, Any] = {}
    if args.config.exists():
        raw_cfg = json.loads(args.config.read_text())
        x_cfg = raw_cfg.get("x_detection") or {}
        keywords = list(x_cfg.get("original_rule_keywords") or [])
        gate = float(x_cfg.get("target_min_median_views") or gate)
    denylist = Path("research/candidates/denylist.txt")
    if denylist.exists():
        excluded |= load_exclusions([denylist])
    if args.command == "merge":
        merged = merge_into_pool(
            json.loads(args.pool.read_text()), candidates, audits, excluded, keywords,
            ReplyFormula.from_config(raw_cfg.get("reply_formula")),
        )
        atomic_write_json(args.output or args.pool, merged)
        print(json.dumps({**merged["counts"], "added": merged["merged_added"]}))
        return
    if raw_cfg.get("reply_formula"):
        pool = build_formula_pool(
            candidates, audits, excluded, keywords,
            ReplyFormula.from_config(raw_cfg["reply_formula"]),
        )
    else:
        pool = build_pool(candidates, audits, excluded, keywords=keywords, min_median_views=gate)
    atomic_write_json(args.output, pool)
    print(json.dumps({**pool["counts"], **({"formula": pool["formula"]} if "formula" in pool else {})}))


if __name__ == "__main__":
    main()
