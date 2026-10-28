import json
from pathlib import Path

import pytest

from xstream.settings import load
from xstream.sinks import reply_link, signature, telegram_text

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def test_the_example_config_loads_and_secrets_come_from_the_environment():
    settings = load(str(EXAMPLE), env={"X_BEARER_TOKEN": "t"})
    assert settings.handles and settings.x_bearer_token == "t"
    assert settings.active_hours_utc == (12, 23)


def test_secrets_in_the_config_file_are_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"handles": ["alice"], "x_api_key": "k"}))
    with pytest.raises(ValueError, match="environment"):
        load(str(path), env={})


def test_typos_and_bad_handles_are_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"handles": ["alice"], "keyword": ["seo"]}))
    with pytest.raises(ValueError, match="keyword"):
        load(str(path), env={})
    path.write_text(json.dumps({"handles": ["not a handle"]}))
    with pytest.raises(ValueError, match="handle"):
        load(str(path), env={})


def test_telegram_message_carries_the_post_and_a_one_tap_reply_link():
    event = {"id": "100", "url": "https://x.com/alice/status/100", "text": "hello", "created_at_epoch": 1000.0,
             "author": {"username": "alice"}}
    text = telegram_text(event, now=1005.0)
    assert text.startswith("@alice · 5s ago") and reply_link("100") in text
    assert reply_link("100") == "https://x.com/intent/post?in_reply_to=100"


def test_webhook_signature_is_hmac_sha256_of_the_body():
    import hashlib
    import hmac

    assert signature("s", b"{}") == "sha256=" + hmac.new(b"s", b"{}", hashlib.sha256).hexdigest()
