"""Verify a Telegram bot and discover the chat ID used for reply drafts.

The chat can be a private chat with the bot or a group that holds the owner,
the reply assistant and the bot. Anyone in that chat can start and stop shifts.

Usage:
  1. Put TELEGRAM_BOT_TOKEN in .env.
  2. Private: open the bot and send /start. Group: add the bot to the group
     and send /status@<bot username> in the group.
  3. Run: python -m scripts.telegram_setup
  4. Put the reported chat ID in TELEGRAM_CHAT_ID and run the command again.
  5. Send a visible delivery check with:
     python -m scripts.telegram_setup --send-test
"""
from __future__ import annotations

import argparse
import asyncio

import httpx

from bot.config import load
from bot.telegram_approval import build_x_reply_intent


async def telegram_call(token: str, method: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/{method}",
                json=payload,
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise RuntimeError("Telegram could not be reached") from exc
    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Telegram returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code != 200 or body.get("ok") is not True:
        raise RuntimeError(
            f"Telegram {method} failed: {body.get('description') or response.status_code}"
        )
    return body


ALLOWED_CHAT_TYPES = {"private", "group", "supergroup"}


def discover_chats(updates: list[dict]) -> list[dict]:
    """Chats that messaged the bot, newest last, one entry per chat."""
    chats: dict[str, dict] = {}
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") in ALLOWED_CHAT_TYPES and chat.get("id") is not None:
            chats.pop(str(chat["id"]), None)
            chats[str(chat["id"])] = chat
    return list(chats.values())


async def main(*, send_test: bool = False) -> None:
    cfg = load()
    token = cfg.telegram_bot_token
    if not token:
        raise SystemExit("Add TELEGRAM_BOT_TOKEN to .env first.")

    me = await telegram_call(token, "getMe", {})
    username = str((me.get("result") or {}).get("username") or "")
    print(f"bot=@{username}" if username else "bot=verified")

    if cfg.telegram_chat_id:
        chat = await telegram_call(
            token,
            "getChat",
            {"chat_id": cfg.telegram_chat_id},
        )
        result = chat.get("result") or {}
        print(f"chat_id={result.get('id')}")
        if send_test:
            test_text = (
                "[TEST ONLY - DO NOT POST] "
                "Pentra native X handoff is working."
            )
            intent_url = build_x_reply_intent(
                "1900000000000000000",
                test_text,
                "test",
            )
            sent = await telegram_call(
                token,
                "sendMessage",
                {
                    "chat_id": cfg.telegram_chat_id,
                    "text": (
                        "NATIVE X APP HANDOFF TEST\n\n"
                        "Ignore every older test button. This button first "
                        "opens Pentra's handoff page, which launches the "
                        "installed X app instead of Telegram's X web login.\n\n"
                        "The draft is marked TEST ONLY. Close the X composer "
                        "without posting.\n\n"
                        "New reply drafts will use the same handoff. Telegram "
                        "will never publish without your final tap in X."
                    ),
                    "disable_web_page_preview": True,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Test native X reply handoff",
                                    "url": intent_url,
                                }
                            ],
                            [
                                {
                                    "text": "Copy test draft",
                                    "copy_text": {"text": test_text},
                                }
                            ],
                        ]
                    },
                },
            )
            message_id = (sent.get("result") or {}).get("message_id")
            print(f"test_message_id={message_id}")
            print("Visible Telegram delivery test sent.")
        print("Telegram setup is complete.")
        return

    updates = await telegram_call(
        token,
        "getUpdates",
        {
            "timeout": 0,
            "allowed_updates": ["message"],
        },
    )
    chats = discover_chats(updates.get("result") or [])
    if not chats:
        raise SystemExit(
            f"Send /start to @{username} (or /status@{username} in your group), "
            "then run this command again."
        )
    for chat in chats:
        label = chat.get("title") or chat.get("username") or "private chat"
        print(f"{chat['type']}: {label} -> TELEGRAM_CHAT_ID={chat['id']}")
    print("Put the chat you want drafts in into .env, then run this command once more.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Send a visible connection confirmation to TELEGRAM_CHAT_ID.",
    )
    args = parser.parse_args()
    asyncio.run(main(send_test=args.send_test))
