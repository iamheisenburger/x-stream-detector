#!/usr/bin/env python3
"""Safely apply an evidence-approved X reply roster delta.

The script preserves every unrelated configuration field and fails closed on
unexpected counts, missing removals, duplicate handles, or result drift.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def normalized(handle: str) -> str:
    return handle.lstrip("@").strip().casefold()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--expected-current-count", type=int, required=True)
    parser.add_argument("--expected-result-count", type=int, required=True)
    parser.add_argument("--add", action="append", default=[])
    parser.add_argument("--remove", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.config.read_text())
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise SystemExit("config targets must be a list")
    if len(targets) != args.expected_current_count:
        raise SystemExit(
            f"expected {args.expected_current_count} current targets, found {len(targets)}"
        )

    existing = [normalized(str(target.get("handle") or "")) for target in targets]
    if not all(existing) or len(existing) != len(set(existing)):
        raise SystemExit("current target handles are empty or duplicated")

    removals = {normalized(handle) for handle in args.remove}
    additions = [(normalized(handle), handle.lstrip("@").strip()) for handle in args.add]
    missing = sorted(removals - set(existing))
    if missing:
        raise SystemExit(f"requested removals are absent: {', '.join(missing)}")

    result = [
        target for target in targets
        if normalized(str(target.get("handle") or "")) not in removals
    ]
    result_keys = {
        normalized(str(target.get("handle") or "")) for target in result
    }
    for key, display in additions:
        if not key:
            raise SystemExit("addition handle is empty")
        if key in result_keys:
            raise SystemExit(f"addition already exists: {display}")
        result.append({"handle": display, "tier": "A"})
        result_keys.add(key)

    if len(result) != args.expected_result_count:
        raise SystemExit(
            f"expected {args.expected_result_count} result targets, found {len(result)}"
        )
    if len(result_keys) != len(result):
        raise SystemExit("result contains duplicate handles")

    print(
        json.dumps(
            {
                "before": len(targets),
                "after": len(result),
                "removed": sorted(removals),
                "added": [display for _, display in additions],
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    if args.dry_run:
        return 0

    payload["targets"] = result
    original_mode = args.config.stat().st_mode
    fd, temporary = tempfile.mkstemp(
        prefix=f".{args.config.name}.",
        suffix=".tmp",
        dir=args.config.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, original_mode)
        os.replace(temporary, args.config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
