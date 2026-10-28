import pytest

from xstream.rules import MAX_RULE_VALUE, build_rule_values, canonical_rule_specs


def test_handles_are_packed_lowercased_and_deduplicated():
    values = build_rule_values(["@Alice", "alice", "Bob"])
    assert values == ["(from:alice OR from:bob) -is:reply -is:retweet lang:en"]


def test_many_handles_split_into_rules_under_the_limit():
    handles = [f"creator_{i:04d}" for i in range(120)]
    values = build_rule_values(handles)
    assert len(values) > 1
    assert all(len(v) <= MAX_RULE_VALUE for v in values)
    joined = " ".join(values)
    assert all(f"from:{h}" in joined for h in handles)


def test_long_keyword_lists_split_across_rules_and_every_word_is_kept():
    words = [f"topic{i}" for i in range(150)] + ["build in public"]
    values = build_rule_values(["alice", "bob"], words)
    assert len(values) > 1 and all(len(v) <= MAX_RULE_VALUE for v in values)
    joined = " ".join(values)
    assert all(w in joined for w in words[:-1]) and '"build in public"' in joined
    assert all(v.startswith("(from:alice OR from:bob) (") for v in values)


def test_replies_and_language_are_configurable():
    assert build_rule_values(["alice"], include_replies=True, lang="") == ["(from:alice) -is:retweet"]


def test_specs_have_stable_tags_and_extra_rules_are_checked():
    specs = canonical_rule_specs(["alice"], "xsd", extra_rules=['"search console" -is:retweet'])
    assert [s.tag for s in specs] == ["xsd-001", "xsd-extra-001"]
    assert specs == canonical_rule_specs(["alice"], "xsd", extra_rules=['"search console" -is:retweet'])
    with pytest.raises(ValueError):
        canonical_rule_specs(["alice"], "xsd", extra_rules=["x" * (MAX_RULE_VALUE + 1)])
