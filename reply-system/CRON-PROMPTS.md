> **PAUSED Oct 24 2026 by Arshad: no posts, no replies.** Do not recreate these crons, deploy or start any
> service until he says to resume (see `PAUSED`). Only the reply tracker and daily summary timers run.

# X window scheduled jobs (Oct 22 2026)

The X window's work is driven by `X-VISION-CHECKLIST.md`. These three jobs keep it moving. They live only in the
running Claude Code session and expire after 7 days. On every session start: run CronList, recreate any missing
job from this file (local time is PDT, so UTC = PDT + 7), and recreate all three before they expire.

## 1. Shift check: every 2 hours during the reply shift
cron `17 5-15/2 * * *` (05:17-15:17 PDT = 12:17-22:17 UTC)

```
[X window shift check] Run the X system health check on the server (scratchpad srv.sh; if it is missing, rebuild it from distribution/pentra-x/Pentra server task.command):
- stream connected and reading;
- drafts and draft rate since 12:00 UTC;
- the 12:50 UTC selector result (no collapse, AI at or under 25%);
- post slots published (13:00 and 19:30 UTC);
- Grok leads and digest;
- errors since the last restart.
Fix anything broken in the same turn (regression test, full suite, deploy, server check). Then open distribution/pentra-x/X-VISION-CHECKLIST.md: re-verify the done items still hold, and work the first open item the X window owns. Log outcomes in distribution/pentra-x/OVERNIGHT-LOG.md and update the checklist's Now column with evidence. Never message the Pentra window. Reply in at most 5 lines of outcomes.
```

## 2. Work block: four times a day outside the shift
cron `47 0,3,16,20 * * *` (16:47, 20:47, 00:47, 03:47 PDT = 23:47, 03:47, 07:47, 10:47 UTC)

```
[X window work block] The stream is closed outside 12:00-23:00 UTC, so deploys are safe. Open distribution/pentra-x/X-VISION-CHECKLIST.md and work the first open item the X window owns, top to bottom, until it passes or the block ends: build, measure, replay, ship. Every change gets a regression test that fails without it, the full suite, a deploy and a server check. Re-verify the done items and reopen any that slipped. Update the checklist with evidence and log in distribution/pentra-x/OVERNIGHT-LOG.md. Spend only within the approved caps. Never message the Pentra window. Reply in at most 5 lines of outcomes.
```

## 3. Daily report: after the server's 00:55 UTC summary
cron `13 18 * * *` (18:13 PDT = 01:13 UTC)

```
[X window daily report] After the 00:40 UTC tracker and the 00:55 UTC summary, update every number in the Now column of distribution/pentra-x/X-VISION-CHECKLIST.md from the server (7-day windows), and tick or reopen items with evidence. Report in at most 10 lines:
- each section's status against its pass bar;
- followers today and the 7-day average;
- what shipped;
- the next item.
Write the same as a dated entry at the end of distribution/pentra-x/OVERNIGHT-LOG.md.
```
