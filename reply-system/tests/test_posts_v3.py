"""Original posts v3 (Oct 20 2026): every post is built from one real artifact.

Arshad's rule: every [bracket] in a post shape is real data from Pentra's logs
or Search Console, or the post is not written; the test is that removing the
data makes the post collapse. Artifacts arrive in Telegram (/artifact <facts>,
optionally with a screenshot); /howto asks for a checked Search Console how-to.
With no artifact at a slot, the bot asks for one instead of writing.
"""
import asyncio
import json
from pathlib import Path

from bot.generate import _artifact_tokens, _artifact_writer_system, artifact_posts
from bot.stances import HOW_TO_CARDS
from bot.telegram_approval import parse_artifact_message
from tests.test_bot import _FakeClient, _FakeMessages, _ToolMessage, _audit, _cfg
from tests.test_telegram_approval import cfg as approval_cfg, run, state

ARTIFACT = ('pentra wrote "how to price a cleaning business" for leadpilot.chat on Oct 2. '
            "day 1: position 41. day 14: position 7, 23 clicks. no backlinks.")
RECEIPT = ('pentra wrote "how to price a cleaning business" for leadpilot.chat on oct 2.\n'
           "day 1: position 41.\nday 14: position 7, 23 clicks.\n"
           "no backlinks. it answered the exact question people were typing.")


def _drafts(*drafts, missing=""):
    return ("draft_artifact_posts", {"drafts": list(drafts), "missing": missing})


def _draft(text, shape="receipt", **overrides):
    value = {"shape": shape, "text": text, "data_used": ["position 41", "position 7"],
             "founder_would_save": True, "competitor_could_post": False}
    value.update(overrides)
    return value


def test_artifact_and_howto_commands_are_parsed_from_text_and_photo_captions():
    assert parse_artifact_message({"text": "/artifact day 1: position 41"}) == {
        "kind": "artifact", "text": "day 1: position 41", "photo_file_id": ""}
    photo = {"caption": "/artifact gsc for leadpilot, 23 clicks",
             "photo": [{"file_id": "small"}, {"file_id": "large"}]}
    assert parse_artifact_message(photo) == {
        "kind": "artifact", "text": "gsc for leadpilot, 23 clicks", "photo_file_id": "large"}
    assert parse_artifact_message({"text": "/howto"})["kind"] == "howto"
    assert parse_artifact_message({"text": "/artifact"}) is None, "an artifact needs facts as text"
    assert parse_artifact_message({"text": "/status"}) is None
    assert parse_artifact_message({"text": "nice post"}) is None


def test_artifacts_are_stored_once_and_handed_out_oldest_first():
    s = state()
    first = s.add_post_artifact(kind="artifact", text="a", photo_file_id="p")
    s.add_post_artifact(kind="howto", text="")
    assert s.next_new_post_artifact()["id"] == first
    s.finish_post_artifact(first, status="drafted", now=1000.0)
    assert s.next_new_post_artifact()["kind"] == "howto"
    assert s.last_post_artifact_drafted_at() == 1000.0


def test_writer_carries_the_owners_shapes_the_bad_example_and_the_collapse_test():
    system = " ".join(_artifact_writer_system().split())
    for rule in ("day 1: position [41]", "pentra held back a draft today", "3 searches their customers make",
                 "in search console: performance, filter position [4] to [20]", "scar on them",
                 "remove the artifact's data", "never the numbers"):
        assert rule in system, rule
    assert "—" not in _artifact_writer_system() and "–" not in _artifact_writer_system()


def test_artifact_tokens_are_its_numbers_quotes_and_domains():
    tokens = _artifact_tokens(ARTIFACT)
    assert {"41", "7", "23", "14", "leadpilot.chat", "how to price a cleaning business"} <= tokens


def test_two_drafts_from_one_artifact_pass_the_gates():
    take = ("leadpilot.chat, oct 2: \"how to price a cleaning business\".\n"
            "position 41 on day 1, position 7 on day 14. 23 clicks, no backlinks.")
    client = _FakeClient([_drafts(_draft(RECEIPT), _draft(take, shape="take")),
                          ("audit_public_claims", _audit()), ("audit_public_claims", _audit())])
    drafts = artifact_posts(_cfg(), client, artifact_text=ARTIFACT)
    assert [d["shape"] for d in drafts] == ["receipt", "take"]
    assert drafts[0]["text"] == RECEIPT


def test_drafts_that_fail_the_owners_tests_are_dropped():
    filler = "posts that answer the exact question people type tend to rank.\nwrite those."
    invented = RECEIPT.replace("23 clicks", "230 clicks")
    # The invented number gets one rewrite; a rewrite that keeps it is still dropped.
    client = _FakeClient([_drafts(_draft(filler, shape="take"), _draft(invented)),
                          ("rewritten_post", {"text": invented})])
    trace = {}
    assert artifact_posts(_cfg(), client, artifact_text=ARTIFACT, trace=trace) == []
    reasons = " ".join(trace["rejected"].values())
    assert "artifact's data" in reasons and "unsupported number" in reasons
    copycat = _FakeClient([_drafts(_draft(RECEIPT, competitor_could_post=True))])
    trace = {}
    assert artifact_posts(_cfg(), copycat, artifact_text=ARTIFACT, trace=trace) == []
    assert "competitor" in trace["rejected"]["0"]


def test_no_draft_when_the_artifact_cannot_fill_a_shape():
    client = _FakeClient([_drafts(missing="the position before and after, and the date")])
    trace = {}
    assert artifact_posts(_cfg(), client, artifact_text="we shipped something", trace=trace) == []
    assert trace["missing"] == "the position before and after, and the date"


def test_howto_cards_are_exact_steps_with_their_numbers():
    assert len(HOW_TO_CARDS) >= 4
    for card in HOW_TO_CARDS:
        assert card["id"] and "search console" in card["text"].lower() or "site:" in card["text"]
        assert "—" not in card["text"] and "–" not in card["text"]


class _Approval:
    def __init__(self):
        self.sent = []

    async def send_post_draft(self, **kwargs):
        self.sent.append(("draft", kwargs))
        return {"message_id": len(self.sent)}

    async def send_photo(self, **kwargs):
        self.sent.append(("photo", kwargs))
        return {"message_id": len(self.sent)}

    async def send_message(self, text, **kwargs):
        self.sent.append(("message", text))
        return {"message_id": len(self.sent)}


def _v3_daemon(monkeypatch, drafts):
    from bot import daemon as daemon_module
    from bot.daemon import Daemon

    c = approval_cfg()
    c.post_prompt_version = "v3"
    c.pacing.standalone_posts_per_day = 2
    c.pacing.active_hours_utc = (0, 24)
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = c
    daemon.state = state()
    daemon.post_drafts = True
    daemon.original_anthropic = daemon.anthropic = object()
    daemon.approval = _Approval()
    calls = []

    def fake_artifact_posts(cfg_, client, *, artifact_text, recent_posts=None, trace=None, **_):
        calls.append(artifact_text)
        if trace is not None:
            trace["missing"] = "" if drafts else "the clicks"
        return list(drafts)

    async def never_publish(*args, **kwargs):
        raise AssertionError("v3 post drafts never publish through the X API")

    monkeypatch.setattr(daemon_module.generate, "artifact_posts", fake_artifact_posts)
    monkeypatch.setattr(daemon_module.generate, "standalone_post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no v1/v2 post in v3")))
    monkeypatch.setattr(daemon_module.poster, "post", never_publish)
    return daemon, calls


def test_an_artifact_becomes_two_drafts_with_its_screenshot(monkeypatch):
    daemon, calls = _v3_daemon(monkeypatch, [{"shape": "receipt", "text": RECEIPT},
                                             {"shape": "take", "text": "position 41 to 7"}])
    artifact_id = daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT, photo_file_id="shot")
    run(daemon.maybe_standalone())
    kinds = [kind for kind, _ in daemon.approval.sent]
    assert kinds == ["photo", "draft", "draft"]
    assert daemon.approval.sent[1][1]["post_text"] == RECEIPT
    assert "1/2" in daemon.approval.sent[1][1]["topic"] and "receipt" in daemon.approval.sent[1][1]["topic"]
    assert calls == [ARTIFACT]
    assert daemon.state.next_new_post_artifact() is None
    row = daemon.state.db.execute("SELECT status FROM post_artifacts WHERE id=?", (artifact_id,)).fetchone()
    assert row[0] == "drafted"
    assert daemon.state.count_today("post_draft") == 2


def test_an_artifact_that_fills_no_shape_says_what_is_missing(monkeypatch):
    daemon, _ = _v3_daemon(monkeypatch, [])
    daemon.state.add_post_artifact(kind="artifact", text="we shipped something")
    run(daemon.maybe_standalone())
    (kind, text), = [item for item in daemon.approval.sent if item[0] == "message"][:1]
    assert "the clicks" in text


def test_a_slot_asks_once_only_after_every_howto_card_is_used(monkeypatch):
    daemon, calls = _v3_daemon(monkeypatch, [])
    for _ in HOW_TO_CARDS:
        used = daemon.state.add_post_artifact(kind="howto", text="")
        daemon.state.finish_post_artifact(used, status="drafted", now=1.0)
    run(daemon.maybe_standalone())
    run(daemon.maybe_standalone())
    asks = [text for kind, text in daemon.approval.sent if kind == "message"]
    assert len(asks) == 1 and "/artifact" in asks[0]
    assert calls == []


def test_a_slot_with_no_artifact_posts_the_next_howto_card(monkeypatch):
    """Oct 21 2026: two slots in two days drafted nothing and only asked for an
    artifact. Now an empty slot uses the next checked how-to card."""
    daemon, calls = _v3_daemon(monkeypatch, [{"shape": "how_to", "text": "10-minute seo win"},
                                             {"shape": "how_to", "text": "another 10-minute win"}])
    run(daemon.maybe_standalone())
    assert calls == [HOW_TO_CARDS[0]["text"]]
    assert [kind for kind, _ in daemon.approval.sent] == ["draft", "draft"]
    assert daemon.state.howto_artifacts_done() == 1


def test_howto_command_uses_the_next_checked_card(monkeypatch):
    daemon, calls = _v3_daemon(monkeypatch, [{"shape": "how_to", "text": "10-minute seo win"}])
    daemon.state.add_post_artifact(kind="howto", text="")
    run(daemon.maybe_standalone())
    assert calls == [HOW_TO_CARDS[0]["text"]]


def test_live_config_publishes_v3_posts_automatically():
    live = json.loads(Path(__file__).resolve().parent.parent.joinpath("config.json").read_text())
    assert live["post_prompt_version"] in ("v3", "v4"), "v4 keeps the v3 artifact path"
    assert live["pacing"]["standalone_posts_per_day"] == 2
    # Oct 21 2026, Arshad: "automate posts... stop adding my manual intervention".
    assert live["publishing"]["original_posts_review_first"] is False
    assert live["publishing"]["original_posts_authorized"] is True


def test_the_reply_service_stores_artifacts_from_the_owners_chat():
    from bot.replies_daemon import OfficialReplyRunner

    class Approval:
        def __init__(self):
            self.messages = []

        async def send_message(self, text, **kwargs):
            self.messages.append(text)

    class D:
        pass

    daemon = D()
    daemon.state = state()
    daemon.approval = Approval()
    runner = OfficialReplyRunner.__new__(OfficialReplyRunner)
    runner.daemon = daemon
    runner.cfg = approval_cfg()
    stored = run(runner._store_artifact_message({"caption": "/artifact day 1: position 41",
                                                 "photo": [{"file_id": "shot"}]}))
    assert stored and daemon.state.next_new_post_artifact()["photo_file_id"] == "shot"
    assert run(runner._store_artifact_message({"text": "/artifact"})) is True, "answered, not stored"
    assert "facts as text" in daemon.approval.messages[-1]
    assert run(runner._store_artifact_message({"text": "/status"})) is False


def test_feed_artifacts_wait_for_a_slot_while_telegram_artifacts_draft_now(monkeypatch):
    """The Pentra window's daily artifacts arrive by feed and fill the next
    slot (two drafts per slot, the owner's spec); an artifact the owner sends
    in Telegram is drafted at once."""
    from datetime import datetime, timezone

    daemon, calls = _v3_daemon(monkeypatch, [{"shape": "receipt", "text": RECEIPT}])
    hour = datetime.now(timezone.utc).hour
    daemon.cfg.pacing.active_hours_utc = ((hour + 2) % 24, (hour + 3) % 24)  # no slot now
    daemon.state.add_post_artifact(kind="artifact", text="feed facts 41 7", source="feed")
    run(daemon.maybe_standalone())
    assert calls == []
    daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT)
    run(daemon.maybe_standalone())
    assert calls == [ARTIFACT], "a Telegram artifact does not wait"
    daemon.cfg.pacing.active_hours_utc = (0, 24)
    run(daemon.maybe_standalone())
    assert calls == [ARTIFACT, "feed facts 41 7"]
    assert not [text for kind, text in daemon.approval.sent if kind == "message"], "the slot was filled, not asked"


def test_feed_messages_become_queued_artifacts_once():
    from bot.leads import queue_feed_artifacts

    s = state()
    good = {"id": "receipt-1", "kind": "receipt", "text": ARTIFACT}
    messages = [{"id": "m1", "message": json.dumps(good)}, {"id": "m2", "message": json.dumps(good)},
                {"id": "m3", "message": json.dumps({"id": "x", "text": ""})}, {"id": "m4", "message": "nope"}]
    assert queue_feed_artifacts(s, messages) == {"queued": 1, "duplicates": 1, "invalid": 2}
    assert queue_feed_artifacts(s, messages)["queued"] == 0
    queued = s.next_new_post_artifact(source="feed")
    assert queued["text"] == ARTIFACT and s.next_new_post_artifact(source="telegram") is None


def test_no_copy_overstates_what_holds_a_draft():
    """Oct 20 2026: Pentra's homepage said a separate fact check "blocks any
    claim it can't support". Under rule 2a only objective checks hold an
    Autopilot draft: a statistic its cited source doesn't contain, an uncited
    outcome figure, or a price, plan or product number the owner never
    confirmed. The X system's own copy overstated it the same way."""
    from bot import generate, stances

    texts = [stances.PRODUCT_SUMMARY, generate._artifact_writer_system()]
    texts += [fact for card in stances.PLAYBOOK for fact in card["facts"]]
    joined = " ".join(" ".join(texts).split()).lower()
    for phrase in ("blocks any claim", "every checkable claim", "fact check blocked",
                   "nothing goes out that the business didn't confirm"):
        assert phrase not in joined, phrase


def test_a_premium_length_post_is_kept_and_an_over_long_one_is_shortened():
    """Oct 21 2026: both drafts of the homepage-correction artifact were thrown
    away as "too long" (about 300 and 420 characters). @pentradev has Premium:
    up to 400 characters passes, and a longer draft gets one shortening pass."""
    longer = RECEIPT + "\nthe page matched what people typed, word for word, so a new domain could rank it by day 14."
    too_long = longer + ("\nno backlinks, no outreach, no paid tools. one page that answered the question "
                         "the way people asked it, and position 41 became position 7.")
    assert 280 < len(longer) <= 400 < len(too_long)
    client = _FakeClient([_drafts(_draft(longer), _draft(too_long, shape="take")),
                          ("audit_public_claims", _audit()),
                          ("shortened_post", {"text": RECEIPT}),
                          ("audit_public_claims", _audit())])
    trace = {}
    drafts = artifact_posts(_cfg(), client, artifact_text=ARTIFACT, trace=trace)
    assert [d["text"] for d in drafts] == [longer, RECEIPT], trace
    assert trace["rejected"] == {}


def test_a_rejected_artifact_says_which_check_failed(monkeypatch):
    """The note read "Missing: none, artifact supports both drafts" when both
    drafts had failed "too long" (Oct 21 2026)."""
    from bot import daemon as daemon_module

    daemon, _ = _v3_daemon(monkeypatch, [])

    def rejected_both(cfg_, client, *, artifact_text, recent_posts=None, trace=None, **_):
        trace.update({"missing": "none, artifact supports both drafts", "rejected": {"0": "too long", "1": "too long"}})
        return []

    monkeypatch.setattr(daemon_module.generate, "artifact_posts", rejected_both)
    daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT)
    run(daemon.maybe_standalone())
    (kind, text), = [item for item in daemon.approval.sent if item[0] == "message"][:1]
    assert "too long" in text and "Missing: none" not in text


def test_a_quote_with_punctuation_inside_the_marks_still_counts_as_artifact_data():
    """Oct 21 2026: a draft quoting the old homepage line with the period inside
    the quote marks shared only one token with its artifact and was rejected."""
    artifact = 'Correction, Oct 20: the homepage said a fact check "blocks any claim it can\'t support".'
    draft = 'our homepage said fact check "blocks any claim it can\'t support." fixed oct 20.'
    assert len(_artifact_tokens(draft) & _artifact_tokens(artifact)) >= 2


def test_the_shortening_pass_is_told_to_cut_to_260_characters():
    """Told to keep every number and name, the model returned a 430-character
    post unchanged (Oct 21 2026)."""
    from bot.generate import _shorten_post

    seen = []

    class Recording(_FakeMessages):
        def create(self, **kwargs):
            seen.append(kwargs)
            return super().create(**kwargs)

    client = _FakeClient([])
    client.messages = Recording([("shortened_post", {"text": "short"})])
    assert _shorten_post(_cfg(), client, text="x" * 430, artifact_text=ARTIFACT) == "short"
    system = seen[0]["system"][0]["text"]
    assert "at most 260 characters" in system and "drop whole sentences" in system
    assert seen[0]["tools"][0]["input_schema"]["properties"]["text"]["maxLength"] == 280


def test_a_shape_stage_direction_never_reaches_the_post():
    """Oct 20 2026: the only v3 draft sent ended with "(with the screenshot)",
    copied from the receipt shape, and no screenshot existed."""
    leaked = RECEIPT + "\n(with the screenshot)"
    client = _FakeClient([_drafts(_draft(leaked)), ("audit_public_claims", _audit())])
    drafts = artifact_posts(_cfg(), client, artifact_text=ARTIFACT)
    assert [d["text"] for d in drafts] == [RECEIPT]
    assert "(with the screenshot)" not in _artifact_writer_system()
    assert "never part of the post" in _artifact_writer_system()


def test_howto_drafts_are_not_held_to_the_two_quote_or_number_rule():
    """A how-to card's data is its steps; "inspect-new-page" has one quote and
    no numbers, so every draft from it failed the two-token rule."""
    card = next(c["text"] for c in HOW_TO_CARDS if c["id"] == "inspect-new-page")
    draft = ("after you publish a page, paste its url into the inspection bar in search console. "
             "if google hasn't indexed it yet, test the live url, then request indexing.")
    strict = artifact_posts(_cfg(), _FakeClient([_drafts(_draft(draft, shape="how_to"))]), artifact_text=card)
    assert strict == []
    client = _FakeClient([_drafts(_draft(draft, shape="how_to")), ("audit_public_claims", _audit())])
    relaxed = artifact_posts(_cfg(), client, artifact_text=card, min_shared_tokens=0)
    assert [d["text"] for d in relaxed] == [draft]


def test_a_canned_contrast_draft_gets_one_rewrite_instead_of_being_dropped():
    """Oct 21 2026: the second draft of each how-to card was dropped for a
    canned contrast, leaving one draft per slot instead of two."""
    contrast = RECEIPT + "\nthe fix isn't more content, it's the right page."
    client = _FakeClient([_drafts(_draft(contrast)),
                          ("rewritten_post", {"text": RECEIPT}),
                          ("audit_public_claims", _audit())])
    trace = {}
    drafts = artifact_posts(_cfg(), client, artifact_text=ARTIFACT, trace=trace)
    assert [d["text"] for d in drafts] == [RECEIPT], trace


def test_a_number_the_card_never_says_is_removed_and_a_howto_tip_may_be_generic():
    """Oct 21 2026 server replay: a draft from "inspect-new-page" said
    "10-minute" (copied from the how_to shape; the card never says it) and the
    other was dropped as something a competitor could post."""
    card = next(c["text"] for c in HOW_TO_CARDS if c["id"] == "inspect-new-page")
    with_number = "10-minute check after you publish: paste the url into search console's inspection bar."
    fixed = "after you publish: paste the url into search console's inspection bar."
    generic = ("after every publish, paste the url into the inspection bar. "
               "if it says url is not on google, test the live url and request indexing.")
    client = _FakeClient([_drafts(_draft(with_number, shape="how_to"),
                                  _draft(generic, shape="how_to", competitor_could_post=True)),
                          ("rewritten_post", {"text": fixed}),
                          ("audit_public_claims", _audit()),
                          ("audit_public_claims", _audit())])
    trace = {}
    drafts = artifact_posts(_cfg(), client, artifact_text=card, trace=trace, min_shared_tokens=0, generic_ok=True)
    assert [d["text"] for d in drafts] == [fixed, generic], trace


def test_automatic_mode_publishes_the_best_draft_and_tells_telegram_quietly(monkeypatch):
    """Oct 21 2026, Arshad: "automate posts... stop adding my manual
    intervention". The best passing draft (a receipt over a take, 280
    characters or less first) goes out through the official API."""
    from bot import daemon as daemon_module

    take = "position 41 to position 7 in 14 days, no backlinks."
    daemon, _ = _v3_daemon(monkeypatch, [{"shape": "take", "text": take}, {"shape": "receipt", "text": RECEIPT}])
    daemon.post_drafts = False
    published = []

    async def fake_post(cfg_, state_, text):
        published.append(text)
        return True

    monkeypatch.setattr(daemon_module.poster, "post", fake_post)
    artifact_id = daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT)
    run(daemon.maybe_standalone())
    assert published == [RECEIPT]
    assert [kind for kind, _ in daemon.approval.sent] == ["message"] and "Posted on @pentradev" in daemon.approval.sent[0][1]
    row = daemon.state.db.execute("SELECT status FROM post_artifacts WHERE id=?", (artifact_id,)).fetchone()
    assert row[0] == "published" and daemon.state.count_today("post") == 1
    assert daemon.state.last_post_artifact_drafted_at() > 0, "a published post counts for the slot spacing"


def test_a_refused_publish_is_reported_and_never_retried_as_published(monkeypatch):
    from bot import daemon as daemon_module

    daemon, _ = _v3_daemon(monkeypatch, [{"shape": "receipt", "text": RECEIPT}])
    daemon.post_drafts = False

    async def refused(cfg_, state_, text):
        raise daemon_module.poster.PublishError("official X create failed HTTP 403: not permitted")

    monkeypatch.setattr(daemon_module.poster, "post", refused)
    artifact_id = daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT)
    run(daemon.maybe_standalone())
    row = daemon.state.db.execute("SELECT status, note FROM post_artifacts WHERE id=?", (artifact_id,)).fetchone()
    assert row[0] == "failed" and "403" in row[1]
    assert daemon.state.count_today("post") == 0
    assert "Post not published" in daemon.approval.sent[0][1]


def test_a_failed_artifact_falls_back_to_a_howto_card_in_the_same_slot(monkeypatch):
    """Oct 21 2026: a queued artifact whose drafts all fail must not leave the
    slot empty; the next checked how-to card is published instead."""
    from bot import daemon as daemon_module

    daemon, _ = _v3_daemon(monkeypatch, [])
    daemon.post_drafts = False
    howto = "10-minute seo win: open performance, then pages. find positions 4 to 20."

    def only_howto(cfg_, client, *, artifact_text, recent_posts=None, trace=None, **_):
        if artifact_text == ARTIFACT:
            trace.update({"missing": "", "rejected": {"0": "canned contrast"}})
            return []
        return [{"shape": "how_to", "text": howto}]

    published = []

    async def fake_post(cfg_, state_, text):
        published.append(text)
        return True

    monkeypatch.setattr(daemon_module.generate, "artifact_posts", only_howto)
    monkeypatch.setattr(daemon_module.poster, "post", fake_post)
    daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT, source="feed")
    run(daemon.maybe_standalone())
    assert published == [howto]
    assert daemon.state.howto_artifacts_done() == 1
    assert not [text for kind, text in daemon.approval.sent if "No post from that artifact" in text]


def test_a_premium_length_draft_is_published_as_written(monkeypatch):
    """Oct 21 2026: X accepted the first automatic post at 361 characters, so a
    long draft is no longer cut before publishing (that cost a model call and
    sentences)."""
    from bot import daemon as daemon_module

    long_receipt = RECEIPT + "\nthe page matched what people typed, word for word, so a new domain could rank it by day 14."
    assert len(long_receipt) > 280
    daemon, _ = _v3_daemon(monkeypatch, [{"shape": "receipt", "text": long_receipt}])
    daemon.post_drafts = False
    published = []

    async def fake_post(cfg_, state_, text):
        published.append(text)
        return True

    monkeypatch.setattr(daemon_module.poster, "post", fake_post)
    daemon.state.add_post_artifact(kind="artifact", text=ARTIFACT)
    run(daemon.maybe_standalone())
    assert published == [long_receipt]


def test_an_artifact_post_with_a_label_opening_gets_one_rewrite():
    """Oct 23 2026: the label gate covers artifact posts, and the how_to shape
    no longer teaches "10-minute SEO win:"."""
    hooked = "SEO win: " + RECEIPT
    client = _FakeClient([_drafts(_draft(hooked)), ("rewritten_post", {"text": RECEIPT}),
                          ("audit_public_claims", _audit())])
    drafts = artifact_posts(_cfg(), client, artifact_text=ARTIFACT)
    assert [d["text"] for d in drafts] == [RECEIPT]
    assert "10-minute SEO win" not in _artifact_writer_system()
