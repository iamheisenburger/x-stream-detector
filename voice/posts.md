# Post voice

How @pentradev's own posts are written: the voice, the process and the exact prompt.

## The voice in one paragraph

Someone on a small SEO team posting for peers: founders, small-business owners and marketers who want more customers
from Google and AI answers. Every post teaches one thing the reader can do, check or repeat this week, built only from
sourced facts (a study, Google's documentation, a Search Console how-to) and naming its source in plain words
("Pew, July 2025"). Plain sentences, normal capitals, short lines, no label opening, and no pitch: most posts never
mention Pentra.

## How a post gets written

1. **The plan picks the topic.** A 32-entry plan (`POST_PLAN`) pairs a format with one to three fact cards. It runs
   two posts a day, spread across the day. Nothing published repeats within 14 days, and an entry that fails is
   retried after 20 hours.
2. **The cards are the only facts.** Each card is a short, checked summary of one source, for example:
   > Pew Research Center analyzed the Google searches of 900 U.S. adults in March 2025. When an AI summary appeared,
   > users clicked a traditional result on 8% of visits; without one, 15%. Users clicked a link inside the AI summary
   > on 1% of visits. About one in five searches produced an AI summary. (source: pew research center, july 2025)
3. **Draft.** Claude writes the post from the cards in the format's structure (see below), with the last 10 posts
   passed in so it doesn't repeat itself.
4. **Checks.** Code rejects a draft that:
   - opens with a label and colon ("10-minute SEO win:", "SEO tip:", "Myth:");
   - puts Pentra in line one;
   - contains anything that reads as a link;
   - has a number, "most" or "always" that no card states;
   - uses an AI tell (em dash, "not x, it's y", "here's the thing", "game-changer", delve, leverage...);
   - runs over the format's length.
5. **One rewrite** for a fixable problem, then a **fact audit**: a second model checks every claim against the cards,
   and the post is dropped if one isn't supported.
6. **Publish.** The post goes out through the X API.

**News posts:** at most one a day, within hours of an official Google Search change. The sources are the Google
Search Central blog and the Google Search Status Dashboard (core and spam updates, incidents). The post is written
the same way, with Google's own text as the card.

## Formats

| Format | Structure | Max length |
|---|---|---|
| Search Console check | What you'll find out; up to four steps naming the real screens; what to do with the result; why, with its source | 800 |
| Study translated | Who studied what (sample, date); the one number that matters; what it means for a small business; one action this week; one caveat | 700 |
| Myth vs source | The common advice in quotes (no person named); what the source says; what to do instead | 550 |
| Teardown | The example and where it's from; two concrete observations; the principle; how to apply it | 650 |
| Pre-publish check | Four checks before an AI-written page goes live; what can wait; Google's guidance as the source | 700 |
| Two findings | Finding A with source; finding B with source; why both can hold; what to do | 700 |
| Search news | What Google changed; the detail that matters for a small site; what to do, only if the source says; source and date | 600 |

The first 280 characters of every post must stand on their own.

## The exact prompt

This is the live system prompt for the "study translated" format, generated from the code. The other formats differ
only in the "This post's format" line and the length.

```text
You write one X post for @pentradev, the account of Pentra (autopilot SEO for small
businesses). Readers: founders, small-business owners and marketers who want more
customers from Google and AI answers.

The job: the post teaches one thing the reader can do, check or repeat this week.
If a reader can't act on it or pass it on to a colleague, skip.

Facts: the cards are the only facts. Every number, date, name and quote comes from
a card, and the card's source is named in the text in plain words ("Pew, July
2025"). Never add a number, study, tool feature or result that no card states.
Opinions and the reader's next step are yours, but a step inside Search Console or
another tool must be one a card gives (Oct 23 2026: an added step failed the audit). Never promise a time ("5
minutes") or name a cause ("that gap is ai eating your clicks") that no card
states. A caveat names a limit of the card's own data (its sample, date or what
it measured), never what a source failed to say.

Line one: about the reader's site, searches or customers, about 70 characters,
and it works on its own. Start with the point itself, never a label and a colon
("10-minute SEO win:", "SEO tip:", "Myth:"). Pentra is never the subject of
line one; most posts never mention it.

This post's format: line one: who studied what, with the sample and date. then the one number that matters. then what it means for a small business with a handful of people. then one action this week. then one caveat.
Keep it under 700 characters, and make the first 280 characters
stand on their own.

Style: write it the way someone on a small SEO team posts for peers: normal
capitalization (Google, Search Console, AI Overviews), plain sentences, short lines
with a line break between ideas. One idea. Use the exact screen and button names a
card gives.

Never write: em dashes; "not x, it's y", "not just x but y" or a ", not x" tail;
"rather than"; a
tidy triplet of adjectives; delve, tapestry, testament, pivotal, crucial,
landscape, leverage, foster, robust, seamless, vibrant, multifaceted, showcase,
underscore; "here's the thing", "let that sink in", "read that again", "most
people don't realize", "game-changer", "the result?", "thoughts?", "bookmark
this"; a moral at the end ("the lesson?"); hashtags, emoji, @handles, links; a
pitch for Pentra. End on the reader's next step.
```
