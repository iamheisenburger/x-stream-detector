# Pentra X reply system

Pentra finds fresh original posts from a measured list of high-reach accounts,
drafts a reply worth posting (or skips), and sends the draft to a Telegram chat.
A person — the owner or Pentra's reply assistant — opens the draft in X and
presses Reply. Nothing is posted to X by this system unless the owner turns on
the separate original-posts service.

Built from the same architecture as the owner's other X reply service, with its
own brand voice, target list, X developer project, Telegram bot, database and
config. Browser cookies, proxies, unofficial endpoints and anything meant to
disguise automation are not part of this system.

## How a reply happens

1. Once a day, `pentra-x-target-selector.timer` ranks the measured pool and
   picks the accounts whose expected on-topic posts fit the day's read budget,
   trying to keep 4 accounts from each category (SEO and content is one)
   before filling the rest by score.
2. `pentra-x-replies.service` holds one official X filtered-stream connection
   with canonical rules in the `pentra-replies-v1` namespace. Rules match
   original posts only (`-is:reply -is:retweet`) that contain one of the ~110
   general-business topic words in `x_detection.original_rule_keywords`
   (business, money, sales, founders, marketing, AI, SEO...). X only delivers, and bills, those posts,
   so the read budget goes on posts Pentra can say something useful about.
3. Every delivered post is booked in the read ledger before anything else.
4. Deterministic checks (skipgate), pacing and target membership run for free.
   Posts older than 90 seconds at detection are dropped.
5. Claude Haiku writes one reply or skips. For substantive posts on Pentra's
   topics it must produce a candidate rather than skip. A grounding audit still
   rejects invented facts, borrowed experience, pitches, links and mentions.
6. The draft goes to Telegram with three buttons: Open reply in X app, Copy
   draft, View source. It stays valid for 15 minutes; tap early, the first
   replies are the ones that get seen. About 90 seconds after the post, the
   draft gains a line with the post's live numbers ("Now (1m 32s): 2.4K views ·
   31 likes · 9 replies"; a silent edit), so the fastest risers get posted
   first. The post is read again at 90 s and 60 min only on the UTC day the
   stream read it, which X does not bill again (checked Oct 19 against X's usage
   counter); both readings go to `source_snapshots`.
7. The reply assistant taps Open reply in X app. That opens
   `pentra.dev/x-reply`, which hands the draft to X's own composer. They read
   the source, press Reply, or skip.

The stream only runs while a shift is on (`/on` or `/always`). With nobody on
shift there are no reads and no spend.

Why a person presses Reply: since February 2026 X's API rejects replies to
authors who have not mentioned or quoted the account, and AI reply bots need
X's prior written approval. Driving a logged-in account with software breaks
X's rules. `VA-HIRING.md` has the job post, onboarding and rules for the
person who does it.

## Keys and deployment (no terminal needed)

- **Set Pentra keys.command** (double-click): pop-up boxes for the Anthropic
  key, Telegram bot token and X API key and secret, saved to `keys.env` next to
  it. Nothing is printed. `keys-status.txt` records only which keys are set.
- **Deploy Pentra to server.command** (double-click): copies this folder to
  `/opt/pentra-x` on the server, merges `keys.env` into the server's `.env`,
  builds the virtualenv, runs the tests and installs the systemd units without
  starting them. Output goes to `deploy.log`.

## Shift commands (anyone in the Telegram chat)

- `/on 4h` starts a bounded shift (15 minutes to 24 hours; `/on` = 24h).
- `/always` keeps detection on across restarts until `/off`, inside the daily
  window `human_approval.shift_hours_utc` (12:00-23:00 UTC since Oct 19 2026:
  every reply posted in October went up in those hours; drafts written outside
  them expired unposted). It stops and starts by itself at the window edges.
  A timed `/on` shift ignores the window. `[]` means around the clock.
- `/off` is the kill switch: closes the paid stream immediately.
- `/status` shows mode, drafts, reads and spend.

## Budget ($35/month ceiling, enforced)

`config.json` sets `monthly_ceiling_usd: 35`; the config refuses to load if
the lane caps add up to more.

| Lane | Monthly | Daily | Notes |
|---|---|---|---|
| Official X reads | $13.50 | $0.45 | $0.005 per unique post per UTC day, so 90 on-topic posts/day |
| Model calls: replies | $19 | $0.66 | measured Sep 30: about $0.005 per post read, so about $14 a month at 90 reads a day |
| Model calls: original posts | $1.50 | $0.07 | 2 Sonnet posts a day |
| Official X writes | $1 | $0.05 | original posts only ($0.015 each); replies are always posted by a person |

Why this split (Sep 26): measured overnight, one post costs $0.005 to read and
about $0.0075 in Claude calls (writer, fact check, and a repair when the fact
check vetoes the first draft). With $0.52/day for Claude, Claude ran out at
about 70 posts a day while X reads allowed 100. Matching the two lanes gives
about 90 posts a day inside the same $35. If original posts are authorized,
give them a reserve again by lowering `reply_daily_reserve_microusd`.

Pricing assumptions expire on `pricing_valid_through` (2026-10-31). The
affected lane stops until prices are rechecked and the date moved.

Target: at least 35 drafts a day. The approval cap starts at 35 and steps
40, 45, 50 a week at a time from `started_at_iso`. With keyword-scoped rules,
90 posts a day at a 40% draft rate is about 36 drafts. The rate on
keyword-matched reads is an assumption until Pentra has its own data; the
selector report shows the projection, and `python -m scripts.report_x_reply_funnel`
shows the real funnel. Drafts only arrive while a shift is on, so 35 a day
also needs the shift to cover the busy US hours.

## Building the target pool

Research nominations live in `research/candidates/pentra-candidates-seed.json`
(155 accounts, 6 categories, each with a source). They are unmeasured. An
account only becomes a target after the official X audit shows a median of at
least `x_detection.target_min_median_views` impressions (10,000; measured on Sep 25 no
non-overlapping candidate reached 100,000) over at least 3 original posts, at least 35%
on-topic posts, and at most 20% political, finance or sport posts.

Before go-live (stream disabled), measure every candidate once:

```bash
python -m scripts.build_reply_pool handles \
  --exclude /opt/<other-service>/config.json > research/output/to-audit.txt
python -m scripts.audit_x_target_reach_official \
  --handles-file research/output/to-audit.txt --per-handle \
  --bootstrap-daily-cap-usd 8 --allow-production-read-debit \
  --output research/output/audit-$(date -u +%F).json
python -m scripts.build_reply_pool build \
  --audit research/output/audit-*.json \
  --exclude /opt/<other-service>/config.json
python -m scripts.select_x_reply_targets --dry-run
```

After the pool exists, never rebuild it from the audit files: most measurements
(the August audits) live only in the pool, and on Oct 20 a rebuild cut it from
155 targets to 5. Add a new audit with `merge` instead. It judges only the newly
audited handles and keeps everything else:

```bash
python -m scripts.build_reply_pool merge --audit research/output/audit-<new>.json \
  --exclude /opt/leadpilot-x/config.json
python -m scripts.select_x_reply_targets --dry-run --report /tmp/sel-dry.json
```

The selector refuses to cut the live list below half (`--allow-shrink` overrides
this on purpose). Check every new handle with one batch user lookup before an
audit (`GET /2/users/by?usernames=...`, about $0.01 each). On Oct 20, "SteveBartlettSC"
did not exist and "adamgrant" was a different person, and search returns nothing
for them without saying so. Nine real, active accounts (AdamMGrant, profgalloway,
simonsinek, richardbranson, bchesky, friedberg, benthompson, heykahn, lissijean)
also return no posts from search; measuring them needs the user-timeline endpoint.

- Cost: at most 10 reads per account, so at most $7.75 for 155 accounts. It is
  booked in the same ledger and counts against that month's $24 X budget, so
  run it near the end of a UTC month if you want the full $24 for replies.
- `--bootstrap-daily-cap-usd` is refused once `x_detection.enabled` is true.
- `--exclude` keeps out accounts another account you run already watches, so
  your two accounts never converge on the same conversations.
- The pool report lists every rejected account with its reason and every
  account that still needs measuring. Re-run `handles` with the old audits
  passed as `--audit` to measure only what's missing.
- Posts younger than 24 hours are excluded because their view counts are
  still growing.

## Go-live checklist

1. Separate X developer project and app for @pentradev (X allows one
   filtered-stream connection per project, so it cannot share another brand's).
   Read permission is enough for replies; Read and Write only if original posts
   will be authorized.
2. Keys via **Set Pentra keys.command**, then **Deploy Pentra to server.command**
   (tests run on the server; see `deploy.log`). Never commit `keys.env` or `.env`.
3. `python -m pytest -q` passes on the server (the deploy script runs it).
4. Pool built (above) and selector dry run shows a sensible target list.
5. Telegram test tap works on the phone that will post.
6. Set `started_at_iso` to the go-live time (the ramp steps from there; empty
   keeps it at 35 a day).
7. Deploy to `/opt/pentra-x`, install the units, start the reply service and
   the selector timer:

```bash
sudo cp /opt/pentra-x/deploy/pentra-x-replies.service /etc/systemd/system/
sudo cp /opt/pentra-x/deploy/pentra-x-target-selector.service /etc/systemd/system/
sudo cp /opt/pentra-x/deploy/pentra-x-target-selector.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pentra-x-target-selector.timer
sudo systemctl enable --now pentra-x-replies.service
```

8. Send `/on 1h` and watch the first draft arrive; confirm it posts from the
   assistant's phone.

If it shares a server with another X service, keep everything separate:
directory, venv, `.env`, `state.db`, logs, locks (`/tmp/pentra-x-*.lock`), rule
namespace and developer project. Never copy the other service's `.env` or
database.

## Original posts (automatic, owner-authorized Sep 30 2026)

`pentra-x-posts.service` writes two original posts a day (13:00 and 19:30
UTC), using only the true cards in `bot/stances.py`, and publishes them through
the official X API. The owner authorized this
(`publishing.original_posts_authorized: true`).

Since Oct 20 2026 (`post_prompt_version: "v2"`) a post is written for the
founder who clicks through from one of our replies and has never heard of
Pentra: a move they can make today, a sharp take on getting found in Google and
AI answers, or a build story that ends in their lesson. Facts come only from
`stances.PLAYBOOK` (checkable Search Console, indexing and AI Overview
mechanics, and labeled opinions) and the build notes with a reader lesson. The
v1 posts drew only on build receipts and read as release notes. While
`publishing.original_posts_review_first` is true, each post goes to Telegram as
a draft for the owner to post himself; set it to false to let the API post
again. `python -m scripts.sample_posts --cards a,b --out <file>` writes samples
without sending anything. Safeguards:

- It publishes only with the account's own write token (`X_ACCESS_TOKEN`,
  `X_ACCESS_TOKEN_SECRET`, from an X app with Read and write permission) and
  only when that token belongs to `publishing.expected_username`
  (pentradev), checked at startup.
- Until those keys are installed it sends each post to Telegram as a draft
  ("Pentra post ready", "Open post in X app") instead.
- Every post passes the writer, the managing-editor judge, the mechanical
  guards (including no opening that points at missing context) and the
  grounding audit; a post that fails is skipped, never published.
- Budget: $1.50/month of Claude for posts and $1/month of X writes
  ($0.015 each), inside the $35 ceiling.

To install the keys: in console.x.com open the app, set User authentication
settings > App permissions to "Read and write", then Keys and tokens > Access
Token and Secret > Generate. Double-click "Set Pentra keys.command" and paste
both; it deploys them to the server by itself.

## Original posts v3: built from real artifacts (Oct 20 2026)

`post_prompt_version: "v3"`. Every post is built from one artifact: real data from
Pentra's logs, Search Console or the build log. Arshad's shapes: receipt, fact-check
story, teardown, build in public, 10-minute how-to, take with proof. The test: remove
the artifact's data and the post must collapse.

- `/artifact <facts>` in Telegram (a screenshot captioned `/artifact ...` works too):
  two drafts in two shapes within a minute, screenshot first. `/howto`: two drafts
  from the next checked Search Console how-to (`stances.HOW_TO_CARDS`).
- The Pentra window can push artifacts as JSON (`{"id", "kind", "text"}`, every
  number in `text`) to the ntfy topic in `artifacts.ntfy_topic`. Each one fills the
  next slot (13:00, 19:30 UTC) with two drafts. Owner sites only, unless a customer
  agreed in writing to be named.
- **Automatic since Oct 21 2026** (Arshad: "automate posts... stop adding my manual
  intervention"). `original_posts_review_first: false`: the best passing draft is
  published through the official API with @pentradev's own write token (checked as
  pentradev before go-live), and Telegram gets a quiet "Posted on @pentradev" note.
  Shape order is receipt, fact_check, build, teardown, how_to, take; drafts of 280
  characters or less go first, and a longer one is cut before posting. The publish cap
  is $0.05 a day and $1 a month ($0.015 a post). Only replies stay manual: the X API
  refuses unsummoned replies.
- A slot with no artifact, or whose artifact drafts nothing, publishes the next checked
  how-to card. It asks in Telegram only after all 5 cards are used.
- Each draft gets one rewrite for length, a canned style problem, or a number the
  artifact doesn't contain, and is dropped if the rewrite still fails. Drafts are also
  dropped when they share fewer than two of the artifact's numbers, quoted titles or
  domains, when the writer marks them as something any competitor could post, or when
  the grounding audit fails. How-to cards skip the two-token and competitor rules
  because their data is the steps.

## Warm leads from Grok Bot (Oct 20 2026)

Grok posts each lead (people asking for SEO help on X or Indie Hackers) as one JSON
message to the ntfy topic in `leads.ntfy_topic`. Treat that name as a password.
The fields are `id`, `source` ("x" or "indiehackers"), `url`, `handle`, `posted_at`,
`summary`, `fit` (1-5), `suggested_reply`, `post_text` and `top_replies` (up to 5).
`pentra-x-leads.timer` runs `scripts/process_leads.py` every 10 minutes. It reads
the 12-hour cache, skips leads already sent, drafts a reply (and a DM for fit 5)
with Haiku in the reply lane, and sends each lead to Telegram, highest fit first.
No X read is bought. Lead drafts keep only the safety rules (no links, @handles,
invented numbers, overclaims or pitch in the reply) and may run to 400 characters.

## Directory listings by Grok Bot (Oct 20 2026)

Grok lists Pentra on directories using only `../directory-kit/LISTING.md` (Arshad's
canonical text, logo and banner URLs on pentra.dev, 5 screenshots). Order: first the
sources AI assistants cite for buyer questions ("best AI SEO tool for a small
business"...), then free relevant SEO, AI-tool, SaaS and alternatives directories.
Free only; paid listings, launches (Product Hunt, Hacker News, BetaList) and any
outreach to people go to Arshad first. Once a day Grok posts one JSON digest
(`type` "directory_digest": live listings with URLs, submitted, rejected,
needs_arshad, ai_sources; everything so far, under 3,000 characters) to the ntfy
topic `pentra-grok-<your-private-topic>` (treat the name as a password). Read
it within 12 hours of a post: `curl -s "https://ntfy.sh/<topic>/json?poll=1&since=12h"`.
Nothing on the server stores it yet (open item in the handoff).
Verification emails go to pentrahelp@gmail.com and Arshad clicks them (Grok tells
him which directories wait on a click). That inbox is never signed into Grok's
browser, because it also gets Pentra's support mail and operator alerts. If Arshad
wants Grok to verify on its own, the way is a separate Gmail used only for directory
accounts (Arshad creates it), and the kit's account email changes with it.
@pentradev is never signed into Grok's browser, even if Arshad types the password
himself. An agent holding the logged-in X session is what the X rule forbids, and a
login from Grok's machine can trip X's security lock on our main account. Skip
directories that only offer "Sign in with X". Naming competitors as alternatives on a
directory (names only, no claims about them) is fine (Oct 20: SEObot, Outrank, Byword
on SaaSHub). Paid listings wait for Arshad.

## Local verification

```bash
python3 -m py_compile bot/*.py scripts/*.py
python3 -m pytest -q
```

## Test modes and prompt versions

- `human_approval.observe_only_until_iso` (ISO time): until then drafts are
  written and stored exactly as in production but not sent to Telegram, and
  unsolicited notices are silent. After it passes, drafts go to Telegram with
  no redeploy. `observe_only: true` is the open-ended version.
- `reply_prompt_version`: `"v5"` (config.json since Oct 19 2026, 22:2xZ) is v4
  plus: a character that roots for other builders, a peer rule under the
  author's own launch, product or news (no digs; the author's like or reply is a
  ranking input), 9 more house frames checked across the last 12 replies, and no
  forced candidate under event or ticket promos. Replays on the last 40 real
  posts: v4 drafted 28-29 with about 17 worth posting, v5 21-23 with about 18.
  `"v4"` is the reply-guy voice: 8-20 words, no copied sentence frames, build
  notes only under posts about the same problem, and a judge that never vetoes
  an opinion for lacking a tie-in to our own work. A draft over 28 words or
  repeating a recent reply's frame gets a rewrite (told its word count; a second,
  shorter try if it comes back long). `"v4"`/`"v3"`/`"v2"`/`"v1"` remain for
  rollback: edit the one line and deploy.
- `scripts/replay_drafts.py --variants v1,v2` compares prompts on captured real
  posts (`human_approval.capture_posts_path`). Metered, capped, sends nothing.

## Posted-reply tracking

`pentra-x-reply-tracker.timer` (00:40 UTC) reads @pentradev's own replies from
the last 50 hours (official recent search, one booked read per post), links
each to its draft and stores impressions, likes and replies in `reply_results`.
The 00:55 summary then says how many drafts were posted and how replies 1-4
days old did, and the draft rate (drafts per post the stream delivered) for
each of the last 3 days against the 40% target. Baseline (15 replies posted
Sep 30-Oct 4, re-measured Oct 19): median 129 impressions, 2 likes in total,
0 replies from authors; our reply got a median 0.2% of the source post's
impressions, so the post's size decides most of a reply's reach.
`python -m scripts.report_reply_visibility` (read-only, $0) lists each posted
reply with its timing and its source post's numbers at 90 s and 60 min, against
the R3 target of 5 times the baseline median.

Measurement reads share the monthly X cap with the stream ($13.50 is 90 reads a
day for 30 days), so each paid measurement read moves one stream read off the
end of the month. Same-day re-reads cost nothing.

If X credits run out (HTTP 402) the reply service now says so once in Telegram
instead of only logging it.

## Incident rules

- `/off` first. Do not restart repeatedly to diagnose spend.
- If a read cap trips, leave the stream off until the next UTC day or month.
- If Telegram fails, never fall back to posting replies automatically.
- Preserve `state.db` and logs before any repair.

## What counts as working

1. The selector report shows a measured pool and a daily subset inside the
   read budget.
2. A shift produces at least 35 drafts a day, each sent within 5 minutes of the source post.
3. The assistant can open each draft in X signed in as @pentradev and post it.
4. Daily and monthly spend stay inside the caps.

Followers, impressions, sign-ups and beta testers are outcomes to measure
afterwards, not something a successful deploy proves.

## Targeting rule (Arshad, Oct 19 2026, overrides anything else in this repo)
"we optimize for impressions per reply." Impressions per reply come from FAST replies to posts that get LARGE
impressions, from LARGE creators. Push reply quality as far as it goes, then it is a numbers game: more
impressions -> more followers. Never lower reach floors to admit small/niche accounts (a 3k "SEO/founder"
floor was proposed and rejected). More drafts must come from more large accounts and faster replies, not
smaller accounts. Keep the 40k reach floor (raise it if data shows bigger accounts give more impressions/reply).

Who is a target (Oct 20 2026, Arshad: replace the AI accounts with founders, marketers and creators): people
only. `scripts/select_x_reply_targets.py` drops company and product accounts (`BRAND_ACCOUNTS`) and AI-industry
people (`AI_INDUSTRY_ACCOUNTS`: lab staff, researchers, megacap CEOs, developer-tool accounts, AI investors,
other fields), and the rest of the AI and automation category gets at most 25% of a day's reads
(`CATEGORY_READ_SHARE`). Evidence: 14 settled replies under that category had a median of 34 impressions; 132
under business accounts, 1,821 under @nikitabier. New big accounts join only through a reach audit.
