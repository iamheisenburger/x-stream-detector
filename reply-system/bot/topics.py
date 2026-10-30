"""Local approximation of X's keyword matching for stream rule keywords.

X matches rule keywords case-insensitively against whole tokens; quoted phrases
match consecutive tokens. The pool builder uses this to estimate how many of an
account's original posts a keyword-scoped rule would deliver (and bill).
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def matches_keywords(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    words = tokens(text)
    if not words:
        return False
    word_set = set(words)
    joined = " " + " ".join(words) + " "
    for keyword in keywords:
        parts = tokens(keyword)
        if not parts:
            continue
        if len(parts) == 1:
            if parts[0] in word_set:
                return True
        elif " " + " ".join(parts) + " " in joined:
            return True
    return False
