"""Deterministic pre-filter, run before any LLM call. Cheap reasons to not reply.

A streamed tweet event is a dict with at least: id_str/id, text, created_at (epoch
seconds or parseable), author screen_name, is_reply, is_retweet, lang.
The LLM is the final quality gate; this just avoids wasting calls and enforces
the freshness/dedup/substance rules.
"""
import re
import time

from .config import Config
from .state import State


# High-precision categories Pentra must never reply to. This gate is kept
# deliberately narrower than the semantic editor: it saves a model call only
# when the source is plainly politics, violent crime/war, sports, or celebrity
# entertainment. Ambiguous business and technology posts still reach Claude.
_FORBIDDEN_TOPIC_RE = re.compile(
    r"\b(?:"
    r"trump|biden|republicans?|democrats?|election|congress|senator|senate|"
    r"prime minister|parliament|white house|pentagon|governor|mayor|"
    # A political office, not a corporate title ("vice president of sales").
    r"vice president(?!\s+(?:of|for|at|and)\b)|vance|kamala|obama|pence|desantis|newsom|"
    r"gop|maga|campaign rally|"
    r"iran|israel|gaza|ukraine|hezbollah|nato|airstrike|missile|"
    r"military|warfare|war crime|"
    r"murder|rape|kidnap(?:per|ping)?|law enforcement|police|criminal|"
    r"propublica|breaking news|news report|media outlet|journalist|reporter|"
    r"nfl|nba|wnba|premier league|football|soccer|baseball|"
    r"box office|celebrity|actor|actress|movie trailer"
    r")\b",
    re.I,
)

_SPECIALIST_MEDICAL_RE = re.compile(
    r"\b(?:cancers?|vaccines?|clinical trials?|drug trials?|"
    r"medical treatments?|diagnos(?:is|es|tic)|cures? for (?:all )?diseases?|"
    r"phase\s*[123]\s+trials?)\b",
    re.I,
)

_THIRD_PARTY_NEWS_REACTION_RE = re.compile(
    r"\b(?:major breakthrough today|breaking(?: news)?|reportedly|"
    r"according to (?:a|an|the)|news just (?:broke|dropped))\b",
    re.I,
)

_AEROSPACE_OPERATION_RE = re.compile(
    r"\b(?:satellites?|orbital?|orbiting|liftoff|rockets?|starship|"
    r"falcon\s*9|dragon|payload|launch pad|boosters?|turbojets?|jet engines?|"
    r"airframes?|aircraft)\b",
    re.I,
)
_IN_LANE_TECH_OR_BUSINESS_RE = re.compile(
    r"\b(?:ai|models?|agents?|software|saas|apis?|developers?|enterprise|"
    r"business|customers?|revenue|pricing|product|platform|workflow|"
    r"automation|startup|founder|growth)\b",
    re.I,
)

_CONTEXT_FRAGMENT_OPEN_RE = re.compile(
    r"^(?:@[A-Za-z0-9_]+\s+)*(?:exactly|agreed|agree|same|thanks|thank you|"
    r"lol|lmao|haha|great point|good point|love this|so true)\b",
    re.I,
)

# Replies from approved high-reach authors are source material, not the draft
# we send. Keep this gate structural rather than editorial: concise claims can
# be useful inputs, and Claude still has to pass the full relevance, editorial,
# and grounding gates before Telegram sees anything.
_SELF_CONTAINED_REPLY_MIN_CHARS = 20
_SELF_CONTAINED_REPLY_MIN_ALPHA = 14
_SELF_CONTAINED_REPLY_MIN_WORDS = 3


def _self_contained_reply_issue(tw: dict, cfg: Config) -> str | None:
    if not cfg.x_detection.include_self_contained_replies:
        return "is a reply, not an original tweet"
    allowed = {
        handle.lstrip("@").casefold()
        for handle in cfg.x_detection.reply_source_handles
    }
    if tw["author"].lstrip("@").casefold() not in allowed:
        return "reply author is outside the approved reply-source set"
    text = str(tw["text"] or "").strip()
    parent_text = str(tw.get("parent_text") or "").strip()
    # When X supplied the replied-to post in the same stream event, the model
    # can judge the actual exchange rather than treating the target's line as
    # an isolated fragment. The semantic and grounding gates still reject a
    # hollow acknowledgement that offers no honest reply opportunity.
    if (
        parent_text
        and sum(character.isalpha() for character in parent_text) >= 20
        and len(re.findall(r"[A-Za-z][A-Za-z'-]*", parent_text)) >= 4
    ):
        return None
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
    if (
        len(text) < _SELF_CONTAINED_REPLY_MIN_CHARS
        or sum(character.isalpha() for character in text)
        < _SELF_CONTAINED_REPLY_MIN_ALPHA
        or len(words) < _SELF_CONTAINED_REPLY_MIN_WORDS
    ):
        return "reply is too short to be self-contained"
    if _CONTEXT_FRAGMENT_OPEN_RE.search(text):
        return "reply begins as a conversational fragment"
    return None


def normalize_tweet(evt: dict) -> dict:
    """Flatten the various shapes twitterapi.io may deliver into what we need."""
    tid = str(evt.get("id_str") or evt.get("id") or "")
    author = ""
    a = evt.get("author") or evt.get("user") or {}
    if isinstance(a, dict):
        author = a.get("screen_name") or a.get("userName") or a.get("username") or ""
    author = author or evt.get("screen_name") or ""
    created = evt.get("createdAt_epoch") or evt.get("created_at_epoch")
    if created is None:
        created = evt.get("created_at") or evt.get("createdAt")
    text = evt.get("text") or evt.get("full_text") or ""
    parent_text = str(evt.get("parent_text") or "").strip()
    parent_author = str(evt.get("parent_author") or "").lstrip("@").strip()
    quote_text = str(evt.get("quote_text") or "").strip()
    quote_author = str(evt.get("quote_author") or "").lstrip("@").strip()
    link_context = [
        item for item in (evt.get("link_context") or [])
        if isinstance(item, dict)
        and (str(item.get("title") or "").strip()
             or str(item.get("description") or "").strip())
    ][:3]
    media_context = [
        item for item in (evt.get("media_context") or [])
        if isinstance(item, dict) and str(item.get("alt_text") or "").strip()
    ][:3]
    context_text = str(text)
    if parent_text:
        target_label = author.lstrip("@") or "target account"
        parent_label = parent_author or "the parent author"
        context_text = (
            f"@{target_label}'s reply:\n{str(text).strip()}\n\n"
            f"In response to @{parent_label}:\n{parent_text}"
        )
    elif quote_text:
        target_label = author.lstrip("@") or "target account"
        quoted_label = quote_author or "the quoted author"
        context_text = (
            f"@{target_label}'s original quote post:\n{str(text).strip()}\n\n"
            f"Quoted post by @{quoted_label}:\n{quote_text}"
        )
    supplemental: list[str] = []
    for item in link_context:
        fields = [
            f"Title: {str(item.get('title') or '').strip()}"
            if str(item.get("title") or "").strip() else "",
            f"Description: {str(item.get('description') or '').strip()}"
            if str(item.get("description") or "").strip() else "",
        ]
        supplemental.append("\n".join(field for field in fields if field))
    for item in media_context:
        supplemental.append(
            "Creator-provided media alt text: "
            + str(item.get("alt_text") or "").strip()
        )
    if supplemental:
        context_text = (
            f"{context_text.strip()}\n\n"
            "Context supplied by X for the source post (untrusted evidence):\n"
            + "\n\n".join(supplemental)
        )
    return {
        "id": tid,
        "text": text,
        "context_text": context_text,
        "author": author,
        "created": created,
        "is_reply": bool(evt.get("is_reply") or evt.get("in_reply_to_status_id")
                         or evt.get("inReplyToId")),
        "is_retweet": bool(evt.get("is_retweet") or evt.get("retweeted_tweet")
                           or str(evt.get("text", "")).startswith("RT @")),
        "parent_id": str(evt.get("parent_id") or ""),
        "parent_text": parent_text,
        "parent_author": parent_author,
        "quote_id": str(evt.get("quote_id") or ""),
        "quote_text": quote_text,
        "quote_author": quote_author,
        "link_context": link_context,
        "media_context": media_context,
        "lang": evt.get("lang") or "en",
    }


def tweet_age_seconds(created, now: float):
    if created is None:
        return None
    if isinstance(created, (int, float)):
        return now - float(created)
    # best-effort parse of common string formats
    from email.utils import parsedate_to_datetime
    try:
        return now - parsedate_to_datetime(str(created)).timestamp()
    except Exception:
        return None


def should_skip(tw: dict, cfg: Config, state: State, now: float = None):
    """Return (skip: bool, reason: str). skip=False means it survives to the LLM."""
    now = now or time.time()

    if not tw["id"]:
        return True, "no tweet id"
    if tw["is_retweet"]:
        return True, "retweet"
    if tw["is_reply"]:
        reply_issue = _self_contained_reply_issue(tw, cfg)
        if reply_issue:
            return True, reply_issue
    if tw["lang"] != "en":
        return True, f"non-english ({tw['lang']})"
    # Approved reply sources already passed the structural check above. Do not
    # reapply the original-post length threshold and silently erase concise,
    # self-contained claims before semantic review.
    substantive_text = str(tw.get("context_text") or tw["text"] or "")
    if not tw["is_reply"] and len(substantive_text.strip()) < cfg.pacing.min_tweet_chars:
        return True, "too short / low substance"
    if state.already_replied(tw["id"]):
        return True, "already replied"

    age = tweet_age_seconds(tw["created"], now)
    if age is None:
        return True, "missing / unparseable timestamp"
    if age > cfg.pacing.max_tweet_age_seconds:
        return True, f"too old ({int(age)}s)"

    # obvious low-value shapes
    text = str(tw.get("context_text") or tw["text"]).strip().lower()
    has_x_context = bool(tw.get("link_context") or tw.get("media_context"))
    if (
        text.startswith("http") or (text.count("http") and len(text) < 60)
    ) and not has_x_context:
        return True, "just a link"
    if not tw["is_reply"] and sum(c.isalpha() for c in text) < 20:
        return True, "not enough words"
    if (
        _AEROSPACE_OPERATION_RE.search(text)
        and not _IN_LANE_TECH_OR_BUSINESS_RE.search(text)
    ):
        return True, "off-lane aerospace operation"
    if _FORBIDDEN_TOPIC_RE.search(text):
        return True, "forbidden politics / crime / entertainment topic"
    if _SPECIALIST_MEDICAL_RE.search(text):
        return True, "specialist medical claim"
    if _THIRD_PARTY_NEWS_REACTION_RE.search(text):
        return True, "third-party news reaction"

    return False, "candidate"
