#!/usr/bin/env python3
"""Every 10 minutes (pentra-x-leads.timer): queue the Pentra window's artifacts
for original posts (config artifacts.ntfy_topic), and read Grok's warm leads from the
ntfy.sh topic in config leads.ntfy_topic, draft a reply (and a DM for fit-5
leads) for each new one and send it to Telegram for Arshad to send himself.

Each run reads the whole 12-hour ntfy cache; a lead already sent is skipped by
its id, so a failed run loses nothing. Drafting is metered in the reply lane;
no X read is bought (Grok includes the post text).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NTFY_ROOT = "https://ntfy.sh"


async def main() -> int:
    import httpx
    from anthropic import Anthropic

    from bot.config import CONFIG_PATH, load
    from bot.leads import ntfy_messages, process_leads, queue_feed_artifacts, store_grok_digests
    from bot.llm_budget import MeteredAnthropic
    from bot.state import State
    from bot.telegram_approval import TelegramApprovalClient

    raw = json.loads(Path(CONFIG_PATH).read_text())
    settings = raw.get("leads") or {}
    topic = str(settings.get("ntfy_topic") or "").strip()
    artifacts_topic = str((raw.get("artifacts") or {}).get("ntfy_topic") or "").strip()
    async with httpx.AsyncClient(timeout=20) as http:
        if artifacts_topic:
            # The Pentra window's daily artifacts for original posts (v3).
            feed = await http.get(f"{NTFY_ROOT}/{artifacts_topic}/json", params={"poll": "1", "since": "12h"})
            if feed.status_code == 200:
                print(json.dumps({"artifacts": queue_feed_artifacts(State(), ntfy_messages(feed.text))}))
        grok_topic = str((raw.get("grok") or {}).get("ntfy_topic") or "").strip()
        if grok_topic:
            # Grok's daily directory digest, stored for the morning summary.
            reports = await http.get(f"{NTFY_ROOT}/{grok_topic}/json", params={"poll": "1", "since": "12h"})
            if reports.status_code == 200:
                print(json.dumps({"grok_digests": store_grok_digests(State(), ntfy_messages(reports.text))}))
        if not settings.get("enabled") or not topic:
            print(json.dumps({"skipped": "leads disabled"}))
            return 0
        response = await http.get(f"{NTFY_ROOT}/{topic}/json", params={"poll": "1", "since": "12h"})
    if response.status_code != 200:
        print(json.dumps({"error": f"ntfy HTTP {response.status_code}"}))
        return 0
    messages = ntfy_messages(response.text)
    if not messages:
        print(json.dumps({"messages": 0}))
        return 0
    cfg = load()
    state = State()
    client = MeteredAnthropic(Anthropic(api_key=cfg.anthropic_key), cfg.generation, state, lane="reply")
    result = await process_leads(cfg, state, client, TelegramApprovalClient(cfg), messages)
    print(json.dumps({"messages": len(messages), **result}))
    return 0


if __name__ == "__main__":
    from bot.daemon import daemon_lock

    with daemon_lock("/tmp/pentra-x-leads.lock"):
        raise SystemExit(asyncio.run(main()))
