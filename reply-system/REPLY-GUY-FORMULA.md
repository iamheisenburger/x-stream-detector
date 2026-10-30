# Pentra reply-guy formula

**Principle:** reply growth is a numbers game. Replies earn attention when they
land early, under posts that many people see, often enough to compound. Every
target-list decision follows the formula below. It lives in
`bot/reply_formula.py`, its numbers are in `config.json` → `reply_formula`,
and `tests/test_reply_formula.py` fails if either drifts.

## 1. Speed first, then reach at scale

**Speed:** a reply only wins if it's one of the first under the post. Target:
**a draft in Telegram within a minute of the post, posted within the first
minute or two.** How the system gets there:

- The official X filtered stream pushes each new post from a target account
  within seconds. There's no polling.
- Posts older than **90 seconds** when they arrive (reconnect replays) are
  dropped, because we'd be late.
- Up to 3 drafts are written in parallel, 1 second apart at most. Each takes one
  short Claude Haiku call plus a fact check, a few seconds.
- The draft expires after **15 minutes**; the Telegram message shows the post's
  age so the fastest replies go first.

**Reach at scale:** being first is worth what the post is worth. That's its
impressions, driven by the creator's audience:

```
opportunity(a)       = median impressions per original post of account a
daily_opportunity(a) = relevant_posts_per_day(a) × opportunity(a)
```

A post's final reply count isn't used. We're there before the crowd.

**Relevance is broad, not SEO-only:** Pentra's realm is general business
(business, money, sales, customers, marketing, founders, startups, growth,
careers, AI, tech, content, SEO...). The ~110 topic words in
`x_detection.original_rule_keywords` decide which posts reach us. A Hormozi
post on sales or hiring qualifies; a post about football does not.

## 2. Gates: an account is eligible only if all pass

| Gate | Threshold | Why |
|---|---|---|
| Measured | median views over ≥ 3 original posts, pooled from every measurement of the account | no guessing reach |
| Active | ≥ 0.2 original posts a day (one every 5 days) | a huge account that posts rarely still counts |
| In the realm | ≥ 15% of posts contain a general-business topic word | broad on purpose: the stream itself only delivers (and bills) the posts that match |
| Clean | ≤ 20% politics, crypto, sport or celebrity; hand-reviewed off-lane list | brand safety |

## 3. Reach floor: as big as the volume target allows

```
required_on_topic_posts_per_day = daily_draft_target / draft_rate = 35 / 0.40 = 87.5
reach_floor = the highest of 100K, 75K, 50K, 40K, 30K, 25K, 20K, 15K, 10K
              at which the eligible accounts above it post ≥ 87.5 on-topic posts a day
```

Big accounts first; the floor only comes down as far as needed to hit 35
drafts a day. The pool report records the floor, the supply and whether the
volume target is met.

## 4. Daily order

The daily selector spends the read budget (90 on-topic posts a day) on the
highest-scoring accounts first:

```
score = 0.60 × impressions per post (log scale: 1K → 0, 1M → 1)
      + 0.15 × cadence (on-topic posts a day)
      + 0.10 × on-topic share
      + 0.15 × the account's own measured draft rate (learned from state.db)
```

Accounts whose posts never produce usable drafts sink on their own.

## 5. Speed, volume, quality, money

- **Speed:** the post is detected in seconds; the draft reaches Telegram within
  about a minute; it expires after 15 minutes, because later replies land
  behind the crowd.
- **Volume:** 35 drafts a day from day one, stepping to 50; at most one draft
  per account every 30 minutes.
- **Quality:** each reply adds one concrete thing (an implication, the missing
  step, a tradeoff, a sharper version of the idea, or one short pointed
  question). Voice (`reply_prompt_version: "v2"`): plain words, usually 6-22,
  a statement by default, no interview questions ("how does X handle Y"), no
  jargon stacks, no @handles. There's no praise-only reply, pitch, link,
  hashtag or invented experience, and an independent fact check still vetoes
  invented claims. Travel, family and celebration posts are skipped.
- **Money:** $35 a month hard ceiling (X reads $13.50, Claude $20.50, X writes $1,
  $1 spare). Only on-topic posts are billed. Existing measurements are reused
  at $0; any new paid measurement needs the owner's OK in chat first.

## 6. Review

Weekly: `python -m scripts.report_x_reply_funnel` (reads → drafts), and the
daily summary's "Posted" line from `reply_results` (drafts → posted →
impressions, likes, replies). Change the voice from those numbers, not taste.
Before changing the reply prompt, replay real posts through both versions
(`python -m scripts.replay_drafts --posts research/output/stream-posts.jsonl
--variants v1,v2 --out research/output/replay.jsonl`); it spends from the
same daily reply cap and sends nothing.
If drafts fall under 35 a day, first widen the measured pool (more eligible
big accounts), then let the floor fall. Never lower the quality bar to make
volume.
