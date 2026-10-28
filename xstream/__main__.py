"""python -m xstream --config config.json"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from .budget import ReadLedger
from .runner import Detector
from .settings import load
from .sinks import Sinks


async def _run(settings, ledger) -> None:
    async with httpx.AsyncClient(timeout=10.0) as http:
        await Detector(settings, ledger).run(Sinks(settings, http))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xstream", description="Detect new X posts through the official filtered stream.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--print-rules", action="store_true", help="print the stream rules and exit (no X request)")
    parser.add_argument("--spend", action="store_true", help="print today's and this month's read spend and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    try:
        settings = load(args.config)
    except (OSError, ValueError, TypeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    ledger = ReadLedger(settings.ledger_path, settings.budget)
    if args.print_rules:
        for spec in Detector(settings, ledger).specs:
            print(f"{spec.tag} ({len(spec.value)} chars)\n  {spec.value}")
        return 0
    if args.spend:
        print(
            f"today: {ledger.reads_today()} posts, ${ledger.today_microusd() / 1e6:.3f} "
            f"of ${settings.budget.daily_cap_usd:.2f}\n"
            f"month: ${ledger.month_microusd() / 1e6:.3f} of ${settings.budget.monthly_cap_usd:.2f}"
        )
        return 0
    if not (settings.x_bearer_token or (settings.x_api_key and settings.x_api_secret)):
        print("set X_BEARER_TOKEN, or X_API_KEY and X_API_SECRET", file=sys.stderr)
        return 2
    if settings.output.telegram and not (settings.telegram_bot_token and settings.telegram_chat_id):
        print("output.telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run(settings, ledger))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
