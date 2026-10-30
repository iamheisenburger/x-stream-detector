#!/usr/bin/env python3
"""Write original posts from chosen cards with the production pipeline and
print them, so a change to the post writer is read before anything is
scheduled. Metered in the original-post lane (its daily and monthly caps
apply); sends and publishes nothing.

    python -m scripts.sample_posts --cards page-two-pages,not-indexed --out research/output/posts.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import generate  # noqa: E402
from bot.llm_budget import GenerationBudgetOpen  # noqa: E402


def sample(cfg, client, card_ids: list[str], post=None) -> list[dict]:
    """One post per card; each sees the earlier samples as recent posts, as a
    real schedule would. A closed budget ends the run."""
    post = post or generate.standalone_post
    rows, recent = [], []
    for card_id in card_ids:
        trace: dict = {}
        row = {"card": card_id}
        try:
            text = post(cfg, client, recent_posts=list(recent), evidence_id=card_id, trace=trace)
        except GenerationBudgetOpen as exc:
            rows.append({**row, "stopped": str(exc)})
            break
        row.update({
            "text": text,
            "rejection": "" if text else str(trace.get("rejection") or ""),
            "candidates": {k: v for k, v in (trace.get("draft") or {}).items() if k.startswith("candidate_")},
        })
        rows.append(row)
        if text:
            recent.append(text)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", default="", help="comma list of card ids; default: the next 4 in rotation")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    from bot.daemon import Daemon
    from bot.stances import reader_post_cards

    daemon = Daemon()
    ids = [c.strip() for c in args.cards.split(",") if c.strip()] or [c["id"] for c in reader_post_cards()[:4]]
    rows = sample(daemon.cfg, getattr(daemon, "original_anthropic", daemon.anthropic), ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    for row in rows:
        if row.get("stopped"):
            print(f"[{row['card']}] budget stop: {row['stopped'][:120]}")
        elif row.get("text"):
            print(f"[{row['card']}]\n{row['text']}\n")
        else:
            print(f"[{row['card']}] rejected: {row['rejection'][:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
