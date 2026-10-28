import calendar

from xstream.events import parse_stream_event

PAYLOAD = {
    "data": {
        "id": "100",
        "author_id": "1",
        "text": "Shipped the new pricing page today. Conversion is up 18% so…",
        "note_tweet": {"text": "Shipped the new pricing page today. Conversion is up 18% so far, here is what changed."},
        "created_at": "2026-10-27T12:00:00.000Z",
        "lang": "en",
        "referenced_tweets": [{"type": "quoted", "id": "90"}],
        "entities": {
            "urls": [
                {"expanded_url": "https://example.com/a", "title": "Pricing teardown", "description": "What we changed"},
                {"expanded_url": "https://example.com/bare"},
            ]
        },
        "attachments": {"media_keys": ["3_1"]},
    },
    "includes": {
        "users": [
            {"id": "1", "username": "alice", "name": "Alice", "public_metrics": {"followers_count": 52000}},
            {"id": "2", "username": "bob", "name": "Bob"},
        ],
        "tweets": [{"id": "90", "author_id": "2", "text": "Pricing pages are underrated."}],
        "media": [{"media_key": "3_1", "type": "photo", "alt_text": "Before and after screenshot"}],
    },
    "matching_rules": [{"id": "r1", "tag": "xsd-001"}],
}


def test_a_stream_payload_becomes_a_flat_post():
    event = parse_stream_event(PAYLOAD)
    assert event["id"] == "100" and event["url"] == "https://x.com/alice/status/100"
    assert event["text"].endswith("here is what changed."), "the full long-post text, not the truncated one"
    assert event["author"]["username"] == "alice" and event["author"]["public_metrics"]["followers_count"] == 52000
    assert event["created_at_epoch"] == calendar.timegm((2026, 10, 27, 12, 0, 0))
    assert event["is_quote"] and not event["is_reply"]
    assert (event["quote_author"], event["quote_text"]) == ("bob", "Pricing pages are underrated.")
    assert event["link_context"] == [
        {"url": "https://example.com/a", "title": "Pricing teardown", "description": "What we changed"}
    ]
    assert event["media_context"] == [{"type": "photo", "alt_text": "Before and after screenshot"}]
    assert event["matching_rule_tags"] == ["xsd-001"]
    assert event["included_post_ids"] == ["90"], "the quoted post is booked as a read too"


def test_keep_alives_and_errors_are_not_posts():
    assert parse_stream_event({}) is None
    assert parse_stream_event({"errors": [{"title": "operational-disconnect"}]}) is None
