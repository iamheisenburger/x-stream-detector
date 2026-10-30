#!/bin/bash
# Pops up a box for each missing key and saves it into keys.env (next to this file).
# Keys are never printed or logged.
cd "$(dirname "$0")" || exit 1
F=keys.env
touch "$F"; chmod 600 "$F"

set_key() { # $1 name, $2 value
  grep -q "^$1=" "$F" || printf '%s=\n' "$1" >> "$F"
  V="$2" awk -v k="$1" 'BEGIN{FS="="} $1==k {print k "=" ENVIRON["V"]; next} {print}' "$F" > "$F.tmp" && mv "$F.tmp" "$F" && chmod 600 "$F"
}
has_key() { grep -Eq "^$1=.+" "$F"; }

ask() { # $1 name, $2 prompt, $3 regex check
  has_key "$1" && return 0
  for attempt in 1 2 3; do
    val=$(osascript <<OSA 2>/dev/null
set r to display dialog "$2" default answer "" with hidden answer with title "Pentra keys" buttons {"Skip", "Save"} default button "Save"
if button returned of r is "Skip" then return "__SKIP__"
return text returned of r
OSA
)
    [ "$val" = "__SKIP__" ] || [ -z "$val" ] && return 0
    val=$(printf '%s' "$val" | tr -d '[:space:]')
    if printf '%s' "$val" | grep -Eq "$3"; then set_key "$1" "$val"; return 0; fi
    osascript -e 'display dialog "That does not look right. Copy it again and paste it." with title "Pentra keys" buttons {"OK"} default button "OK"' >/dev/null 2>&1
  done
}

HAD_POST_TOKENS=$( { has_key X_ACCESS_TOKEN && has_key X_ACCESS_TOKEN_SECRET; } && echo yes)
ask X_ANTHROPIC_API_KEY "Paste your Anthropic API key (starts with sk-ant-), then click Save." '^sk-ant-[A-Za-z0-9_-]{20,}$'
ask TELEGRAM_BOT_TOKEN "Paste the Telegram bot token from BotFather (looks like 123456789:AA...), then click Save." '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'
ask X_API_KEY "Paste the X API Key (console.x.com > your app > Keys and tokens). Click Skip if you don't have it yet." '^[A-Za-z0-9]{15,}$'
ask X_API_SECRET "Paste the X API Key Secret. Click Skip if you don't have it yet." '^[A-Za-z0-9]{30,}$'
ask X_ACCESS_TOKEN "For automatic posts: paste the X Access Token for @pentradev (console.x.com > your app > Keys and tokens > Access Token and Secret; it starts with numbers and a dash). The app must have Read and write permission before you generate it." '^[0-9]+-[A-Za-z0-9]{20,}$'
ask X_ACCESS_TOKEN_SECRET "Paste the X Access Token Secret." '^[A-Za-z0-9]{30,}$'

status() { has_key "$1" && echo "saved" || echo "missing"; }
MSG="Anthropic key: $(status X_ANTHROPIC_API_KEY)
Telegram token: $(status TELEGRAM_BOT_TOKEN)
X API key: $(status X_API_KEY)
X API secret: $(status X_API_SECRET)
X access token (posts): $(status X_ACCESS_TOKEN)
X access token secret: $(status X_ACCESS_TOKEN_SECRET)

Done. You can close this window."
osascript -e "display dialog \"$MSG\" with title \"Pentra keys\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1
# Status only (no values) for Claude to read:
{ for k in X_ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN X_API_KEY X_API_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET; do echo "$k=$(status $k)"; done; date -u; } > keys-status.txt
# New posting keys go to the server straight away (automatic posts start).
if [ -z "$HAD_POST_TOKENS" ] && has_key X_ACCESS_TOKEN && has_key X_ACCESS_TOKEN_SECRET; then
  bash "./Deploy Pentra to server.command"
fi
