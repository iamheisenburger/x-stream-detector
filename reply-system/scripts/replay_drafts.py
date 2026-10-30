#!/usr/bin/env python3
"""Re-run the reply writer on captured real posts, to test prompt changes.

Reads the JSONL written by human_approval.capture_posts_path, runs the same
generate.decide call production uses (human review mode, hostile fact check),
and reports how many posts become drafts, why the rest are skipped, and the
drafts themselves. Every call goes through the metered client, so the daily
reply generation cap still applies. Nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import generate  # noqa: E402
from bot.llm_budget import GenerationBudgetOpen  # noqa: E402


def load_posts(path: Path, limit: int, originals_only: bool = True) -> list[dict]:
    posts, seen = [], set()
    for line in path.read_text().splitlines():
        try:
            post = json.loads(line)
        except json.JSONDecodeError:
            continue
        if post.get("id") in seen or (originals_only and post.get("is_reply")):
            continue
        seen.add(post.get("id"))
        posts.append(post)
    return posts[-limit:]


def summarize(results: list[dict]) -> dict:
    drafted = [r for r in results if r.get("action") == "reply"]
    reasons = Counter(
        str(r.get("reason") or r.get("error") or "")[:70]
        for r in results if r.get("action") != "reply"
    )
    return {
        "posts": len(results),
        "drafts": len(drafted),
        "draft_rate": round(len(drafted) / len(results), 3) if results else 0.0,
        "top_skip_reasons": reasons.most_common(8),
    }


def replay(cfg, client, posts: list[dict], variants: list[str], decide=None,
           carry_recent: bool = False, keep_trace: bool = False) -> list[dict]:
    """Run each post through every prompt variant, interleaved per post, so a
    budget stop leaves comparable pairs rather than one finished variant.

    carry_recent: each variant sees its own last 12 drafts as the account's
    recent replies, as production does (the repeated-frame guard needs them).
    keep_trace: store each decision's trace (writer output, repairs, audit)."""
    decide = decide or generate.decide
    results = []
    recent: dict[str, list[str]] = {variant: [] for variant in variants}
    original = getattr(cfg, "reply_prompt_version", "v1")
    try:
        for post in posts:
            for variant in variants:
                cfg.reply_prompt_version = variant
                row = {"id": post["id"], "author": post["author"], "variant": variant,
                       "source": post["text"][:400]}
                trace = {} if keep_trace else None
                try:
                    decision = decide(
                        cfg, post["text"], post["author"], client,
                        recent_replies=recent[variant][-12:] if carry_recent else [],
                        trace=trace, secondary_audit=True, human_review=True,
                    )
                    row.update({k: decision.get(k) for k in ("action", "text", "reason")})
                    if decision.get("action") == "reply":
                        recent[variant].append(str(decision.get("text") or ""))
                    if trace is not None:
                        row["trace"] = trace
                except GenerationBudgetOpen as exc:
                    row.update({"action": "budget_stop", "error": str(exc)})
                    results.append(row)
                    return results
                except Exception as exc:  # one bad post must not end the replay
                    row.update({"action": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
                results.append(row)
    finally:
        cfg.reply_prompt_version = original
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--posts", type=Path, action="extend", nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=40, help="per posts file (most recent)")
    parser.add_argument("--variants", default="", help="comma list, e.g. v1,v2; default: configured")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--carry-recent", action="store_true",
                        help="each variant sees its own last drafts, as in production")
    parser.add_argument("--trace", action="store_true", help="store each decision's trace in --out")
    args = parser.parse_args()

    from bot.daemon import Daemon

    daemon = Daemon()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()] or [
        getattr(daemon.cfg, "reply_prompt_version", "v1")
    ]
    posts: list[dict] = []
    for path in args.posts:
        posts.extend(load_posts(path, args.limit))
    results = replay(daemon.cfg, daemon.anthropic, posts, variants,
                     carry_recent=args.carry_recent, keep_trace=args.trace)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    for variant in variants:
        rows = [r for r in results if r.get("variant") == variant]
        print(variant, json.dumps(summarize(rows)))
    for r in results:
        verdict = repr(r.get("text")) if r.get("action") == "reply" else f"[{r.get('action')}] {str(r.get('reason') or r.get('error') or '')[:90]}"
        print(f"{r['variant']} @{r['author']}: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
