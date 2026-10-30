"""Warm leads from Grok Bot (Oct 20 2026).

Grok posts each lead (people on X or Indie Hackers asking for SEO help) as one
JSON message to an unguessable ntfy.sh topic. Every 10 minutes the server
reads new messages, drafts a reply in our voice (and a DM for fit-5 leads), and
sends each to Telegram for Arshad to send himself. Grok includes the post text,
so no X read is bought.
"""
import json

from bot.leads import draft_lead, ntfy_messages, parse_lead, process_leads, tweet_id_from_url
from tests.test_bot import _FakeClient, _FakeMessages, _cfg
from tests.test_telegram_approval import run, state

LEAD = {
    "id": "x-2112400000000000000",
    "source": "x",
    "url": "https://x.com/jane_builds/status/2112400000000000000",
    "handle": "jane_builds",
    "posted_at": "2026-10-21T13:02:00Z",
    "summary": "solo founder, blog gets no google traffic after 6 months",
    "fit": 5,
    "suggested_reply": "check indexing first",
    "post_text": "6 months of blogging for my saas and google sends me nothing. what am i doing wrong?",
    "top_replies": [{"handle": "someone", "text": "write more"}],
}


def test_a_lead_is_parsed_and_checked():
    lead, reason = parse_lead(json.dumps(LEAD))
    assert reason == "" and lead["handle"] == "jane_builds" and lead["fit"] == 5
    assert parse_lead("not json")[0] is None
    bad = dict(LEAD, url="")
    assert parse_lead(json.dumps(bad))[1] == "no url"
    assert parse_lead(json.dumps(dict(LEAD, fit=9)))[0]["fit"] == 5, "fit is clamped to 1-5"


def test_tweet_ids_come_from_x_status_urls_only():
    assert tweet_id_from_url(LEAD["url"]) == "2112400000000000000"
    assert tweet_id_from_url("https://twitter.com/a/status/123?s=20") == "123"
    assert tweet_id_from_url("https://www.indiehackers.com/post/seo-help-abc") == ""


def test_ntfy_poll_body_yields_only_messages():
    body = "\n".join([
        json.dumps({"id": "a1", "event": "open"}),
        json.dumps({"id": "m1", "event": "message", "message": json.dumps(LEAD)}),
        json.dumps({"id": "k1", "event": "keepalive"}),
        "",
    ])
    assert ntfy_messages(body) == [{"id": "m1", "message": json.dumps(LEAD)}]


def _writer(reply, dm=""):
    return ("draft_lead_reply", {"reply": reply, "dm": dm, "skip": False, "reason": "answers the question"})


def test_a_fit_5_lead_gets_a_reply_and_a_dm():
    reply = "check search console's page indexing report before writing more. six months of posts google didn't index would look exactly like this."
    dm = "saw your post about six months of blogging and no google traffic. happy to take a quick look at your site and tell you what i'd check first."
    drafts = draft_lead(_cfg(), _FakeClient([_writer(reply, dm)]), LEAD)
    assert drafts["reply"] == reply and drafts["dm"] == dm and drafts["problems"] == []


def test_a_draft_with_a_link_or_an_invented_number_is_flagged_not_sent_as_written():
    lead = dict(LEAD, fit=3)
    drafts = draft_lead(_cfg(), _FakeClient([_writer("see https://pentra.dev for a fix")]), lead)
    assert drafts["reply"] == "" and "link" in drafts["problems"][0]
    drafts = draft_lead(_cfg(), _FakeClient([_writer("90% of new blogs don't get indexed in their first month.")]), lead)
    assert drafts["reply"] == "" and "unsupported number" in drafts["problems"][0]


class _Telegram:
    def __init__(self):
        self.sent = []

    async def send_lead_draft(self, **kwargs):
        self.sent.append(kwargs)
        return {"message_id": len(self.sent)}


def test_new_leads_are_drafted_once_and_sent_highest_fit_first():
    s = state()
    low = dict(LEAD, id="x-1", url="https://x.com/a/status/1", handle="a", fit=2)
    high = dict(LEAD, id="x-2", url="https://x.com/b/status/2", handle="b", fit=5)
    messages = [{"id": "m1", "message": json.dumps(low)}, {"id": "m2", "message": json.dumps(high)},
                {"id": "m3", "message": json.dumps(high)}, {"id": "m4", "message": "garbage"}]
    telegram = _Telegram()
    drafted = []

    def fake_draft(cfg, client, lead, avoid=None):
        drafted.append(lead["handle"])
        return {"reply": f"reply for {lead['handle']}", "dm": "a dm" if lead["fit"] == 5 else "", "problems": []}

    result = run(process_leads(_cfg(), s, None, telegram, messages, draft=fake_draft))
    assert drafted == ["b", "a"]
    assert [m["lead"]["handle"] for m in telegram.sent] == ["b", "a"]
    assert telegram.sent[0]["dm"] == "a dm" and telegram.sent[1]["dm"] == ""
    assert result == {"new": 2, "duplicates": 1, "invalid": 1, "sent": 2}
    again = run(process_leads(_cfg(), s, None, telegram, messages, draft=fake_draft))
    assert again["new"] == 0 and len(telegram.sent) == 2


def test_lead_message_has_the_x_handoff_and_copy_buttons(monkeypatch):
    from bot.telegram_approval import TelegramApprovalClient
    from tests.test_telegram_approval import cfg as approval_cfg

    client = TelegramApprovalClient(approval_cfg())
    posted = {}

    async def fake_post(method, payload, **_):
        posted.update(payload)
        return {"ok": True, "result": {"message_id": 5}}

    monkeypatch.setattr(client, "_post", fake_post)
    run(client.send_lead_draft(lead=LEAD, reply="a short reply", dm="a short dm", problems=[]))
    assert "fit 5/5" in posted["text"] and "@jane_builds" in posted["text"] and "a short dm" in posted["text"]
    buttons = [b for row in posted["reply_markup"]["inline_keyboard"] for b in row]
    labels = [b["text"] for b in buttons]
    assert labels[0] == "Open reply in X app" and "pentra.dev/x-reply" in buttons[0]["url"]
    assert "Copy DM" in labels and "Open their post" in labels


def test_style_only_rules_do_not_withhold_a_helpful_lead_reply():
    """Oct 20 end-to-end test: a good answer ("...the problem is crawling or
    quality, not keywords") was withheld for a canned contrast, a rule made for
    short reply-guy posts. Safety rules still hold (see the link test above)."""
    reply = ("first check whether google indexed your pages: paste a post url into search console's url "
             "inspection. if it says not indexed, the problem is crawling or quality, not keywords.")
    drafts = draft_lead(_cfg(), _FakeClient([_writer(reply)]), dict(LEAD, fit=3))
    assert drafts["reply"] == reply and drafts["problems"] == []
    usually = "it's usually indexing. paste a post url into search console's url inspection first."
    assert draft_lead(_cfg(), _FakeClient([_writer(usually)]), dict(LEAD, fit=3))["reply"] == usually


def test_a_lead_reply_may_run_past_280_characters_but_not_past_400():
    """@pentradev has Premium, and a direct answer to someone's question can
    need a little more room (Oct 20: a good answer was withheld at 300)."""
    longer = ("first check whether google indexed your pages at all: paste a post url into search console's url "
              "inspection tool. if it says crawled or discovered but not indexed, the fix is quality and internal "
              "links, so link the post from your homepage and strongest pages before writing anything new.")
    assert 280 < len(longer) <= 400
    assert draft_lead(_cfg(), _FakeClient([_writer(longer)]), dict(LEAD, fit=3))["reply"] == longer
    too_long = longer + " " + longer
    assert draft_lead(_cfg(), _FakeClient([_writer(too_long)]), dict(LEAD, fit=3))["reply"] == ""


def test_each_sent_lead_keeps_its_drafts_for_review():
    """Drafts only lived in Telegram; the X window could not check the quality
    of a lead batch (Oct 20 2026)."""
    s = state()
    telegram = _Telegram()

    def fake_draft(cfg, client, lead, avoid=None):
        return {"reply": "check url inspection first", "dm": "happy to look at your site", "problems": []}

    run(process_leads(_cfg(), s, None, telegram, [{"id": "m1", "message": json.dumps(LEAD)}], draft=fake_draft))
    stored = json.loads(s.db.execute("SELECT payload FROM leads").fetchone()[0])
    assert stored["draft_reply"] == "check url inspection first" and stored["draft_dm"] == "happy to look at your site"


def test_lead_batch_feedback_oct_20():
    """First real batch (15 leads): 6 of 13 replies gave the same Search Console
    tip, some skipped the question asked, two fit-5 drafts were withheld over
    advice numbers ("positions 4 to 20"), and a DM credited Pentra with a
    feature the site doesn't claim."""
    from bot.leads import _lead_writer_system

    system = " ".join(_lead_writer_system().split())
    assert "answer the question they actually asked" in system
    assert "no two replies in a batch give the same advice" in system
    assert "only these facts about" in system

    seen = []

    class Recording(_FakeMessages):
        def create(self, **kwargs):
            seen.append(kwargs)
            return super().create(**kwargs)

    client = _FakeClient([])
    client.messages = Recording([_writer("open search console and look at pages at positions 4 to 20 first.")])
    drafts = draft_lead(_cfg(), client, dict(LEAD, fit=3), avoid=["check url inspection first"])
    assert drafts["reply"].startswith("open search console"), drafts
    assert "check url inspection first" in seen[0]["messages"][0]["content"]


def test_a_batch_tells_each_draft_what_earlier_ones_already_said():
    s = state()
    telegram = _Telegram()
    avoided = []

    def fake_draft(cfg, client, lead, avoid=None):
        avoided.append(list(avoid or []))
        return {"reply": f"reply for {lead['handle']}", "dm": "", "problems": []}

    leads = [dict(LEAD, id=f"x-{i}", url=f"https://x.com/u{i}/status/{i}", handle=f"u{i}", fit=4) for i in range(3)]
    run(process_leads(_cfg(), s, None, telegram, [{"id": f"m{i}", "message": json.dumps(l)} for i, l in enumerate(leads)],
                      draft=fake_draft))
    assert avoided == [[], ["reply for u0"], ["reply for u0", "reply for u1"]]


def test_grok_digests_are_kept_once_and_the_latest_whole_one_reaches_the_summary():
    """Oct 21 2026: Grok's daily directory digest lived only in the 12-hour
    ntfy cache, so the morning summary couldn't show directories."""
    from bot.leads import store_grok_digests
    from scripts.daily_summary import directories_line

    s = state()
    digest = {"type": "directory_digest", "date": "2026-10-21",
              "live": [{"directory": "SaaSHub", "listing_url": "https://www.saashub.com/pentra"}],
              "submitted": [{"directory": "Uneed"}, {"directory": "FutureTools"}],
              "rejected": [{"directory": "DevHunt", "reason": "social login only"}]}
    sources = {"type": "directory_digest", "date": "2026-10-21", "part": 2, "ai_sources": ["https://a.example"]}
    messages = [{"id": "t1", "message": json.dumps({"type": "test"})},
                {"id": "d1", "message": json.dumps(digest)},
                {"id": "d2", "message": json.dumps(sources)},
                {"id": "d1", "message": json.dumps(digest)}]
    assert store_grok_digests(s, messages) == {"stored": 2, "duplicates": 1, "invalid": 1}
    assert s.latest_grok_digest()["submitted"][0]["directory"] == "Uneed"
    line = directories_line(s)
    assert line.startswith("Directories (Grok, 2026-10-21): 1 live, 2 submitted, 1 rejected") and "SaaSHub" in line
    assert directories_line(state()) is None
