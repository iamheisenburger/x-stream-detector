"""Original posts v4 (Oct 22 2026): posts that teach the reader one thing.

Arshad: "our posts need to offer insights/value to readers... its just an
update, there is no learning lesson here". The research playbook: every post
teaches one thing a founder can act on; every number comes from a sourced card
named in the text; Pentra is never the subject of line one; the common
AI-writing tells are gated because X labels "llm_slop" accounts and drops their
posts from For You for 30 days.
"""
import json
from pathlib import Path

from bot.generate import value_post
from bot.post_cards import EVIDENCE_CARDS, POST_FORMATS, POST_PLAN, card, plan_entry
from tests.test_bot import _FakeClient, _audit, _cfg
from tests.test_posts_v3 import _v3_daemon
from tests.test_telegram_approval import run

TOP10 = plan_entry(0)[1]  # page-two how-to + the ahrefs top-10 card
GOOD = ("before writing a new post, find the pages google already shows on page two.\n\n"
        "in search console: performance, then the pages tab. turn on average position and filter to positions "
        "greater than 3 and smaller than 21.\n\n"
        "ahrefs, july 2025: 76.1% of pages cited in ai overviews ranked in google's top 10.\n\n"
        "rewrite the first paragraph of your top page there to answer its top query.")


def _writer(text, skip=False):
    return ("value_post", {"text": text, "skip": skip, "reason": "teaches the page-two check"})


def test_every_plan_entry_uses_real_cards_with_named_sources():
    for format_id, card_ids in POST_PLAN:
        assert format_id in POST_FORMATS
        for card_id in card_ids:
            c = card(card_id)
            assert c["text"] and c["source"], card_id
            if not card_id.startswith("howto:"):
                assert EVIDENCE_CARDS[card_id]["url"].startswith("https://"), card_id
    # B7 (Oct 23 2026): fourteen days at two posts a day before anything repeats.
    assert len(POST_PLAN) >= 28, "fourteen days at two posts a day before anything repeats (B7)"
    assert len({(f, tuple(c)) for f, c in POST_PLAN}) == len(POST_PLAN), "no entry appears twice"


def test_a_value_post_passes_when_every_number_is_in_its_cards():
    client = _FakeClient([_writer(GOOD), ("audit_public_claims", _audit())])
    post = value_post(_cfg(), client, format_id="search_console_check", cards=TOP10)
    assert post == {"text": GOOD, "format": "search_console_check"}


def test_a_number_the_cards_never_state_is_never_published():
    invented = GOOD.replace("76.1% of pages", "90% of pages")
    client = _FakeClient([_writer(invented), ("rewritten_post", {"text": invented})])
    trace = {}
    assert value_post(_cfg(), client, format_id="search_console_check", cards=TOP10, trace=trace) is None
    assert trace["rejected"].startswith("unsupported number")


def test_pentra_as_the_subject_of_line_one_gets_one_rewrite():
    about_us = "pentra found a 10-minute search console check.\n\n" + GOOD
    client = _FakeClient([_writer(about_us), ("rewritten_post", {"text": GOOD}), ("audit_public_claims", _audit())])
    assert value_post(_cfg(), client, format_id="search_console_check", cards=TOP10)["text"] == GOOD


def test_ai_writing_tells_are_rewritten_and_dropped_if_they_stay():
    sloppy = GOOD + "\n\nlet that sink in."
    client = _FakeClient([_writer(sloppy), ("rewritten_post", {"text": sloppy})])
    trace = {}
    assert value_post(_cfg(), client, format_id="search_console_check", cards=TOP10, trace=trace) is None
    assert trace["rejected"] == "ai tell"


def test_a_v4_slot_publishes_the_next_plan_post_and_skips_a_failing_entry(monkeypatch):
    from bot import daemon as daemon_module

    daemon, _ = _v3_daemon(monkeypatch, [])
    daemon.cfg.post_prompt_version = "v4"
    daemon.post_drafts = False
    calls, published = [], []

    def fake_value_post(cfg_, client, *, format_id, cards, recent_posts=None, trace=None):
        calls.append(format_id)
        if len(calls) == 1:
            trace["rejected"] = "grounding audit: overreach"
            return None
        return {"text": GOOD, "format": format_id}

    async def fake_post(cfg_, state_, text):
        published.append(text)
        return True

    monkeypatch.setattr(daemon_module.generate, "value_post", fake_value_post)
    monkeypatch.setattr(daemon_module.poster, "post", fake_post)
    run(daemon.maybe_standalone())
    assert calls == [POST_PLAN[0][0], POST_PLAN[1][0]]
    assert published == [GOOD]
    assert daemon.state.plan_posts_done() == 2
    rows = daemon.state.db.execute("SELECT status FROM post_artifacts WHERE kind='plan' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["failed", "published"]


def test_live_config_runs_posts_v4():
    live = json.loads(Path(__file__).resolve().parent.parent.joinpath("config.json").read_text())
    assert live["post_prompt_version"] == "v4"
    assert live["publishing"]["original_posts_review_first"] is False, "Arshad, Oct 23: keep posts automatic"


def test_numbered_steps_are_not_read_as_invented_numbers():
    """Oct 22 dry run: "1. open search console" was rejected as "unsupported
    number: 1", and step lists are the point of the search console check."""
    steps = ("before writing a new post, find the pages google already shows on page two.\n\n"
             "1. search console, performance, pages tab\n2. turn on average position\n"
             "3. filter to positions greater than 3 and smaller than 21\n\n"
             "ahrefs, july 2025: 76.1% of pages cited in ai overviews ranked in google's top 10.")
    client = _FakeClient([_writer(steps), ("audit_public_claims", _audit())])
    assert value_post(_cfg(), client, format_id="search_console_check", cards=TOP10)["text"] == steps


def test_a_contrast_rewrite_is_told_to_drop_the_not():
    """Oct 22 dry run: "those are notes, not reasons to hold a page back" kept
    its contrast through a rewrite told only to "say the point directly"."""
    from bot.generate import _repair_post
    from tests.test_bot import _FakeMessages

    seen = []

    class Recording(_FakeMessages):
        def create(self, **kwargs):
            seen.append(kwargs)
            return super().create(**kwargs)

    client = _FakeClient([])
    client.messages = Recording([("rewritten_post", {"text": "style notes can wait."})])
    _repair_post(_cfg(), client, text="style scores are notes, not reasons to hold a page back.",
                 artifact_text="editorial scores are notes", issue="canned contrast")
    assert 'no "not" in it' in seen[0]["messages"][0]["content"]


def test_no_format_instruction_carries_a_number_the_cards_lack():
    """Oct 22 dry run: "for a 1 to 20 person business" in the study format made
    the writer write "1-20", which the number guard rejected (it isn't in any
    card)."""
    import re
    for format_id, fmt in POST_FORMATS.items():
        assert not re.search(r"\d", fmt["structure"]), format_id


def test_a_domain_is_rewritten_before_it_can_make_the_post_cost_a_link():
    """Oct 22 19:30: the schema myth post said "schema.org", which the publisher
    prices as a link post ($0.20, over the $0.05 daily cap), so it was refused."""
    cards = plan_entry(2)[1]
    with_domain = ('"add schema so ai cites you."\n\nahrefs tracked 1,885 pages that added json-ld schema '
                   "(aug 2025 to mar 2026): ai overview citations fell 4.6%.\n\ngoogle's guide: no special "
                   "schema.org markup for generative ai search. skip the schema project this week.")
    plain = with_domain.replace("schema.org markup", "schema markup")
    client = _FakeClient([_writer(with_domain), ("rewritten_post", {"text": plain}), ("audit_public_claims", _audit())])
    assert value_post(_cfg(), client, format_id="myth_vs_source", cards=cards)["text"] == plain


def test_naming_the_source_is_not_blocked_as_meta_talk():
    """Oct 22 19:30: the Pew study post failed "source-meta copy", a reply rule,
    though naming the source is the point of a value post."""
    cards = plan_entry(1)[1]
    study = ("pew tracked the google searches of 900 us adults in march 2025.\n\n"
             "the source says people clicked a normal result on 8% of visits with an ai summary, 15% without.\n\n"
             "this week, check which of your pages lose clicks while impressions hold.")
    client = _FakeClient([_writer(study), ("audit_public_claims", _audit())])
    assert value_post(_cfg(), client, format_id="study_translated", cards=cards)["text"] == study


def test_a_label_opening_gets_one_rewrite_and_the_writer_asks_for_plain_capitals():
    """Oct 23 2026: "10-minute SEO win: open search console..." opened the post
    of the day X limited the account; a label and a colon is how templated
    posts open, and every post was lowercase."""
    from bot.generate import _LABEL_HOOK_RE, _value_post_system

    for hook in ("10-minute SEO win: open Search Console", "SEO tip: check your titles", "Myth: schema gets you cited"):
        assert _LABEL_HOOK_RE.search(hook), hook
    for line in ("Pew tracked 900 people's searches:", "Before an AI-written page goes live, check four things.",
                 "Your Search Console has a report for AI traffic now."):
        assert not _LABEL_HOOK_RE.search(line), line
    system = " ".join(_value_post_system("search_console_check").split())
    assert "normal capitalization" in system and "lowercase" not in system and "never a label and a colon" in system
    cards = plan_entry(2)[1]
    hooked = ('Myth: "add schema so AI cites you."\n\nAhrefs tracked 1,885 pages that added JSON-LD schema '
              "(Aug 2025 to Mar 2026): AI Overview citations fell 4.6%.\n\nGoogle's guide: no special schema "
              "markup for generative AI search. Skip the schema project this week.")
    plain = hooked.replace('Myth: "add schema', '"Add schema')
    client = _FakeClient([_writer(hooked), ("rewritten_post", {"text": plain}), ("audit_public_claims", _audit())])
    assert value_post(_cfg(), client, format_id="myth_vs_source", cards=cards)["text"] == plain


def test_post_formats_are_shorter_and_promise_no_time():
    """Oct 23 2026: shorter, plain posts; "the time it takes" and "in ten
    minutes" produced the "10-minute" label opening."""
    assert all(fmt["max_chars"] <= 800 for fmt in POST_FORMATS.values())
    for fmt in POST_FORMATS.values():
        assert "time it takes" not in fmt["structure"] and "minutes" not in fmt["structure"]


def test_a_prevalence_word_the_cards_lack_gets_one_rewrite():
    """Oct 23 2026 dry run: a search console post was dropped for "unsupported
    prevalence", which value posts never rewrote."""
    cards = plan_entry(1)[1]
    study = ("Pew tracked the Google searches of 900 US adults in March 2025.\n\n"
             "Most people clicked a normal result on 8% of visits with an AI summary, 15% without.\n\n"
             "This week, check which of your pages lose clicks while impressions hold.")
    fixed = study.replace("Most people clicked", "People clicked")
    client = _FakeClient([_writer(study), ("rewritten_post", {"text": fixed}), ("audit_public_claims", _audit())])
    out = value_post(_cfg(), client, format_id="study_translated", cards=cards)
    assert out and out["text"] == fixed, out


def test_how_to_cards_carry_no_label_or_promised_time():
    """Oct 23 2026: every how-to card opened with "10-minute seo win in Search
    Console:", which the writer copied into the post of the day X limited the
    account."""
    from bot.generate import _LABEL_HOOK_RE
    from bot.stances import HOW_TO_CARDS

    for howto in HOW_TO_CARDS:
        assert not _LABEL_HOOK_RE.search(howto["text"].splitlines()[0]), howto["id"]
        assert "minute" not in howto["text"].lower(), howto["id"]
        assert howto["text"][0].isupper(), howto["id"]


def test_file_names_are_not_priced_as_link_posts():
    """Oct 23 2026: "robots.txt" and "llms.txt" read as links to the publisher
    ($0.20, over the $0.05 daily write cap), so SEO posts naming them would be
    refused like the Oct 22 "schema.org" post."""
    from bot.poster import _URL_LIKE_RE, _create_cost_microusd
    from bot.stances import HOW_TO_CARDS

    for name in ("robots.txt", "sitemap.xml", "llms.txt", "index.html"):
        assert not _URL_LIKE_RE.search(f"Check your {name} first."), name
    for link in ("schema.org", "pentra.dev", "https://x.com/a", "www.example.com", "example.com/robots.txt"):
        assert _URL_LIKE_RE.search(link), link
    cfg = _cfg()
    assert _create_cost_microusd(cfg, "Check robots.txt.") == cfg.publishing.create_microusd
    assert not any(_URL_LIKE_RE.search(c["text"]) for c in HOW_TO_CARDS)
    assert not any(_URL_LIKE_RE.search(c["text"]) for c in EVIDENCE_CARDS.values())


def test_an_absolute_claim_in_a_post_gets_one_rewrite_and_steps_come_from_the_cards():
    """Oct 23 2026 dry run: the sitemap how-to was dropped for an absolute claim
    (posts never rewrote it), and the links how-to for a Search Console step
    the writer added, which the prompt had invited ("the reader's next step is yours")."""
    from bot.generate import _value_post_system

    cards = plan_entry(1)[1]
    study = ("Pew tracked the Google searches of 900 US adults in March 2025.\n\n"
             "An AI summary guarantees fewer clicks: 8% of visits with one, 15% without.\n\n"
             "This week, check which of your pages lose clicks while impressions hold.")
    fixed = study.replace("An AI summary guarantees fewer clicks:", "With an AI summary, people clicked a result on")
    client = _FakeClient([_writer(study), ("rewritten_post", {"text": fixed}), ("audit_public_claims", _audit())])
    out = value_post(_cfg(), client, format_id="study_translated", cards=cards)
    assert out and out["text"] == fixed, out
    system = " ".join(_value_post_system("search_console_check").split())
    assert "a step inside Search Console or another tool must be one a card gives" in system


def test_a_held_plan_post_is_retried_on_a_later_day_and_nothing_repeats_within_14_days():
    """Oct 23 2026: each held entry used up a plan slot until the plan wrapped,
    and with half the attempts held the 32-entry plan repeated after about 8
    days of posts."""
    from tests.test_telegram_approval import state as make_state

    s, now = make_state(), 2_000_000_000.0

    def row(index, status, age):
        rid = s.add_post_artifact(kind="plan", text=json.dumps({"index": index}), source="auto")
        s.db.execute("UPDATE post_artifacts SET status=?, updated_at=? WHERE id=?", (status, now - age, rid))
        s.db.commit()

    assert s.next_plan_index(4, now=now) == 0
    row(0, "published", 3 * 86_400)
    row(1, "failed", 3 * 86_400)
    row(2, "published", 2 * 86_400)
    row(3, "failed", 3_600)
    assert s.next_plan_index(4, now=now) == 1, "held 3 days ago: tried again; 0 and 2 were published inside 14 days"
    row(1, "published", 60)
    assert s.next_plan_index(4, now=now) == 0, "everything excluded: the oldest publish comes back"
    assert s.next_plan_index(4, now=now + 15 * 86_400) == 2, "after 14 days entries are eligible again, in order"
