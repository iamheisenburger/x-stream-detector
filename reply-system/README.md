# pentra-x

The X distribution system for @pentradev. It has two services, both running on the server at `/opt/pentra-x`:

- **Replies** (`bot/replies_daemon.py`). It watches chosen creators through X's official filtered stream and has
  each new post on the server in about 5 seconds. It writes a reply draft (median 11 s from the creator's post) and
  sends it to Telegram with a one-tap reply link. A person reviews it and presses Post, since X's API refuses
  automated unsummoned replies.
- **Posts** (`bot/posts_daemon.py`). It publishes up to 2 original posts a day through the X API. They follow a
  32-entry plan of sourced formats (`bot/post_cards.py`). It adds at most 1 news post a day, written from Google
  Search Central's blog and status dashboard (`bot/news.py`).

The detection layer alone is published separately as
[x-stream-detector](https://github.com/iamheisenburger/x-stream-detector).

**Status:** paused since Oct 24 2026 (the `PAUSED` file, kept local and on the server). Nothing deploys or restarts
until Arshad says "resume". The steps are in `CRON-PROMPTS.md` and `OVERNIGHT-LOG.md`.

## Layout

| Path | What |
|---|---|
| `bot/` | The services: stream client (`x_stream.py`), replies and posts daemons, writing and review (`generate.py`, `stances.py`), budgets (`x_read_budget.py`, `llm_budget.py`, `costguard.py`), state (`state.py`, SQLite) |
| `scripts/` | Timers and tools: daily summary, reply tracker, target selection, watchdog, leads |
| `deploy/` | systemd units and timers |
| `research/` | Candidate creators, measured reach and the replay corpus used by target selection |
| `config.json` | Live settings: targets, caps, prompt versions, posting schedule |
| `tests/` | The pytest suite. The deploy runs it locally and on the server, and stops if it fails |
| `OPERATIONS.md` | How the system runs, the rules (including the 40K reach floor) and costs |
| `HANDOFF-2026-10-19.md`, `X-VISION-CHECKLIST.md`, `OVERNIGHT-LOG.md` | Handoff, goals and the dated log |

## Keys

They live in `keys.env` next to this file (never committed) and go to the server's `.env` on deploy.
`Set Pentra keys.command` asks for any missing key. The names are in `.env.example`:

- `X_API_KEY`, `X_API_SECRET`: the X developer app (stream, reads).
- `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`: posting as @pentradev.
- `X_ANTHROPIC_API_KEY`: drafting.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: draft delivery.

## Tests and deploy

```bash
python3 -m pytest -q
bash "Deploy Pentra to server.command"   # refuses while PAUSED exists
```

The deploy runs the tests locally, copies the code over with rsync (keys, state and logs excluded), merges
`keys.env` into the server's `.env` without printing it, and runs the tests again on the server. Only then does it
restart the services.
