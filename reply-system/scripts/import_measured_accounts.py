#!/usr/bin/env python3
"""Import already-paid-for reach measurements into Pentra's pool inputs ($0).

Reads reach-audit JSON files (rows with handle, median_views, sample_posts and
sample_originals[{created_at, views, replies, text}]) and writes:

- an audit file in the format build_reply_pool.py reads, with each account's
  measured posts per day, and
- a candidates file that merges the existing seed list with every imported
  account that passes the reply-guy formula (see REPLY-GUY-FORMULA.md).

Nothing here calls X. Accounts listed in --exclude (another brand's live
targets) are dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.topics import matches_keywords  # noqa: E402
from scripts.audit_x_target_reach_official import (  # noqa: E402
    FORBIDDEN_RE,
    TOPICAL_RE,
    URL_RE,
)
from scripts.build_reply_pool import load_exclusions  # noqa: E402

CATEGORY_TERMS = {
    "SEO and content": r"seo|search|content|blog|newsletter|writing|writer|copywriting|audience",
    "AI and automation": r"ai|llm|gpt|claude|agents?|models?|openai|anthropic|automation|ml",
    "SaaS and founders": r"saas|founders?|startups?|bootstrapp\w*|indie|mrr|arr|yc|vc|investors?|ceo",
    "Sales and marketing": r"marketing|marketer|sales|brand|ads|growth|creators?|copy",
    "Product and growth": r"product|design|designer|pm|ux|ship|launch",
}
CATEGORY_RE = {k: re.compile(rf"\b(?:{v})\b", re.I) for k, v in CATEGORY_TERMS.items()}


def _time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def category_for(bio: str, texts: list[str]) -> str:
    blob = " ".join([bio or ""] + texts)
    scores = {k: len(rx.findall(blob)) for k, rx in CATEGORY_RE.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "General business"


def posts_per_day(row: dict[str, Any], generated_at: str) -> float:
    if row.get("original_posts_per_day_7d") is not None:
        return float(row["original_posts_per_day_7d"])
    posts = row.get("sample_originals") or []
    times = [t for t in (_time(p.get("created_at")) for p in posts) if t]
    end = _time(generated_at) or datetime.now(timezone.utc)
    if not times:
        return 0.0
    span_days = max(1.0, (end - min(times)).total_seconds() / 86_400)
    return round(len(times) / span_days, 3)


def convert_row(row: dict[str, Any], generated_at: str) -> dict[str, Any] | None:
    samples = row.get("sample_originals") or []
    if not isinstance(samples, list) or not samples:
        return None
    posts = []
    for p in samples:
        text = str(p.get("text") or "")
        clean = URL_RE.sub("", text).strip()
        posts.append({
            "created_at": str(p.get("created_at") or ""),
            "views": int(p.get("views") or 0),
            "replies": int(p.get("replies") or 0),
            "text": text,
            "topical": bool(TOPICAL_RE.search(clean)),
            "forbidden": bool(FORBIDDEN_RE.search(clean)),
            "link_only": not clean,
        })
    views = [p["views"] for p in posts]
    relevant = [p for p in posts if p["topical"] and not p["forbidden"] and not p["link_only"]]
    return {
        "handle": str(row["handle"]),
        "sample_posts": len(posts),
        "median_views": float(row.get("median_views") or statistics.median(views)),
        "median_replies": float(
            row.get("median_replies")
            if row.get("median_replies") is not None
            else statistics.median([p["replies"] for p in posts])
        ),
        "forbidden_posts": sum(p["forbidden"] for p in posts),
        "relevant_original_ratio": round(len(relevant) / len(posts), 4),
        "posts_per_day": posts_per_day(row, generated_at),
        "bio": str(row.get("bio") or ""),
        "measured_at": generated_at,
        "posts": [
            {k: p[k] for k in ("created_at", "views", "replies", "text")}
            for p in posts
        ],
    }


def import_rows(
    audits: list[dict[str, Any]],
    excluded: set[str],
) -> list[dict[str, Any]]:
    """Merge every measurement of a handle (posts de-duplicated).

    A single audit samples only 3-5 posts; pooling all of them gives a steadier
    median and a fairer read of what the account posts about.
    """
    merged: dict[str, dict[str, Any]] = {}
    for audit in sorted(audits, key=lambda a: str(a.get("generated_at") or "")):
        generated_at = str(audit.get("generated_at") or "")
        for row in audit.get("rows") or []:
            if not isinstance(row, dict) or not row.get("handle"):
                continue
            key = str(row["handle"]).lstrip("@").casefold()
            if key in excluded:
                continue
            converted = convert_row(row, generated_at)
            if not converted:
                continue
            prior = merged.get(key)
            if prior is None:
                merged[key] = converted
                continue
            # The same post measured twice keeps its later (higher) view count.
            by_post = {(p["created_at"], p["text"][:60]): p for p in prior["posts"]}
            for post in converted["posts"]:
                by_post[(post["created_at"], post["text"][:60])] = post
            posts = list(by_post.values())
            views = [p["views"] for p in posts]
            clean = [URL_RE.sub("", p["text"]).strip() for p in posts]
            relevant = sum(
                bool(TOPICAL_RE.search(c)) and not FORBIDDEN_RE.search(c) and bool(c)
                for c in clean
            )
            merged[key] = {
                **converted,
                "sample_posts": len(posts),
                "median_views": float(statistics.median(views)),
                "median_replies": float(statistics.median([p["replies"] for p in posts])),
                "forbidden_posts": sum(bool(FORBIDDEN_RE.search(c)) for c in clean),
                "relevant_original_ratio": round(relevant / len(posts), 4),
                "posts_per_day": max(prior["posts_per_day"], converted["posts_per_day"]),
                "bio": converted["bio"] or prior["bio"],
                "posts": posts,
            }
    return list(merged.values())


def formula_filter(
    rows: list[dict[str, Any]],
    keywords: list[str],
    *,
    min_median: float,
    min_posts_per_day: float,
    min_on_topic: float,
) -> list[dict[str, Any]]:
    kept = []
    for row in rows:
        if row["sample_posts"] < 3 or row["median_views"] < min_median:
            continue
        if row["posts_per_day"] < min_posts_per_day:
            continue
        texts = [p["text"] for p in row["posts"]]
        share = sum(matches_keywords(t, keywords) for t in texts) / len(texts)
        if share < min_on_topic:
            continue
        if row["forbidden_posts"] / row["sample_posts"] > 0.2:
            continue
        kept.append(row)
    return kept


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audits", nargs="+", type=Path)
    parser.add_argument("--exclude", type=Path, action="extend", nargs="+", default=[])
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--seed", type=Path, default=Path("research/candidates/pentra-candidates-seed.json"))
    parser.add_argument("--out-audit", type=Path, default=Path("research/measured/imported-measurements.json"))
    parser.add_argument("--out-candidates", type=Path, default=Path("research/candidates/pentra-candidates.json"))
    parser.add_argument("--min-posts-per-day", type=float, default=None)
    parser.add_argument("--min-on-topic", type=float, default=None)
    args = parser.parse_args()

    raw_cfg = json.loads(args.config.read_text())
    x_cfg = raw_cfg.get("x_detection") or {}
    f_cfg = raw_cfg.get("reply_formula") or {}
    if args.min_posts_per_day is None:
        args.min_posts_per_day = float(f_cfg.get("min_posts_per_day", 0.5))
    if args.min_on_topic is None:
        args.min_on_topic = float(f_cfg.get("min_on_topic_share", 0.3))
    keywords = list(x_cfg.get("original_rule_keywords") or [])
    gate = float(x_cfg.get("target_min_median_views") or 100_000)
    audits = []
    for path in args.audits:
        try:
            audits.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    rows = import_rows(audits, load_exclusions(args.exclude))
    kept = formula_filter(
        rows, keywords,
        min_median=gate,
        min_posts_per_day=args.min_posts_per_day,
        min_on_topic=args.min_on_topic,
    )
    now = datetime.now(timezone.utc).isoformat()
    args.out_audit.parent.mkdir(parents=True, exist_ok=True)
    args.out_audit.write_text(json.dumps({
        "generated_at": now,
        "source": "imported prior reach measurements; no X reads",
        "mode": "imported",
        "rows": kept,
    }, indent=1) + "\n")

    seed = json.loads(args.seed.read_text())
    merged = {c["handle"].casefold(): c for c in seed["candidates"]}
    for row in kept:
        key = row["handle"].casefold()
        if key in merged:
            continue
        merged[key] = {
            "handle": row["handle"],
            "category": category_for(row["bio"], [p["text"] for p in row["posts"]]),
            "why": f"measured median {int(row['median_views'])} views/post",
            "source": f"prior reach measurement {row['measured_at'][:10]}",
        }
    args.out_candidates.parent.mkdir(parents=True, exist_ok=True)
    args.out_candidates.write_text(json.dumps({
        "generated_at": now,
        "method": "seed nominations plus accounts from prior reach measurements that pass the formula",
        "candidates": list(merged.values()),
    }, indent=1) + "\n")
    print(json.dumps({
        "measured_accounts": len(rows),
        "pass_formula": len(kept),
        "candidates_total": len(merged),
        "median_of_medians": int(statistics.median([r["median_views"] for r in kept])) if kept else 0,
        "on_topic_posts_per_day_est": round(sum(
            r["posts_per_day"] * sum(matches_keywords(p["text"], keywords) for p in r["posts"]) / len(r["posts"])
            for r in kept
        ), 1),
    }))


if __name__ == "__main__":
    main()
