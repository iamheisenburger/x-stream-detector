"""Original posts v2 (Oct 20 2026): written for the founder who clicked through
from one of our replies, not about Pentra's internals.

The v1 writer could only use build receipts and its editor rejected anything not
"recognizably about Pentra", so the posts read as release notes (4 and 11
views); the owner called them the most unengaging posts he had seen.
"""
import json
from pathlib import Path

from bot.generate import (
    _post_judge_system_v2,
    _post_writer_system,
    _post_writer_system_v2,
    _select_evidence_card,
    standalone_post,
)
from bot.stances import CONTENT_EVIDENCE, PLAYBOOK, reader_post_cards
from tests.test_bot import _FakeClient, _FakeMessages, _ToolMessage, _audit, _cfg

INTERNAL_NOTES = {"dashboard-reads", "ghost-stream", "newer-model-token-limits", "writer-checker-split",
                  "accept-style-notes", "optional-step-blocked-live", "findings-only-fact-check",
                  "retire-failing-topics", "owner-edits-untouched", "repair-not-reject"}


def _v2_cfg():
    cfg = _cfg()
    cfg.post_prompt_version = "v2"
    return cfg


def _judge(text, **overrides):
    value = {
        "action": "post", "text": text, "evidence_supported": True, "reader_would_follow": True,
        "hook_first_line": True, "specific": True, "about_the_reader": True, "clear_on_first_read": True,
        "natural_voice": True, "generic_paste_test": False, "reason": "a move the reader can make today",
    }
    value.update(overrides)
    return value


def _recording(outputs):
    seen = []

    class Recording(_FakeMessages):
        def create(self, **kwargs):
            seen.append(kwargs)
            name, value = self.outputs.pop(0)
            return _ToolMessage(name, value)

    client = _FakeClient([])
    client.messages = Recording(outputs)
    return client, seen


def _system_text(kwargs):
    system = kwargs["system"]
    return system if isinstance(system, str) else "".join(block.get("text", "") for block in system)


POST = ("your next blog topic is already in search console.\n"
        "searches you show up for, with no page that really answers them.\n"
        "write that page.")


def test_v2_cards_are_for_the_reader_and_leave_internal_engineering_out():
    cards = reader_post_cards()
    ids = [card["id"] for card in cards]
    assert len(ids) == len(set(ids))
    assert not INTERNAL_NOTES & set(ids)
    assert {card["id"] for card in PLAYBOOK} <= set(ids)
    first_twelve = cards[:12]
    assert sum(card["kind"] == "build_receipt" for card in first_twelve) <= 3, "build notes stay a minority"
    for card in PLAYBOOK:
        assert card["facts"] and card["angle"], card["id"]


def test_v2_writer_serves_a_stranger_and_v1_is_unchanged():
    v2 = " ".join(_post_writer_system_v2().split())
    for rule in ("never heard of Pentra", "the first line is the hook", "never a changelog",
                 "State nothing beyond them"):
        assert rule in v2, rule
    assert "Every post is about Pentra itself" in _post_writer_system()
    judge = " ".join(_post_judge_system_v2().split())
    assert "release notes" in judge and "generic SEO tips" in judge


def test_v2_rotation_draws_from_reader_cards_and_skips_what_was_just_posted():
    pool_ids = {card["id"] for card in reader_post_cards()}
    picked = {_select_evidence_card(None, None, evidence_rotation_index=i, v2=True)["id"] for i in range(40)}
    assert picked == pool_ids
    assert _select_evidence_card(None, None, evidence_rotation_index=0)["id"] in {
        card["id"] for card in CONTENT_EVIDENCE}, "v1 keeps its own pool"
    assert _select_evidence_card(None, "page-two-pages", v2=True)["id"] == "page-two-pages"


def test_v2_post_keeps_its_line_breaks_and_goes_through_the_v2_editor():
    draft = {"candidate_a": POST, "candidate_b": "search console knows your next topic.",
             "candidate_c": "stop asking a keyword tool what to write.", "claims_used": ["queries"]}
    client, seen = _recording([("draft_evidence_posts", draft), ("edit_reader_post", _judge(POST)),
                               ("audit_public_claims", _audit())])
    trace = {}
    text = standalone_post(_v2_cfg(), client, evidence_id="queries-you-already-show-for", trace=trace)
    assert text == POST and "\n" in text
    assert "never heard of Pentra" in " ".join(_system_text(seen[0]).split())
    assert seen[1]["tool_choice"]["name"] == "edit_reader_post"
    assert trace["evidence_id"] == "queries-you-already-show-for"


def test_v2_editor_rejects_a_post_the_reader_would_not_follow_for():
    draft = {"candidate_a": POST, "candidate_b": "", "candidate_c": "", "claims_used": []}
    client, _ = _recording([("draft_evidence_posts", draft),
                            ("edit_reader_post", _judge(POST, reader_would_follow=False))])
    trace = {}
    assert standalone_post(_v2_cfg(), client, evidence_id="queries-you-already-show-for", trace=trace) == ""


def test_v2_copy_guard_lets_plain_opinion_words_through_but_not_invented_numbers():
    from bot.generate import _post_candidate_set
    card = next(card for card in PLAYBOOK if card["id"] == "small-sites-win-specific")
    draft = {"candidate_a": "small sites can only win on specific searches.\nthat's the whole game.",
             "candidate_b": "a site 40 days old won't outrank the giants.", "candidate_c": "", "claims_used": []}
    _, issues, valid = _post_candidate_set(draft, card, v2=True)
    assert "a" in valid, issues
    assert issues["b"].startswith("unsupported number"), issues


def test_review_first_sends_drafts_even_when_api_posting_is_authorized(monkeypatch):
    from bot import posts_daemon

    class FakeDaemon:
        def __init__(self):
            cfg = _cfg()
            cfg.publishing.original_posts_authorized = True
            cfg.publishing.original_post_drafts = True
            cfg.publishing.original_posts_review_first = True
            cfg.env = {"X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "s"}
            self.cfg = cfg
            self.post_drafts = False

    monkeypatch.setattr(posts_daemon, "Daemon", FakeDaemon)
    daemon = posts_daemon.build_daemon()
    assert daemon.post_drafts is True and daemon.cfg.publishing.enabled is False


def test_live_config_runs_v2_posts_as_drafts_for_review_with_their_own_budget():
    from bot.config import load
    live = json.loads(Path(__file__).resolve().parent.parent.joinpath("config.json").read_text())
    assert live["post_prompt_version"] in ("v2", "v3", "v4"), "v3 and v4 build on the v2 config"
    # Oct 21 2026, Arshad: "automate posts". Originals publish through the API.
    assert live["publishing"]["original_posts_review_first"] is False
    assert live["publishing"]["original_posts_authorized"] is True, "the owner's authorization is untouched"
    assert live["pacing"]["standalone_posts_per_day"] == 2
    gen = live["generation"]
    assert gen["daily_microusd_cap"] - gen["reply_daily_reserve_microusd"] >= 300_000
    assert gen["monthly_microusd_cap"] - gen["reply_monthly_reserve_microusd"] >= 3_000_000
    load()  # the lane caps still fit the owner's $35 ceiling


def test_v2_writer_examples_pass_the_v2_copy_guard():
    """An example that breaks the guard teaches the writer to break it (the
    first draft of this prompt used "isn't x. it's y" twice)."""
    import re
    from bot.generate import _post_context_issue, _public_text_issue
    examples = re.findall(r'post: "(.*)"', _post_writer_system_v2())
    assert len(examples) == 3
    for example in examples:
        text = example.replace("\\n", "\n").replace('\\"', '"')
        assert _public_text_issue(text, allow_first_person=True, allow_product_name=True,
                                  relaxed_vocabulary=True) in (None, "unsupported number: 24"), text
        assert _post_context_issue(text) is None, text


def test_sample_posts_feeds_earlier_samples_as_recent_and_stops_at_the_budget():
    from types import SimpleNamespace
    from bot.llm_budget import GenerationBudgetOpen
    from scripts.sample_posts import sample

    calls = []

    def fake_post(cfg, client, recent_posts=None, evidence_id=None, trace=None, **_):
        calls.append((evidence_id, list(recent_posts or [])))
        if evidence_id == "c3":
            raise GenerationBudgetOpen("daily original-post generation lane exhausted")
        trace["draft"] = {"candidate_a": f"draft for {evidence_id}"}
        return f"post for {evidence_id}" if evidence_id != "c2" else ""

    rows = sample(SimpleNamespace(), None, ["c1", "c2", "c3", "c4"], post=fake_post)
    assert [r["card"] for r in rows] == ["c1", "c2", "c3"]
    assert rows[0]["text"] == "post for c1" and rows[1]["text"] == "" and rows[2]["stopped"]
    assert calls[1] == ("c2", ["post for c1"])


def test_v2_hooks_lead_with_the_payoff_and_example_searches_stay_illustrations():
    """Oct 20 samples: one post opened with a bare instruction ("open search
    console, performance report..."), another stated "has almost no
    competition" about a real search nobody checked."""
    writer = " ".join(_post_writer_system_v2().split())
    judge = " ".join(_post_judge_system_v2().split())
    assert "lead with the payoff or the pain" in writer
    assert "an example search is an illustration" in writer
    assert "a bare instruction" in judge and "competition, volume or ranking" in judge
    card = next(card for card in PLAYBOOK if card["id"] == "small-sites-win-specific")
    assert "is the kind a small site can win" in card["angle"]
