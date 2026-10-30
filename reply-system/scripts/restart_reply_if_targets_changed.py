#!/usr/bin/env python3
"""Restart the reply stream only when the daily target set changed."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("research/output/active-x-reply-targets.json"),
    )
    parser.add_argument(
        "--service",
        default="pentra-x-replies.service",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = json.loads(args.report.read_text())
    if not report.get("configChanged"):
        print("reply_stream_restart=skipped reason=targets_unchanged")
        return

    subprocess.run(
        ["systemctl", "try-restart", args.service],
        check=True,
    )
    print("reply_stream_restart=completed reason=targets_changed")


if __name__ == "__main__":
    main()
