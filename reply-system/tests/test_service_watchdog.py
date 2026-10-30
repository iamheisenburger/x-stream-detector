"""The outside watchdog: Oct 5-14 2026 nothing told the owner that Convex was
disabled, pentra.dev articles returned 500 and publishing had stopped."""
import sqlite3
import time

from scripts.service_watchdog import convex_failure, credit_failure, parse_sitemap, plan_alerts, site_failures

NOW = 1_792_000_000.0
SITE = {"domain": "leadpilot.chat", "sitemap": "https://leadpilot.chat/sitemap.xml", "max_publish_gap_hours": 12}


def sitemap(*rows):
    return "<urlset>" + "".join(f"<url><loc>{loc}</loc><lastmod>{mod}</lastmod></url>" for loc, mod in rows) + "</urlset>"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_healthy_site_has_no_failures():
    xml = sitemap(("https://leadpilot.chat/", iso(NOW - 86400)), ("https://leadpilot.chat/blog/a", iso(NOW - 3600)))
    assert parse_sitemap(xml)[1] == ("https://leadpilot.chat/blog/a", iso(NOW - 3600))
    assert site_failures(SITE, 200, xml, 200, NOW) == []


def test_broken_sitemap_article_and_stalled_publishing_are_each_reported():
    assert [f["key"] for f in site_failures(SITE, 500, "", None, NOW)] == ["leadpilot.chat:sitemap"]
    stale = sitemap(("https://leadpilot.chat/blog/a", iso(NOW - 9 * 86400)))
    keys = [f["key"] for f in site_failures(SITE, 200, stale, 500, NOW)]
    assert keys == ["leadpilot.chat:article", "leadpilot.chat:publishing"]
    assert "216 h" in site_failures(SITE, 200, stale, 200, NOW)[0]["message"]


def test_convex_probe_is_healthy_only_when_the_public_lookup_succeeds():
    assert convex_failure(200, '{"status":"success","value":null}') is None
    # What the HTTP API answered while the deployment was disabled (Oct 14 2026).
    disabled = convex_failure(200, '{"status":"error","errorMessage":"[Request ID: 767da652b618b554] Server Error"}')
    assert disabled["key"] == "convex" and "disabled" in disabled["message"]
    assert "free plan limits" in convex_failure(400, '{"status":"error","errorMessage":"You have exceeded the free plan limits"}')["message"]
    assert convex_failure(None, "")["key"] == "convex"


def test_out_of_credit_refusal_is_reported_until_a_metered_call_succeeds(tmp_path):
    db = tmp_path / "state.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE generation_outcomes(at REAL, kind TEXT, outcome TEXT, reason TEXT)")
    c.execute("CREATE TABLE runtime_settings(key TEXT PRIMARY KEY, value TEXT, updated_at REAL)")
    c.execute("CREATE TABLE llm_usage(at REAL)")
    c.execute("INSERT INTO llm_usage VALUES (?)", (NOW - 3600,))
    c.execute("INSERT INTO runtime_settings VALUES ('provider_credit_exhausted_at', ?, ?)", (str(NOW - 600), NOW - 600))
    c.commit()
    assert "console.anthropic.com" in credit_failure(str(db), NOW)["message"]
    assert credit_failure(str(db), NOW + 7 * 3600) is None, "an old refusal is not news"
    # After a top-up the next metered call succeeds: recovered.
    c.execute("INSERT INTO llm_usage VALUES (?)", (NOW - 60,))
    c.commit()
    assert credit_failure(str(db), NOW) is None
    # Older builds recorded the refusal as an outcome reason.
    c.execute("INSERT INTO generation_outcomes VALUES (?,?,?,?)", (NOW - 30, "reply", "generation_error",
              "BadRequestError: Your credit balance is too low to access the Anthropic API."))
    c.commit()
    assert credit_failure(str(db), NOW)["key"] == "anthropic"


def test_alerts_once_then_every_few_hours_then_once_on_recovery():
    failure = [{"key": "convex", "message": "The Pentra backend (Convex) refuses requests."}]
    messages, state = plan_alerts(failure, {}, NOW, 6)
    assert messages == ["PROBLEM: The Pentra backend (Convex) refuses requests."]
    assert plan_alerts(failure, state, NOW + 1800, 6)[0] == []
    again, state = plan_alerts(failure, state, NOW + 6 * 3600, 6)
    assert again[0].startswith("STILL BROKEN (6 h)")
    recovered, state = plan_alerts([], state, NOW + 7 * 3600, 6)
    assert recovered == ["RECOVERED after 7 h: convex"] and state == {}
