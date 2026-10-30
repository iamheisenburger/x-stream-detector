#!/bin/bash
# Copies pentra-x to the server (/opt/pentra-x), installs it and runs its tests.
# keys.env goes to the server's .env without being printed. Log: deploy.log
cd "$(dirname "$0")" || exit 1
KEY="${PENTRA_X_SSH_KEY:?set PENTRA_X_SSH_KEY to the server SSH key path}"
HOST="${PENTRA_X_HOST:?set PENTRA_X_HOST, e.g. ubuntu@<server-ip>}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes)
exec > >(tee deploy.log) 2>&1
echo "== deploy $(date -u)"
[ -f "$KEY" ] || { echo "SSH key not found at $KEY"; exit 1; }
# Oct 24 2026: Arshad paused the X system. A deploy restarts the services, so it waits for him.
[ -f PAUSED ] && { cat PAUSED; echo "PAUSED: nothing deployed"; exit 1; }
# Oct 22 2026: a failing test used to deploy anyway (the result was only printed).
python3 -m pytest -q -p no:cacheprovider > /tmp/pentra-x-local-pytest.log 2>&1 || {
  tail -20 /tmp/pentra-x-local-pytest.log; echo "LOCAL TESTS FAILED: nothing deployed"; exit 1; }
echo "local tests: $(tail -1 /tmp/pentra-x-local-pytest.log)"
"${SSH[@]}" "$HOST" 'set -e; sudo mkdir -p /opt/pentra-x; sudo chown ubuntu:ubuntu /opt/pentra-x; echo host=$(hostname); python3 --version; df -h / | tail -1' || { echo "SSH FAILED"; exit 1; }
rsync -az --delete \
  --exclude '.env' --exclude 'keys.env' --exclude 'keys-status.txt' --exclude 'deploy.log' \
  --exclude '.git' --exclude '*.command' --exclude 'venv' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude 'research/output' --exclude 'server-task.*' --exclude 'VA-HIRING.md' --exclude 'state.db' --exclude 'state.db-*' --exclude 'watchdog-state.json' --exclude '*.log' \
  -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes" ./ "$HOST:/opt/pentra-x/" && echo "rsync ok"
if [ -f keys.env ]; then
  grep -E '^[A-Z_]+=.+' keys.env | "${SSH[@]}" "$HOST" 'umask 077; cat > /opt/pentra-x/.env.new; touch /opt/pentra-x/.env; while IFS= read -r line; do k=${line%%=*}; grep -v "^$k=" /opt/pentra-x/.env > /opt/pentra-x/.env.tmp || true; mv /opt/pentra-x/.env.tmp /opt/pentra-x/.env; echo "$line" >> /opt/pentra-x/.env; done < /opt/pentra-x/.env.new; rm -f /opt/pentra-x/.env.new; chmod 600 /opt/pentra-x/.env; echo "server .env keys set: $(cut -d= -f1 /opt/pentra-x/.env | tr "\n" " ")"'
fi
"${SSH[@]}" "$HOST" 'set -e; cd /opt/pentra-x; test -d venv || python3 -m venv venv; venv/bin/pip install -q --upgrade pip >/dev/null; venv/bin/pip install -q -r requirements-dev.txt; venv/bin/python -m pytest -q -p no:cacheprovider > /tmp/pentra-x-pytest.log 2>&1 || { tail -20 /tmp/pentra-x-pytest.log; echo "SERVER TESTS FAILED: services not restarted"; exit 1; }; tail -1 /tmp/pentra-x-pytest.log; sudo cp deploy/pentra-x-*.service deploy/pentra-x-*.timer /etc/systemd/system/; sudo systemctl daemon-reload; echo units-installed; test -f research/output/pentra-x-reply-targets.json && venv/bin/python -m scripts.select_x_reply_targets >/dev/null && echo targets-restored; sudo systemctl try-restart pentra-x-replies.service && echo replies-restarted; sudo systemctl restart pentra-x-posts.service && echo posts-restarted; sudo systemctl enable --now pentra-x-watchdog.timer >/dev/null 2>&1 && echo watchdog-timer-on; sudo systemctl enable --now pentra-x-reply-tracker.timer >/dev/null 2>&1 && echo reply-tracker-timer-on; sudo systemctl enable --now pentra-x-leads.timer >/dev/null 2>&1 && echo leads-timer-on; systemctl is-enabled pentra-x-replies.service pentra-x-posts.service pentra-x-target-selector.timer 2>&1 | tr "\n" " "; echo'
# Telegram chat: find it from the bot's messages (/start, /status) and save it.
CHAT=$("${SSH[@]}" "$HOST" 'cd /opt/pentra-x; grep -Eq "^TELEGRAM_CHAT_ID=.+" .env && { grep -E "^TELEGRAM_CHAT_ID=" .env | cut -d= -f2; exit 0; }; venv/bin/python - <<"PY"
import asyncio
from bot.config import load
from scripts.telegram_setup import telegram_call, discover_chats
cfg = load()
if not cfg.telegram_bot_token:
    raise SystemExit(0)
updates = asyncio.run(telegram_call(cfg.telegram_bot_token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}))
chats = discover_chats(updates.get("result") or [])
private = [c for c in chats if c.get("type") == "private"]
pick = (private or chats or [None])[-1]
if pick:
    cid = pick["id"]
    with open(".env", "a") as f:
        f.write("TELEGRAM_CHAT_ID=%s\n" % cid)
    print(cid)
PY')
if [ -n "$CHAT" ]; then
  echo "telegram chat id: $CHAT"
  grep -q '^TELEGRAM_CHAT_ID=' keys.env || echo 'TELEGRAM_CHAT_ID=' >> keys.env
  V="$CHAT" awk 'BEGIN{FS="="} $1=="TELEGRAM_CHAT_ID" {print "TELEGRAM_CHAT_ID=" ENVIRON["V"]; next} {print}' keys.env > keys.env.tmp && mv keys.env.tmp keys.env && chmod 600 keys.env
else
  echo "telegram chat id: not found yet (send /start to the bot, then run this again)"
fi
echo "== done $(date -u)"
