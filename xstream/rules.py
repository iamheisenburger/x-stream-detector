"""Pack handles and topic words into the fewest legal filtered-stream rules."""
from __future__ import annotations

from dataclasses import dataclass

MAX_RULE_VALUE = 1024
# Leaves room for about 25 handles in each rule.
KEYWORD_CLAUSE_BUDGET = 480


@dataclass(frozen=True)
class RuleSpec:
    value: str
    tag: str


def keyword_clause(keywords: list[str] | None) -> str:
    terms = [
        f'"{k}"' if " " in k else k
        for k in dict.fromkeys(str(k).strip() for k in (keywords or []))
        if k
    ]
    return f" ({' OR '.join(terms)})" if terms else ""


def keyword_chunks(keywords: list[str] | None) -> list[list[str]]:
    """Split a long topic list so every rule stays under X's length limit.

    X delivers (and bills) a post once even when several rules match it, so
    splitting the topic words across rules changes nothing but rule count.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    for word in dict.fromkeys(str(k).strip() for k in (keywords or []) if str(k).strip()):
        if current and len(keyword_clause(current + [word])) > KEYWORD_CLAUSE_BUDGET:
            chunks.append(current)
            current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks or [[]]


def build_rule_values(
    handles: list[str],
    keywords: list[str] | None = None,
    *,
    lang: str = "en",
    include_replies: bool = False,
) -> list[str]:
    """Without keywords every original post from the handles is delivered.
    With keywords, only posts containing one of them are delivered (and
    billed); long keyword lists are split across rules. All rules share the
    one stream connection."""
    normalized = sorted({h.lstrip("@").strip().lower() for h in handles if h.lstrip("@").strip()})
    if not normalized:
        return []
    filters = ("" if include_replies else " -is:reply") + " -is:retweet" + (f" lang:{lang}" if lang else "")

    values: list[str] = []
    for chunk in keyword_chunks(keywords):
        suffix = f"){keyword_clause(chunk)}{filters}"
        terms: list[str] = []
        for handle in normalized:
            term = f"from:{handle}"
            candidate = f"({' OR '.join(terms + [term])}{suffix}"
            if len(candidate) > MAX_RULE_VALUE:
                if not terms:
                    raise ValueError(f"X handle cannot fit in one rule: {handle}")
                values.append(f"({' OR '.join(terms)}{suffix}")
                terms = [term]
            else:
                terms.append(term)
        if terms:
            values.append(f"({' OR '.join(terms)}{suffix}")
    return values


def canonical_rule_specs(
    handles: list[str],
    namespace: str,
    *,
    keywords: list[str] | None = None,
    extra_rules: list[str] | None = None,
    lang: str = "en",
    include_replies: bool = False,
) -> list[RuleSpec]:
    """The exact rule set, with stable tags, so a restart never re-adds rules."""
    values = build_rule_values(handles, keywords, lang=lang, include_replies=include_replies)
    specs = [RuleSpec(value=v, tag=f"{namespace}-{i:03d}") for i, v in enumerate(values, start=1)]
    extra = [
        RuleSpec(value=str(v).strip(), tag=f"{namespace}-extra-{i:03d}")
        for i, v in enumerate(extra_rules or [], start=1)
        if str(v).strip()
    ]
    if any(len(rule.value) > MAX_RULE_VALUE for rule in extra):
        raise ValueError(f"an extra rule exceeds X's {MAX_RULE_VALUE}-character limit")
    return specs + extra
