"""Where detected posts go: a JSONL file, a signed webhook, a Telegram chat."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

from .settings import Settings

log = logging.getLogger("xstream")

TELEGRAM_TEXT_CHARS = 3_000


def reply_link(post_id: str) -> str:
    """Opens X's composer as a reply; a person reviews and presses Post."""
    return f"https://x.com/intent/post?in_reply_to={post_id}"


def telegram_text(event: dict, now: float) -> str:
    created = event.get("created_at_epoch")
    age = f" · {now - created:.0f}s ago" if created else ""
    text = event["text"][:TELEGRAM_TEXT_CHARS]
    return f"@{event['author']['username']}{age}\n\n{text}\n\n{event['url']}\nReply: {reply_link(event['id'])}"


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Sinks:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        if settings.output.telegram and not (settings.telegram_bot_token and settings.telegram_chat_id):
            raise ValueError("output.telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    async def __call__(self, event: dict) -> None:
        out = self.settings.output
        if out.jsonl_path:
            with open(out.jsonl_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({**event, "detected_at": time.time()}, ensure_ascii=False) + "\n")
        if out.webhook_url:
            body = json.dumps(event, ensure_ascii=False).encode()
            headers = {"Content-Type": "application/json"}
            if self.settings.webhook_secret:
                headers["X-Signature-256"] = signature(self.settings.webhook_secret, body)
            response = await self.http.post(out.webhook_url, content=body, headers=headers)
            if response.status_code >= 300:
                log.warning("webhook answered HTTP %s for post %s", response.status_code, event["id"])
        if out.telegram:
            response = await self.http.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": telegram_text(event, time.time()),
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                log.warning("Telegram answered HTTP %s for post %s", response.status_code, event["id"])
