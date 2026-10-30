# x-stream-detector

Detects new posts from the X accounts you choose, about 5 seconds after they go live. It uses X's official
filtered stream (`/2/tweets/search/stream`): X pushes each matching post to you, so there's no polling.

It came out of Pentra's reply system (@pentradev), which ran it in production from September to October 2026. How
that account writes its replies and posts is in [`voice/`](voice/):
[reply voice](voice/replies.md) and [post voice](voice/posts.md).

## Measured in production

206 posts from 73 creators, Sep 25 to Oct 23 2026:

| | p10 | median | p90 |
|---|---|---|---|
| Post goes live → on our server | 4.5 s | **4.9 s** | 5.3 s |
| Post goes live → reply draft ready (with an LLM step after detection) | 8.8 s | 11 s | 16 s |

Cost: about 40 matching posts a day at $0.005 each, so **about $0.20 a day** (about $6 a month).

## Why a stream, not polling

| | This (official stream) | Polling a scraping API (e.g. TwitterAPI.io) |
|---|---|---|
| Speed | X pushes the post: ~5 s | Half the poll interval on average, plus the provider's own scrape delay |
| What you pay for | Only posts that match your rules. Nothing when nobody posts. | Every request, including empty ones: TwitterAPI.io's published minimum is $0.00015 a request. 73 accounts checked once a minute is ~105,000 requests, so **$15+ a day**, and checking faster costs more. |
| Filtering | Keywords run on X's side, so off-topic posts are never delivered or billed. The same budget covers more creators, which means more reply candidates. | You download everything and filter it yourself |
| Account risk | None. App-only auth; it never touches a logged-in X account. | Cookie-based setups (`ct0` / `auth_token`) drive a logged-in account from a script. That's what gets accounts labeled or suspended. |

The detector itself doesn't post. Each alert carries a reply link (`x.com/intent/post?in_reply_to=…`) that opens X's composer, and a
person reviews and presses Post. X's API refuses automated unsummoned replies, and AI reply bots need X's written
approval.

## Setup

1. **X developer app.** At console.x.com, create an app on pay-per-use and add credits. Copy its bearer token (or the
   API key and secret).
2. **Install.**
   ```bash
   git clone <this repo> && cd x-stream-detector
   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
   cp config.example.json config.json   # set handles and keywords
   cp .env.example .env                 # set X_BEARER_TOKEN
   ```
3. **Check the rules (free, no X request).**
   ```bash
   .venv/bin/python -m xstream --config config.json --print-rules
   ```
4. **Run.**
   ```bash
   set -a; . ./.env; set +a
   .venv/bin/python -m xstream --config config.json
   ```
   ```
   2026-10-27 12:00:05 stream connected | 3 rule(s) | 73 handle(s)
   2026-10-27 12:04:11 post @creator 1850000000000000000 | 4.9s after posting | reads today $0.005
   ```
5. **Run it on a server.** Use `deploy/x-stream-detector.service` (systemd, restarts on failure).

Run one instance per X app: X limits each app's stream connections, and a second instance gets refused with a 429.

## Config (`config.json`)

| Key | What it does |
|---|---|
| `handles` | Accounts to watch, without `@`. Packed into as few rules as possible (X's limit is 1024 characters a rule). |
| `keywords` | Optional. Only posts containing one of these words are delivered, and billed. Long lists are split across rules. Leave empty to get every original post. |
| `extra_rules` | Raw X rule strings, added as-is (e.g. `"\"search console\" -is:retweet lang:en"`). |
| `include_replies` | `false` (default) watches original posts only. |
| `lang` | Language filter, `"en"` by default; `""` for any language. |
| `active_hours_utc` | e.g. `[12, 23]`: connected 12:00 to 23:00 UTC only, so nothing is read or billed outside it. `null` = always on. |
| `budget` | `post_read_usd` (X's price), `daily_cap_usd`, `monthly_cap_usd`, `pricing_valid_through`. |
| `output` | `jsonl_path` (one JSON line a post), `webhook_url` (signed with `WEBHOOK_SECRET` as `X-Signature-256: sha256=…`), `telegram` (needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`). |

Secrets come only from environment variables. The tool refuses to start if one is in the config file.

## Cost controls

- **Ledger.** Every delivered post, including a quoted post that arrives with it, is booked in `reads.db` before
  anything else happens. A post counts once per UTC day, matching X's billing. Counting quoted posts means the ledger
  errs high, never low.
- **Caps.** At the daily or monthly cap the stream disconnects and stays off until the next UTC day. `--spend` prints
  today's and this month's spend.
- **Price check.** After `pricing_valid_through` the stream refuses to connect until you check X's rate card and move
  the date forward.
- **Rule sync is idempotent.** A restart with the same config makes no rule changes. If the app has rules this tool
  didn't create, it stops instead of paying for their posts too.

## Staying connected

- X sends a keep-alive every 20 seconds. Ninety seconds of silence counts as a dead connection, and the tool
  reconnects.
- After a drop, it reconnects with back-off (1 s, doubling to 60 s), waiting at least 10 s because X briefly still
  holds the old connection.
- When X answers 429 with "connection limit", the old connection hasn't been released yet. The tool retries after
  30 s, 60 s, 120 s and so on, never past X's reset time. On the third refusal in a row it asks X which connections it
  still counts for the app and ends them. One stale connection once blocked every reconnect for two days.
- A genuine 429 waits for X's reset time. A 402 (no credits) retries every 5 minutes.
- At most 12 reconnects an hour; beyond that it waits rather than hot-looping.

## Use it as a library

```python
import asyncio
from xstream import Detector, ReadLedger, load

settings = load("config.json")

async def on_post(post: dict) -> None:
    print(post["author"]["username"], post["url"], post["text"][:80])

asyncio.run(Detector(settings, ReadLedger(settings.ledger_path, settings.budget)).run(on_post))
```

Your handler runs as its own task, so a slow or failing handler never holds up the stream. Each post carries `id`,
`url`, `text` (the full long-post text), `author` (`username`, `name`, `description`, `public_metrics`, `verified`),
`created_at_epoch`, `is_reply`, `is_quote`, the quoted or parent post's text and author, `link_context` (link
previews X attached), `media_context` (alt text) and `matching_rule_tags`.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Voice: how @pentradev writes replies and posts

- [Reply voice](voice/replies.md): agree and add one specific thing, in one plain sentence. It covers the checks that
  reject slogan and skeptic shapes, the rewrite and fact-check steps, and the exact live prompt.
- [Post voice](voice/posts.md): one thing a small business can act on this week, built only from sourced facts. It
  covers the seven formats, the checks and the exact live prompt.
