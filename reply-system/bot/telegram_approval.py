"""Deliver human-approved reply drafts through Telegram.

Telegram is only the notification transport. The URL button opens Pentra's
mobile handoff (pentra.dev/x-reply), which launches the installed X app with
the reply prefilled. A person, the owner or their reply assistant, performs
the final Post action in X.
"""
from __future__ import annotations

import html
import os
from urllib.parse import urlencode, urlsplit

import httpx

from .config import Config

TELEGRAM_API_ROOT = "https://api.telegram.org"
# Pentra's handoff page opens the installed X app (Telegram's in-app browser
# would otherwise show X's web login). X's own web intent
# (https://x.com/intent/post) also works, e.g. for a desktop reply assistant.
DEFAULT_X_REPLY_HANDOFF_ROOT = "https://pentra.dev/x-reply"
X_REPLY_HANDOFF_ROOT = (
    os.environ.get("PENTRA_X_REPLY_HANDOFF_ROOT", "").strip()
    or DEFAULT_X_REPLY_HANDOFF_ROOT
)
X_STATUS_ROOT = "https://x.com"


class TelegramApprovalError(RuntimeError):
    """A Telegram approval notification could not be delivered safely."""


def parse_shift_command(
    text: str,
    *,
    default_minutes: int,
    max_minutes: int,
) -> tuple[str, int | None] | None:
    """Parse the small operator command surface used by Telegram."""
    parts = str(text or "").strip().lower().split()
    if not parts:
        return None
    command = parts[0].split("@", 1)[0]
    if command in {"/always", "/off", "/status"}:
        return command[1:], None
    if command != "/on":
        return None
    minutes = int(default_minutes)
    if len(parts) > 1:
        raw = parts[1]
        try:
            if raw.endswith("h"):
                minutes = round(float(raw[:-1]) * 60)
            elif raw.endswith("m"):
                minutes = round(float(raw[:-1]))
            else:
                minutes = round(float(raw))
        except (TypeError, ValueError):
            return None
    minutes = max(15, min(int(max_minutes), int(minutes)))
    return "on", minutes


def parse_artifact_message(message: dict) -> dict | None:
    """'/artifact <facts>' as text, or a screenshot whose caption starts with
    /artifact: the real data an original post is built from (Oct 20 2026).
    The facts must be text, because every number in a post is checked against
    them. '/howto' asks for a checked how-to card instead."""
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        return None
    command, _, body = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    if command == "/howto":
        return {"kind": "howto", "text": "", "photo_file_id": ""}
    if command != "/artifact" or not body.strip():
        return None
    photos = message.get("photo") or []
    photo_file_id = str((photos[-1] or {}).get("file_id") or "") if photos else ""
    return {"kind": "artifact", "text": body.strip(), "photo_file_id": photo_file_id}


def build_x_reply_intent(
    tweet_id: str,
    reply_text: str,
    author: str = "",
    root: str | None = None,
) -> str:
    root = root or X_REPLY_HANDOFF_ROOT
    if urlsplit(root).path.rstrip("/") in {"/intent/post", "/intent/tweet"}:
        query = urlencode({"in_reply_to": str(tweet_id), "text": reply_text})
        return f"{root}?{query}"
    # The handoff page reads its payload from the fragment, which browsers
    # never send to the server or to page analytics.
    fragment = urlencode(
        {
            "id": str(tweet_id),
            "text": reply_text,
            "author": author.lstrip("@"),
        }
    )
    return f"{root}#{fragment}"


def build_x_post_intent(post_text: str, root: str | None = None) -> str:
    """Handoff link for a new original post (no reply target)."""
    root = root or X_REPLY_HANDOFF_ROOT
    if urlsplit(root).path.rstrip("/") in {"/intent/post", "/intent/tweet"}:
        return f"{root}?{urlencode({'text': post_text})}"
    return f"{root}#{urlencode({'kind': 'post', 'text': post_text})}"


def _window_label(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 120:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def source_post_url(author: str, tweet_id: str) -> str:
    return f"{X_STATUS_ROOT}/{author.lstrip('@')}/status/{tweet_id}"


NTFY_PUBLISH_URL = "https://ntfy.sh/"


def phone_push_payload(topic: str, *, title: str, message: str, url: str) -> dict:
    """ntfy JSON publish body: high priority, tap opens the X handoff."""
    return {
        "topic": topic,
        "title": title[:120],
        "message": message[:1000],
        "priority": 5,
        "click": url,
        "actions": [{"action": "view", "label": "Open in X", "url": url, "clear": True}],
    }


class TelegramApprovalClient:
    def __init__(
        self,
        cfg: Config,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.cfg = cfg
        self._client = client

    @property
    def _base_url(self) -> str:
        token = self.cfg.telegram_bot_token
        if not token:
            raise TelegramApprovalError("TELEGRAM_BOT_TOKEN is missing")
        return f"{TELEGRAM_API_ROOT}/bot{token}"

    def _chat_id(self) -> str:
        chat_id = self.cfg.telegram_chat_id
        if not chat_id:
            raise TelegramApprovalError("TELEGRAM_CHAT_ID is missing")
        return chat_id

    async def _post(
        self,
        method: str,
        payload: dict,
        *,
        timeout_seconds: float | None = None,
    ) -> dict:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=(
                timeout_seconds
                or self.cfg.human_approval.request_timeout_seconds
            )
        )
        try:
            request_kwargs = {"json": payload}
            if timeout_seconds is not None:
                request_kwargs["timeout"] = timeout_seconds
            response = await client.post(
                f"{self._base_url}/{method}",
                **request_kwargs,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TelegramApprovalError(
                "Telegram request failed before delivery was confirmed"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        try:
            body = response.json()
        except Exception as exc:
            raise TelegramApprovalError(
                f"Telegram returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code != 200 or body.get("ok") is not True:
            description = str(body.get("description") or "unknown Telegram error")
            raise TelegramApprovalError(
                f"Telegram {method} failed HTTP {response.status_code}: {description}"
            )
        return body

    async def get_updates(
        self,
        *,
        offset: int = 0,
        timeout_seconds: int | None = None,
    ) -> list[dict]:
        poll_seconds = int(
            timeout_seconds or self.cfg.human_approval.telegram_poll_seconds
        )
        body = await self._post(
            "getUpdates",
            {
                "offset": int(offset),
                "timeout": poll_seconds,
                "allowed_updates": ["message"],
            },
            timeout_seconds=poll_seconds + 5,
        )
        return list(body.get("result") or [])

    async def phone_push(self, *, title: str, message: str, url: str) -> bool:
        """Best effort: a failed push never blocks or fails a draft."""
        topic = str(getattr(self.cfg.human_approval, "phone_push_topic", "") or "").strip()
        if not topic:
            return False
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.post(
                NTFY_PUBLISH_URL,
                json=phone_push_payload(topic, title=title, message=message, url=url),
                timeout=10,
            )
            return 200 <= response.status_code < 300
        except Exception:
            return False
        finally:
            if owns_client:
                await client.aclose()

    async def send_message(self, text: str, *, silent: bool = False) -> dict:
        """silent: shown in the chat without a phone notification."""
        return await self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id(),
                "text": str(text),
                "disable_web_page_preview": True,
                **({"disable_notification": True} if silent else {}),
            },
        )

    async def send_photo(self, *, file_id: str, caption: str = "") -> dict:
        """Re-send a photo already in Telegram (by file id), e.g. the
        screenshot that goes with a post draft."""
        return await self._post(
            "sendPhoto",
            {"chat_id": self._chat_id(), "photo": str(file_id), "caption": str(caption)[:1024]},
        )

    async def verify(self) -> dict:
        """Verify the bot and destination chat before paid detection starts."""
        me = await self._post("getMe", {})
        chat = await self._post("getChat", {"chat_id": self._chat_id()})
        return {
            "bot_username": str(me.get("result", {}).get("username") or ""),
            "chat_id": str(chat.get("result", {}).get("id") or ""),
        }

    def _reply_draft_message(
        self,
        *,
        tweet_id: str,
        author: str,
        source_text: str,
        reply_text: str,
        source_age_seconds: float,
        expires_in_seconds: int,
        author_reach: str = "",
        live_line: str = "",
    ) -> tuple[str, list, str]:
        """The draft message, its buttons and the X handoff link."""
        root = str(
            (getattr(self.cfg, "env", None) or {}).get("PENTRA_X_REPLY_HANDOFF_ROOT", "")
        ).strip() or None
        intent_url = build_x_reply_intent(tweet_id, reply_text, author, root=root)
        source_url = source_post_url(author, tweet_id)
        safe_author = html.escape(author.lstrip("@"))
        safe_source = html.escape(source_text[:900])
        safe_reply = html.escape(reply_text)
        message = (
            "<b>Pentra reply ready</b>\n"
            f"@{safe_author}"
            + (f" · {html.escape(author_reach)}" if author_reach else "")
            + f" · {source_age_seconds:.0f}s old"
            + (f"\n{html.escape(live_line)}" if live_line else "")
            + "\n\n"
            f"<b>Source</b>\n{safe_source}\n\n"
            f"<b>Draft</b>\n{safe_reply}\n\n"
            f"Post within {_window_label(expires_in_seconds)}."
        )
        keyboard = [
            [{"text": "Open reply in X app", "url": intent_url}],
        ]
        if 0 < len(reply_text) <= 256:
            keyboard.append(
                [
                    {
                        "text": "Copy draft",
                        "copy_text": {"text": reply_text},
                    }
                ]
            )
        keyboard.append([{"text": "View source post", "url": source_url}])
        return message, keyboard, intent_url

    async def send_reply_draft(
        self,
        *,
        tweet_id: str,
        author: str,
        source_text: str,
        reply_text: str,
        source_age_seconds: float,
        expires_in_seconds: int,
        author_reach: str = "",
    ) -> dict:
        message, keyboard, intent_url = self._reply_draft_message(
            tweet_id=tweet_id, author=author, source_text=source_text, reply_text=reply_text,
            source_age_seconds=source_age_seconds, expires_in_seconds=expires_in_seconds,
            author_reach=author_reach,
        )
        body = await self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id(),
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        result = body.get("result") or {}
        message_id = result.get("message_id")
        if message_id is None:
            raise TelegramApprovalError(
                "Telegram accepted the request without returning a message ID"
            )
        await self.phone_push(
            title=f"Reply ready: @{author.lstrip('@')}",
            message=reply_text,
            url=intent_url,
        )
        return {
            "chat_id": str((result.get("chat") or {}).get("id") or self._chat_id()),
            "message_id": int(message_id),
            "intent_url": intent_url,
        }

    async def edit_reply_draft(self, *, message_id: int, live_line: str, **draft) -> None:
        """Add the source post's live numbers to a sent draft, buttons kept.
        An edit is silent: it never notifies a second time."""
        message, keyboard, _ = self._reply_draft_message(live_line=live_line, **draft)
        await self._post(
            "editMessageText",
            {
                "chat_id": self._chat_id(),
                "message_id": int(message_id),
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    async def send_lead_draft(self, *, lead: dict, reply: str, dm: str, problems: list[str]) -> dict:
        """A warm lead from Grok with our drafted reply (and DM for fit 5).
        Arshad sends them himself: the X button prefills the reply composer."""
        from .leads import tweet_id_from_url

        root = str(
            (getattr(self.cfg, "env", None) or {}).get("PENTRA_X_REPLY_HANDOFF_ROOT", "")
        ).strip() or None
        source = "X" if lead.get("source") == "x" else "Indie Hackers"
        header = f"<b>Lead</b> · fit {int(lead.get('fit') or 0)}/5 · {source} · @{html.escape(str(lead.get('handle') or ''))}"
        if lead.get("posted_at"):
            header += f" · {html.escape(str(lead['posted_at']))}"
        lines = [header, html.escape(str(lead.get("summary") or ""))]
        if lead.get("post_text"):
            lines += ["", "<b>Their post</b>", html.escape(str(lead["post_text"])[:700])]
        if reply:
            lines += ["", "<b>Reply</b>", html.escape(reply)]
        if dm:
            lines += ["", "<b>DM</b>", html.escape(dm)]
        if problems:
            lines += ["", html.escape("; ".join(problems))[:300]]
        keyboard = []
        tweet_id = tweet_id_from_url(str(lead.get("url") or ""))
        if reply and tweet_id:
            keyboard.append([{"text": "Open reply in X app",
                              "url": build_x_reply_intent(tweet_id, reply, str(lead.get("handle") or ""), root=root)}])
        if 0 < len(reply) <= 256:
            keyboard.append([{"text": "Copy reply", "copy_text": {"text": reply}}])
        if 0 < len(dm) <= 256:
            keyboard.append([{"text": "Copy DM", "copy_text": {"text": dm}}])
        if str(lead.get("url") or "").startswith("https://"):
            keyboard.append([{"text": "Open their post", "url": str(lead["url"])}])
        body = await self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id(),
                "text": "\n".join(lines)[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        return {"message_id": int((body.get("result") or {}).get("message_id") or 0)}

    async def send_post_draft(self, *, post_text: str, topic: str = "") -> dict:
        """One original post for a person to publish from the X app."""
        root = str(
            (getattr(self.cfg, "env", None) or {}).get("PENTRA_X_REPLY_HANDOFF_ROOT", "")
        ).strip() or None
        intent_url = build_x_post_intent(post_text, root=root)
        message = (
            "<b>Pentra post ready</b>"
            + (f" · {html.escape(topic)}" if topic else "")
            + f"\n\n{html.escape(post_text)}\n\n"
            "Post it whenever you like; nothing is published until you press Post in X."
        )
        keyboard = [[{"text": "Open post in X app", "url": intent_url}]]
        if 0 < len(post_text) <= 256:
            keyboard.append(
                [{"text": "Copy post", "copy_text": {"text": post_text}}]
            )
        body = await self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id(),
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        result = body.get("result") or {}
        message_id = result.get("message_id")
        if message_id is None:
            raise TelegramApprovalError(
                "Telegram accepted the request without returning a message ID"
            )
        await self.phone_push(title="Post ready", message=post_text, url=intent_url)
        return {"message_id": int(message_id), "intent_url": intent_url}
