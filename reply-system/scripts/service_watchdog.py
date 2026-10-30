#!/usr/bin/env python3
"""Outside watchdog for Pentra and the sites it publishes (runs every 30 min on
this server, independent of Convex).

Oct 5-14 2026: Convex disabled the deployment (free-plan limits) and the
Anthropic credit ran out. Pentra's own health report runs inside Convex, so it
died with it: for nine days nothing published, every pentra.dev article
returned HTTP 500, and nobody was told. This checks from the outside and
messages the owner on Telegram when something breaks, again every few hours
while it stays broken, and once when it recovers.

Checks (watchdog.json):
- each site's sitemap answers 200 and lists blog articles;
- the newest article page answers 200;
- the newest article is no older than the site's cadence allows;
- the Convex deployment accepts requests (a lookup of a function that does not
  exist: it costs no database reads, and a disabled deployment refuses it
  before the lookup);
- this bot's own Claude calls are not being refused for lack of credit.
Read-only: nothing is written anywhere except this script's state file.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "watchdog-state.json"
DISABLED_MARKERS = ("disabled", "exceeded", "paused", "suspended", "spending limit")
# A healthy deployment answers the probe "Could not find public function ...";
# a disabled one answers a bare "[Request ID: ...] Server Error" (Oct 14 2026).
GENERIC_SERVER_ERROR = "server error"


def parse_sitemap(xml: str) -> list[tuple[str, str]]:
    """(loc, lastmod) for every <url>; lastmod may be empty."""
    rows = []
    for block in re.findall(r"<url>(.*?)</url>", xml or "", re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block)
        if not loc:
            continue
        mod = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", block)
        rows.append((loc.group(1), mod.group(1) if mod else ""))
    return rows


def parse_time(value: str) -> float | None:
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def newest_article(rows: list[tuple[str, str]], path_marker: str = "/blog/") -> tuple[str, float] | None:
    dated = [(parse_time(mod), loc) for loc, mod in rows if path_marker in loc]
    dated = [(at, loc) for at, loc in dated if at is not None]
    if not dated:
        return None
    at, loc = max(dated)
    return loc, at


def site_failures(site: dict, sitemap_status: int | None, sitemap_xml: str,
                  article_status: int | None, now: float) -> list[dict]:
    """Failures for one site, each {"key", "message"}; empty when healthy."""
    domain = site["domain"]
    if sitemap_status != 200:
        return [{"key": f"{domain}:sitemap", "message":
                 f"{domain} sitemap answers {sitemap_status or 'no response'} (Google cannot find new articles)."}]
    rows = parse_sitemap(sitemap_xml)
    newest = newest_article(rows, site.get("path_marker", "/blog/"))
    if not newest:
        return [{"key": f"{domain}:sitemap", "message": f"{domain} sitemap lists no dated articles."}]
    failures = []
    url, at = newest
    if article_status != 200:
        failures.append({"key": f"{domain}:article", "message":
                         f"{domain} articles answer {article_status or 'no response'} ({url})."})
    gap_hours = (now - at) / 3600
    if gap_hours > float(site["max_publish_gap_hours"]):
        failures.append({"key": f"{domain}:publishing", "message":
                         f"{domain} has not published for {gap_hours:.0f} h (newest {datetime.fromtimestamp(at, timezone.utc):%b %d %H:%M} UTC; "
                         f"expected at least every {site['max_publish_gap_hours']} h)."})
    return failures


def convex_failure(status: int | None, body: str) -> dict | None:
    """A disabled or unreachable Convex deployment; None when the public blog
    lookup the probe makes succeeds (it answers {"status":"success"})."""
    text = (body or "").lower()
    if status is None:
        return {"key": "convex", "message": "The Pentra backend (Convex) does not answer."}
    if status < 500 and '"status":"success"' in text.replace(" ", ""):
        return None
    detail = re.search(r'"errorMessage"\s*:\s*"([^"]{0,220})', body or "")
    hint = (" The deployment looks disabled: check the Convex plan limits / spending threshold."
            if any(marker in text for marker in DISABLED_MARKERS) or GENERIC_SERVER_ERROR in text else "")
    return {"key": "convex", "message": f"The Pentra backend (Convex) fails requests (HTTP {status}"
            + (f": {detail.group(1)}" if detail else "") + ")." + hint}


def credit_failure(db_path: str, now: float, window_seconds: float = 6 * 3600) -> dict | None:
    """The bot's last Claude call was refused for lack of credit: the refusal
    (recorded by the metered client, or as an outcome by older builds) is
    newer than the last successful, metered call."""
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        refused = [float(r[0]) for r in c.execute(
            "SELECT value FROM runtime_settings WHERE key='provider_credit_exhausted_at'").fetchall()]
        refused += [float(r[0]) for r in c.execute(
            "SELECT MAX(at) FROM generation_outcomes WHERE lower(COALESCE(reason,'')) LIKE ?",
            ("%credit balance is too low%",)).fetchall() if r[0] is not None]
        last_success = c.execute("SELECT MAX(at) FROM llm_usage").fetchone()[0] or 0
    except (sqlite3.Error, ValueError):
        return None
    last_refusal = max(refused, default=0)
    if last_refusal and now - last_refusal <= window_seconds and last_refusal > float(last_success):
        return {"key": "anthropic", "message":
                "The Anthropic API credit is used up: no articles or reply drafts can be written. "
                "Add credit and turn on auto-reload at console.anthropic.com > Plans & Billing."}
    return None


def plan_alerts(failures: list[dict], state: dict, now: float, realert_hours: float) -> tuple[list[str], dict]:
    """Messages to send and the next state: a new failure alerts at once, a
    persisting one again every realert_hours, a cleared one once on recovery."""
    messages: list[str] = []
    current = {f["key"]: f for f in failures}
    nxt: dict = {}
    for key, failure in current.items():
        seen = state.get(key)
        if not seen:
            messages.append("PROBLEM: " + failure["message"])
            nxt[key] = {"since": now, "last_alert": now}
        elif now - float(seen.get("last_alert", 0)) >= realert_hours * 3600:
            hours = (now - float(seen.get("since", now))) / 3600
            messages.append(f"STILL BROKEN ({hours:.0f} h): " + failure["message"])
            nxt[key] = {"since": seen.get("since", now), "last_alert": now}
        else:
            nxt[key] = seen
    for key, seen in state.items():
        if key not in current:
            hours = (now - float(seen.get("since", now))) / 3600
            messages.append(f"RECOVERED after {hours:.0f} h: {key}")
    return messages, nxt


async def fetch(client, method: str, url: str, **kwargs) -> tuple[int | None, str]:
    try:
        response = await client.request(method, url, **kwargs)
        return response.status_code, response.text
    except Exception:  # unreachable is itself the signal
        return None, ""


async def run(config: dict, now: float) -> list[dict]:
    import httpx

    failures: list[dict] = []
    headers = {"User-Agent": "pentra-watchdog/1.0 (+https://pentra.dev)"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        for site in config.get("sites", []):
            status, xml = await fetch(client, "GET", site["sitemap"])
            article_status = None
            newest = newest_article(parse_sitemap(xml), site.get("path_marker", "/blog/")) if status == 200 else None
            if newest:
                article_status, _ = await fetch(client, "GET", newest[0])
            failures += site_failures(site, status, xml, article_status, now)
        if config.get("convex_url"):
            # A real public query (two index lookups for a slug that does not
            # exist): a healthy deployment answers success with null.
            status, body = await fetch(client, "POST", config["convex_url"].rstrip("/") + "/api/query",
                                       json={"path": "blog:getPublishedBySlug",
                                             "args": {"domain": config.get("convex_probe_domain", "pentra.dev"), "slug": "watchdog-probe"},
                                             "format": "json"})
            failure = convex_failure(status, body)
            if failure:
                failures.append(failure)
    failure = credit_failure(str(ROOT / "state.db"), now)
    if failure:
        failures.append(failure)
    return failures


async def main() -> int:
    config = json.loads((ROOT / "watchdog.json").read_text())
    if not config.get("enabled", True):
        return 0
    now = time.time()
    failures = await run(config, now)
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    messages, nxt = plan_alerts(failures, state, now, float(config.get("realert_hours", 6)))
    if messages:
        from bot.config import load
        from bot.telegram_approval import TelegramApprovalClient

        await TelegramApprovalClient(load()).send_message("Pentra watchdog\n\n" + "\n\n".join(messages))
    STATE_PATH.write_text(json.dumps(nxt))
    print(json.dumps({"at": now, "failures": failures, "sent": len(messages)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
