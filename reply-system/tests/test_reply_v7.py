"""Reply voice v7 (Oct 23 2026). X limited @pentradev on Oct 22 (Sorsa: search
ban; every reply from 13:17 got 3 to 8 views). The v6 drafts had become one
template, "the x is the y." plus a maxim, all lowercase: the shape AI reply
tools write. v7 keeps agree-and-extend and drops the slogan."""
import json
import re

from bot import generate
from bot.generate import _OPENER_SHAPE_RE, _SLOGAN_RE, _reply_copy_issue, _reply_review_system_v3, decide
from tests.test_bot import _V3_SOURCE, _call, _cfg, _recording_client, _reply_audit, _review_reply

# Oct 22 drafts that read like an AI reply tool (state.db reply_drafts).
OCT_22_SLOGANS = [
    "a basecamp plugin inside chatgpt means the to-do list shows up where people already ask their questions. "
    "the plugin is the new front door.",
    "the duo screen is a new room, and most apps are still standing in the hallway. the first agency to measure "
    "the cramped app wins the pitch.",
    "the flashlight is the feature that proves the ui library is real. nobody ships a flashlight for a demo.",
    "the apple store is now the newsletter distribution channel nobody budgeted for. the specialist knows what "
    "people are reading.",
    "the side project is often where a team finds what it should have been building. a founder who lets it grow "
    "is reading the roadmap early.",
    "a hackathon where the prize is two 1:1s is a pitch meeting with a trophy. the best cold email is the one you "
    "get to write in person.",
    "banks blocking agents is the first draft of the rules. the agents that survive will be the ones that can "
    "prove who sent them.",
    "the \"i'm coming of age\" line is doing more work than the video. you gave the model a persona and it went "
    "straight for an existential crisis.",
]

OCT_22_OPENINGS = [
    "no face is a nice trick until the agent has to explain itself.",
    "the chat is easy to ship.",
    "the permissions screen is where this gets real.",
    "the agent point is the sharpest one.",
    "patching was the slow half.",
    "a hackathon where the prize is two 1:1s is a pitch meeting with a trophy.",
    "the captcha was a test of who got bored of the puzzle first.",
    "the \"i'm coming of age\" line is doing more work than the video.",
    "the line that stuck for me is \"what if everything goes right?\"",
    "the checking loop is the clever part.",
    "banks blocking agents is the first draft of the rules.",
    "saying no to someone's work is easier when nobody is protecting feelings.",
    "the duo screen is a new room.",
    "the flashlight is the feature that proves the ui library is real.",
]


def _issue(text, source=None, v7=True, recent=None):
    return _reply_copy_issue(text, source_text=text if source is None else source, voice_v2=True, voice_v3=True,
                             v4=True, v5=True, v6=True, v7=v7, recent_replies=recent or [])


def _v7_cfg():
    cfg = _cfg()
    cfg.reply_prompt_version = "v7"
    return cfg


def test_v7_catches_every_oct_22_slogan_that_v6_sent():
    for text in OCT_22_SLOGANS:
        assert _SLOGAN_RE.search(text), text
        if len(text) <= generate._V6_MAX_CHARS:
            assert _issue(text) == "slogan line", text
    assert _issue(OCT_22_SLOGANS[2], v7=False) is None, "v6 is unchanged"


def test_v7_lets_our_winners_and_the_plain_versions_through():
    winners = re.findall(r'post: "(.*)"\nreply: "(.*)"', generate._REPLY_EXAMPLES_V7)[:2]
    plains = re.findall(r'post: "(.*)"\nslogan: "(.*)"\nplain: "(.*)"', generate._REPLY_EXAMPLES_V7)
    assert len(winners) == 2 and len(plains) == 3
    for post, reply in winners:
        assert _issue(reply, post) is None, reply
    for post, slogan, plain in plains:
        assert _SLOGAN_RE.search(slogan), slogan
        assert _issue(plain, post) is None, plain
    for text in ("Putting the to-do list where people already ask questions makes sense.",
                 "We hit the same thing when our bot moved to a newer model.",
                 "Is the new pricing per seat or per workspace?"):
        assert not _SLOGAN_RE.search(text), text


def test_v7_varies_the_opening_after_ten_of_fourteen_drafts_opened_alike():
    assert sum(bool(_OPENER_SHAPE_RE.match(t)) for t in OCT_22_OPENINGS) == 10
    reply, source = "the chat is easy to ship.", "the chat"
    assert _issue(reply, source, recent=["the flashlight is a fun touch in the demo."]) == "repeated opening"
    assert _issue(reply, source, recent=["the flashlight is a fun touch.", "Love the demo.", "We did that too."]) is None
    assert _issue(reply, source, v7=False, recent=["the flashlight is a fun touch."]) is None


def test_v7_prompt_drops_the_quotable_line_and_asks_for_plain_words():
    v7 = " ".join(_reply_review_system_v3(v4=True, v5=True, v6=True, v7=True).split())
    v6 = " ".join(_reply_review_system_v3(v4=True, v5=True, v6=True).split())
    assert "quotable" not in v7.replace("built to be quoted", "") and "screenshot" not in v7
    assert "normal capitalization" in v7 and "What X penalizes" in v7 and "2,044 views" in v7
    assert "a warm, quotable line" in v6, "v6 is unchanged"


def test_a_v7_slogan_gets_one_plain_rewrite():
    slogan = "unrepresentable bad code is the new code review."
    candidate = _review_reply(text=slogan, source_quote="make bad code unrepresentable",
                              contribution_type="compression", information_delta="types replace a review step")
    plain = {"text": "Agreed. If the type can't hold the bad state, the agent can't write that bug.",
             "source_quote": "make bad code unrepresentable", "information_delta": "same point", "reason": "plain"}
    client, seen = _recording_client([("compose_required_review_reply", candidate),
                                      ("repair_grounded_question", plain), ("verify_reply", _reply_audit())])
    result = decide(_v7_cfg(), _V3_SOURCE, "Builder", client, human_review=True)
    assert result["action"] == "reply" and result["text"] == plain["text"], result
    repair = _call(seen, "repair_grounded_question")
    assert "normal capitalization" in repair["system"] and "sharp builder typing one line" not in repair["system"]
    veto = json.loads(repair["messages"][0]["content"])["audit_veto"]
    assert "slogan" in veto and "is the new code" in veto, veto
    writer = _call(seen, "compose_required_review_reply")["system"]
    writer = writer if isinstance(writer, str) else " ".join(block.get("text", "") for block in writer)
    assert "What X penalizes" in writer


def test_a_reply_over_140_characters_is_told_its_character_count():
    """Oct 23 replay: under 28 words but over 140 characters, the rewrite was
    told "25 words, over the 28-word limit", and 5 of 20 v7 drafts were lost."""
    long_reply = ("Agents building Notion configs that nobody on the whole team can follow is a real risk for any "
                  "company that lets them run without any review.")
    assert len(long_reply.split()) <= generate._V4_MAX_WORDS and len(long_reply) > generate._V6_MAX_CHARS
    reason = generate._repair_reason("too wordy", "copy guard", text=long_reply)
    assert f"has {len(long_reply)} characters" in reason and "under 110 characters" in reason


def test_v7_also_varies_acknowledgement_openers_and_catches_the_replays_audit_frames():
    recent = ["Agree on the gen-lock. A shared trigger pulse keeps the phases together."]
    assert _issue("Fair point on the CAPTCHA. Bots can build up history now.", recent=recent) == "repeated opening"
    assert "'agree' or 'fair point'" in generate._repair_reason("repeated opening", "copy guard", text="Fair point.")
    for text in ("The harder part now is telling a real account from a bot.",
                 "Auto-invite and auto-remove is the part I'd watch.",
                 "The permissions list is the part to get right first."):
        assert _issue(text) == "skeptic frame", text


def test_v7_rewrites_are_not_steered_into_audit_frames():
    """Oct 23 replay: the rewrite prompt offered "the catch" and "the hard part
    is" as the reply's view, and v7 rewrites came back as skeptic frames."""
    from bot.generate import _repair_grounded_question
    client, seen = _recording_client([("repair_grounded_question", {"text": "Agreed.", "source_quote": "make bad code"})])
    _repair_grounded_question(_v7_cfg(), client, source_text=_V3_SOURCE, question="x", audit_reason="slogan line",
                              max_words=22, prefer_statement=True, require_statement=True, plain=True)
    system = _call(seen, "repair_grounded_question")["system"]
    assert "a take, the catch, the missing step" not in system and "'the hard part is'" not in system
    for text in ("Banks not telling an agent from a person is the scariest part.",
                 "The auto-remove part is what would make me nervous as a creator."):
        assert _issue(text) == "skeptic frame", text


def test_a_v7_rewrite_that_keeps_a_repeated_opening_gets_a_second_try():
    """Oct 23 replay: the first rewrite fixed the length but kept "Agree on...",
    and the draft was dropped."""
    long_text = ("Agree on making bad code unrepresentable. If the type can't hold the bad state, the agent can't "
                 "write that bug, and every review step gets much shorter.")
    assert len(long_text.split()) <= generate._V4_MAX_WORDS and len(long_text) > generate._V6_MAX_CHARS
    candidate = _review_reply(text=long_text, source_quote="make bad code unrepresentable",
                              contribution_type="implication", information_delta="reviews get shorter")
    first = {"text": "Agree on that. If the type can't hold the bad state, the agent can't write that bug.",
             "source_quote": "make bad code unrepresentable", "information_delta": "same", "reason": "cut"}
    second = {"text": "If the type can't hold the bad state, the agent can't write that bug in the first place.",
              "source_quote": "make bad code unrepresentable", "information_delta": "same", "reason": "new opening"}
    client, seen = _recording_client([("compose_required_review_reply", candidate), ("repair_grounded_question", first),
                                      ("repair_grounded_question", second), ("verify_reply", _reply_audit())])
    result = decide(_v7_cfg(), _V3_SOURCE, "Builder", client, human_review=True,
                    recent_replies=["Agreed, the flat structure helps here."])
    assert result["action"] == "reply" and result["text"] == second["text"], result
    repairs = [k for k in seen if k.get("tool_choice", {}).get("name") == "repair_grounded_question"]
    assert "'agree' or 'fair point'" in json.loads(repairs[1]["messages"][0]["content"])["audit_veto"]


def test_v7_frames_from_the_oct_23_replay_cannot_repeat():
    from bot.generate import template_frame

    assert template_frame("Letting customers pick is the part I'd build first.", v5=True) == "the x i'd y"
    assert template_frame("Dave's point is the one I'd underline.", v5=True) == "the x i'd y"
    assert template_frame("The zoo fence is the funny part.", v5=True) == "is the funny part"
    assert _issue("The grid is the one I'd worry about.") == "skeptic frame"


def test_v7_writer_goes_straight_to_the_point_in_one_sentence():
    """Oct 23 replay traces: first drafts ran 150-220 characters and opened by
    naming the part the writer liked ("the fax is the best part"); the reviewer
    dropped 5 of 40 for restating the post."""
    voice = " ".join(generate._REPLY_VOICE_V7.split())
    assert "one sentence of 60 to 110 characters" in voice
    assert "go straight to your point" in voice and "the $5B number is the part that stuck with me" in voice


def test_v7_rewrites_hold_every_rule_at_once():
    from bot.generate import _repair_grounded_question

    client, seen = _recording_client([("repair_grounded_question", {"text": "Agreed.", "source_quote": "make bad code"})])
    _repair_grounded_question(_v7_cfg(), client, source_text=_V3_SOURCE, question="x", audit_reason="too wordy",
                              max_words=22, prefer_statement=True, require_statement=True, plain=True)
    system = " ".join(_call(seen, "repair_grounded_question")["system"].split())
    assert "never most, usually, typically, generally, always or never" in system and "under 110 characters" in system


def test_a_v7_second_fix_keeps_the_first():
    """Oct 23 replay: a prevalence fix came back too long, and the length fix
    brought "most" back (joshm, realEstateTrent)."""
    candidate = _review_reply(text="Agreed, most abstractions help when the type can't hold the bad state.",
                              source_quote="make bad code unrepresentable", contribution_type="mechanism",
                              information_delta="where the idea works")
    long_fix = {"text": ("Agreed, abstractions help when the type can't hold the bad state, which is the whole point of "
                         "making bad code unrepresentable for the agents."),
                "source_quote": "make bad code unrepresentable", "information_delta": "same", "reason": "removed most"}
    short_fix = {"text": "Abstractions help when the type can't hold the bad state.",
                 "source_quote": "make bad code unrepresentable", "information_delta": "same", "reason": "cut"}
    assert len(long_fix["text"]) > generate._V6_MAX_CHARS
    client, seen = _recording_client([("compose_required_review_reply", candidate), ("repair_grounded_question", long_fix),
                                      ("repair_grounded_question", short_fix), ("verify_reply", _reply_audit())])
    result = decide(_v7_cfg(), _V3_SOURCE, "Builder", client, human_review=True)
    assert result["action"] == "reply" and result["text"] == short_fix["text"], result
    repairs = [k for k in seen if k.get("tool_choice", {}).get("name") == "repair_grounded_question"]
    second = json.loads(repairs[1]["messages"][0]["content"])["audit_veto"]
    assert "also keep the earlier fix" in second and "'most'" in second, second


def test_a_rejected_live_draft_is_logged_with_its_rewrites():
    """Oct 23 2026: production kept no text of rejected drafts, so the live v7
    losses (two "unsupported prevalence" by 14:29) could not be read."""
    from bot.daemon import _rejected_draft_line

    trace = {"single_pass": {"text": "Most apps stretch to fill the screen."},
             "bounded_copy_repair": {"after": "Many apps stretch.", "shortened": {"after": "Apps stretch to fill it."}},
             "veto_rewrite": {"retry": {"single_pass": {"text": "Recipes fit big screens."}}}}
    line = _rejected_draft_line("gregisenberg", {"reason": "deterministic copy guard: unsupported prevalence"}, trace, False)
    assert line.startswith("REJECTED DRAFT @gregisenberg [deterministic copy guard: unsupported prevalence]")
    assert "'Most apps stretch to fill the screen.' -> 'Apps stretch to fill it.'" in line
    assert "after veto: 'Recipes fit big screens.'" in line
    assert _rejected_draft_line("a", {"reason": "Political post, outside the lane."}, trace, False) is None
    assert _rejected_draft_line("a", {"reason": "x"}, trace, True) == "DRY CANDIDATE @a: 'Most apps stretch to fill the screen.'"
    assert _rejected_draft_line("a", {"reason": "deterministic copy guard: x"}, {}, False) is None


def test_a_v7_rewrite_just_over_the_limit_keeps_its_first_sentence():
    """Oct 23 2026, live: a rewrite of 143 characters (cap 140) was dropped
    although its first sentence stood on its own."""
    long_text = ("Agreed, the type should carry the rule. If the type can't hold the bad state, the agent can't write "
                 "that bug, and reviews of agent code get much shorter for everyone.")
    two = ("If the type can't hold the bad state, the agent can't write that bug at all. Reviews of agent-written code "
           "then get shorter and calmer for the team.")
    assert len(two) > generate._V6_MAX_CHARS
    candidate = _review_reply(text=long_text, source_quote="make bad code unrepresentable",
                              contribution_type="implication", information_delta="reviews get shorter")
    rewrite = {"text": two, "source_quote": "make bad code unrepresentable", "information_delta": "same", "reason": "cut"}
    client, _ = _recording_client([("compose_required_review_reply", candidate), ("repair_grounded_question", rewrite),
                                   ("repair_grounded_question", rewrite), ("verify_reply", _reply_audit())])
    result = decide(_v7_cfg(), _V3_SOURCE, "Builder", client, human_review=True)
    assert result["action"] == "reply", result
    assert result["text"] == "If the type can't hold the bad state, the agent can't write that bug at all."
    assert generate._first_sentence("Agreed.") == "" and generate._first_sentence("One. Two.") == "One."


def test_a_v7_contrast_fix_that_repeats_a_frame_gets_a_second_try():
    """Oct 23 2026, live (@ChrisJBakke): the contrast fix came back as "...is the
    part I'd want to hear more about", which repeated a recent frame, and the
    copy-repair branch had no second try."""
    contrast = "Making bad code unrepresentable is about the type, not just the tests."
    recent = ["Letting agents read each other's sessions directly is the part I'd pay for."]
    assert _issue(contrast, _V3_SOURCE, recent=recent) == "canned contrast"
    candidate = _review_reply(text=contrast, source_quote="make bad code unrepresentable",
                              contribution_type="reframe", information_delta="the type carries the rule")
    framed = {"text": "Making bad code unrepresentable is the part I'd bet on for agents.",
              "claim_preserved": True, "reason": "dropped the contrast"}
    clean = {"text": "Putting the rule in the type means an agent can't skip it.",
             "source_quote": "make bad code unrepresentable", "information_delta": "same", "reason": "new frame"}
    client, seen = _recording_client([("compose_required_review_reply", candidate), ("repair_public_text", framed),
                                      ("repair_grounded_question", clean), ("verify_reply", _reply_audit())])
    result = decide(_v7_cfg(), _V3_SOURCE, "Builder", client, human_review=True, recent_replies=recent)
    assert result["action"] == "reply" and result["text"] == clean["text"], result
    veto = json.loads(_call(seen, "repair_grounded_question")["messages"][0]["content"])["audit_veto"]
    assert "a recent reply already used this sentence frame" in veto and "also keep the earlier fix" in veto


def test_v7_uses_a_build_note_when_the_post_is_about_the_same_problem():
    """Vision checklist A10 (Oct 24 2026): 0 of 101 drafts used a build note,
    because the v4 rule said most replies use none."""
    v7 = " ".join(_reply_review_system_v3(v4=True, v5=True, v6=True, v7=True).split())
    v6 = " ".join(_reply_review_system_v3(v4=True, v5=True, v6=True).split())
    assert "it is the detail only this account can add" in v7 and "Most replies use none of them" not in v7
    assert "Most replies use none of them" in v6, "v6 is unchanged"
