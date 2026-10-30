"""Sourced evidence cards and the weekly plan for @pentradev's original posts
(posts v4, Oct 22 2026).

From the X window's research playbook: every post teaches one thing a founder
can act on this week; every number, date and quote comes from a card whose
source is named in the post; Pentra is never the subject of line one. Cards
are phrased tightly so the writer cannot stretch them. Owner data only, never
a customer's.
"""
from __future__ import annotations

from .stances import HOW_TO_CARDS

EVIDENCE_CARDS: dict[str, dict[str, str]] = {
    "pew-ai-summary-clicks": {
        "text": ("Pew Research Center analyzed the Google searches of 900 U.S. adults in March 2025. When an AI "
                 "summary appeared, users clicked a traditional result on 8% of visits; without one, 15%. Users "
                 "clicked a link inside the AI summary on 1% of visits. About one in five searches produced an AI "
                 "summary."),
        "source": "pew research center, july 2025",
        "url": "https://www.pewresearch.org/short-reads/2025/07/22/google-users-are-less-likely-to-click-on-links-when-an-ai-summary-appears-in-the-results/",
    },
    "ahrefs-aio-ctr-58": {
        "text": ("Ahrefs (300,000 keywords, Search Console data, Dec 2023 vs Dec 2025): an AI Overview correlates "
                 "with a 58% lower average click-through rate for the top-ranking page; its April 2025 study had "
                 "found 34.5%."),
        "source": "ahrefs, feb 2026",
        "url": "https://ahrefs.com/blog/ai-overviews-reduce-clicks-update/",
    },
    "ahrefs-aio-triggers": {
        "text": ("Ahrefs (146 million desktop results, Sept 2025): AI Overviews appeared on 20.5% of results; on "
                 "57.9% of question searches vs 15.5% of non-question searches; on 7.9% of local searches; on 46.4% "
                 "of searches of seven or more words vs 9.5% of one-word searches."),
        "source": "ahrefs, nov 2025",
        "url": "https://ahrefs.com/blog/ai-overview-triggers/",
    },
    "ahrefs-aio-top10": {
        "text": ("Ahrefs (1.9 million citations from 1 million AI Overviews): 76.1% of cited pages ranked in Google's "
                 "top 10; 9.5% ranked 11 to 100; 14.4% were outside the top 100."),
        "source": "ahrefs, july 2025",
        "url": "https://ahrefs.com/blog/search-rankings-ai-citations/",
    },
    "ahrefs-schema": {
        "text": ("Ahrefs tracked 1,885 pages that added JSON-LD schema (Aug 2025 to Mar 2026) against matched "
                 "control pages: AI Overview citations fell 4.6%; AI Mode (+2.4%) and ChatGPT (+2.2%) changes were "
                 "statistically indistinguishable from zero."),
        "source": "ahrefs, may 2026",
        "url": "https://ahrefs.com/blog/schema-ai-citations/",
    },
    "ahrefs-ctr-benchmark": {
        "text": ("Ahrefs' monthly benchmark from 422,421 sites' Search Console data: a good whole-site organic CTR "
                 "usually falls between 1% and 2%."),
        "source": "ahrefs, june 2026",
        "url": "https://ahrefs.com/blog/what-is-a-good-ctr/",
    },
    "seer-aio-ctr": {
        "text": ("Seer Interactive (3,119 informational queries, 42 organizations, June 2024 to Sept 2025): organic "
                 "CTR on queries with AI Overviews fell from 1.76% to 0.61%; on queries without them, from 2.72% to "
                 "1.62%. In Q3 2025, brands cited in the AI Overview had 0.70% organic CTR vs 0.52% for uncited ones; "
                 "Seer says citation may not cause the gap."),
        "source": "seer interactive, nov 2025",
        "url": "https://www.seerinteractive.com/insights/aio-impact-on-google-ctr-september-2025-update",
    },
    "backlinko-page-two": {
        "text": ("In Backlinko's CTR study (about 4 million results; page updated April 2025, underlying data older), "
                 "0.63% of Google searchers clicked a result on page two, and the #1 result had a 27.6% CTR."),
        "source": "backlinko",
        "url": "https://backlinko.com/google-ctr-stats",
    },
    "google-genai-report": {
        "text": ("On June 3, 2026 Google launched Generative AI performance reports in Search Console, showing "
                 "impressions in AI Overviews, AI Mode and Discover's generative AI features by page, country, device "
                 "and date. Since Aug 31, 2026 they are available for all websites."),
        "source": "google search central, june 2026",
        "url": "https://developers.google.com/search/blog/2026/06/gen-ai-performance-reports",
    },
    "google-ignore-list": {
        "text": ("Google's guide \"Optimizing your website for generative AI features on Google Search\" (May 2026, "
                 "updated July 10, 2026): Google Search doesn't use llms.txt or other AI text files; there's no "
                 "requirement to \"chunk\" content; there's no special schema markup for generative AI search; you "
                 "don't need to rewrite content for AI."),
        "source": "google's ai optimization guide, july 2026",
        "url": "https://developers.google.com/search/docs/fundamentals/ai-optimization-guide",
    },
    "google-no-special-requirements": {
        "text": ("Google: \"There are no additional requirements to appear in AI Overviews or AI Mode, nor other "
                 "special optimizations necessary.\" A page must be indexed and eligible to show with a snippet."),
        "source": "google search central",
        "url": "https://developers.google.com/search/docs/appearance/ai-features",
    },
    "google-commodity": {
        "text": ("Google's AI optimization guide gives \"7 Tips for First-Time Homebuyers\" as an example of commodity "
                 "content and \"Why We Waived the Inspection & Saved Money: A Look Inside the Sewer Line\" as "
                 "non-commodity content."),
        "source": "google's ai optimization guide",
        "url": "https://developers.google.com/search/docs/fundamentals/ai-optimization-guide",
    },
    "google-query-fanout": {
        "text": ("Google's AI features use \"query fan-out\", related searches the model generates. Google's example "
                 "for \"how to fix a lawn that's full of weeds\" includes \"best herbicides for lawns\", \"remove weeds "
                 "without chemicals\" and \"how to prevent weeds in lawn\"."),
        "source": "google's ai optimization guide",
        "url": "https://developers.google.com/search/docs/fundamentals/ai-optimization-guide",
    },
    "google-ai-content": {
        "text": ("Google: \"using generative AI tools or other similar tools to generate many pages without adding "
                 "value for users may violate Google's spam policy on scaled content abuse.\" Google asks site owners "
                 "to \"factcheck and review all AI-generated content for accuracy and trustworthiness before "
                 "publishing.\""),
        "source": "google search central, oct 2026",
        "url": "https://developers.google.com/search/docs/fundamentals/using-gen-ai-content",
    },
    "lilyray-listicles": {
        "text": ("Lily Ray's own check of about 100 queries: in September 2026 vs June, Google's AI Overviews cited "
                 "38% fewer self-promotional \"best of\" listicles and left out the publisher's brand 83% of the time "
                 "(69% in June)."),
        "source": "lily ray's test, sept 2026",
        "url": "https://x.com/lilyraynyc/status/2105387660726477087",
    },
    "ahrefs-chatgpt-listicles": {
        "text": ("Ahrefs, as summarized by its CMO Tim Soulo (June 2026): \"best X\" listicles made up 43.8% of the "
                 "page types ChatGPT cited."),
        "source": "ahrefs via tim soulo, june 2026",
        "url": "https://x.com/timsoulo/status/2061796432534003866",
    },
    "google-genai-control": {
        "text": ("Search Console has a control at Settings > Search generative AI with Include, Exclude, or Inherit "
                 "from parent. Excluded sites stop receiving traffic and impressions from AI Overviews, AI Mode and "
                 "Discover's generative AI features. Google says the control isn't a ranking signal for other parts "
                 "of Search. Rolled out to all websites as of Aug 31, 2026."),
        "source": "search console help, aug 2026",
        "url": "https://support.google.com/webmasters/answer/16908024?hl=en",
    },
    "google-genai-report-details": {
        "text": ("The Generative AI performance report (Search) shows impressions only, with Pages, Countries, Dates "
                 "and Devices tabs and no query data. It may not appear when a site has too few generative AI "
                 "impressions or is excluded from these features."),
        "source": "search console help",
        "url": "https://support.google.com/webmasters/answer/16984139?hl=en",
    },
    # Oct 22 2026 batch (B7), each number checked against its source page.
    'sparktoro-zero-click-68pct-2026': {
        "text": "SparkToro analyzed Similarweb's U.S. desktop and mobile clickstream panel for January-April 2026 (published June 8, 2026): 68.01% of Google searches ended without a click, up from 60.45% in SparkToro's 2024 study, which used a different panel (Datos). Only 0.34% of searches went on to AI Mode, and the data excludes the Google app, where SparkToro expects the zero-click share is even higher.",
        "source": 'sparktoro, june 2026',
        "url": 'https://sparktoro.com/blog/in-2026-less-than-one-third-of-google-searches-still-send-a-click/',
    },
    'pew-chatbots-search-42pct-2026': {
        "text": 'Pew Research Center surveyed 5,119 U.S. adults on Feb. 17-23, 2026 (published June 17, 2026): 42% say they use AI chatbots to search for information, the most common use Pew asked about, and six in ten say they read the AI summaries at the top of search engine results.',
        "source": 'pew research center, june 2026',
        "url": 'https://www.pewresearch.org/internet/2026/06/17/americans-and-ai-2026-chatbots-smart-devices-and-views-on-impact/',
    },
    'brightlocal-ai-local-recs-45pct-2026': {
        "text": "BrightLocal's Local Consumer Review Survey 2026 (1,002 U.S. adults, published Feb. 11, 2026): 45% used ChatGPT or other generative AI tools for local business recommendations, up from 6% the year before, making AI the third most-used recommendation source after Google and Facebook. The share using Google for reviews fell from 83% in 2025 to 71%.",
        "source": 'brightlocal, february 2026',
        "url": 'https://www.brightlocal.com/research/local-consumer-review-survey/',
    },
    'search-console-branded-queries-filter': {
        "text": "Google added a branded queries filter to Search Console's Performance report (announced Nov. 20, 2025; available to all eligible sites since March 11, 2026). It splits clicks, impressions, CTR and average position into branded and non-branded queries using an AI-assisted classifier, works only for top-level properties (not URL-path or subdomain properties) with enough query volume, and has no effect on ranking.",
        "source": 'google search central, november 2025',
        "url": 'https://developers.google.com/search/blog/2025/11/search-console-branded-filter',
    },
    'google-faq-rich-results-retired-2026': {
        "text": "Google's Search documentation changelog says FAQ rich results no longer appear in Google Search as of May 7, 2026 (deprecation notice posted May 8). On June 15, 2026, Google removed the FAQ rich result documentation because the feature 'is no longer shown in Google Search results.'",
        "source": 'google search central, may 2026',
        "url": 'https://developers.google.com/search/updates#faq-deprecation',
    },
    'google-ai-mode-queries-3x-longer': {
        "text": 'Google said on May 19, 2026 that AI Mode, one year after its U.S. launch, has passed 1 billion monthly active users globally and that AI Mode queries have more than doubled every quarter since launch. In the U.S., the average AI Mode search is triple the length of a traditional Search query.',
        "source": 'google, may 2026',
        "url": 'https://blog.google/products-and-platforms/products/search/ai-mode-us-insights/',
    },
    'ahrefs-brand-mentions-ai-overviews': {
        "text": "Ahrefs studied 75,000 brands (domains with Domain Rating above 40) against their mentions in Google's AI Overviews (published May 26, 2025): branded web mentions had the strongest Spearman correlation with AI Overview visibility at 0.664, versus 0.218 for number of backlinks. The top three factors were all off-site: web mentions, branded anchor text (0.527) and branded search volume (0.392).",
        "source": 'ahrefs, may 2025',
        "url": 'https://ahrefs.com/blog/ai-overview-brand-correlation/',
    },
    'ahrefs-ai-cites-fresher-content': {
        "text": "Ahrefs analyzed 16.975 million cited URLs from ChatGPT, Perplexity, Gemini, Copilot, AI Overviews and Google's organic results (published July 28, 2025): pages cited by the four AI assistants averaged 1,064 days (2.9 years) since publication, which Ahrefs calls 25.7% 'fresher' than organic results. ChatGPT's citations were the newest, 458 days newer than organic results on average, while AI Overviews skewed oldest.",
        "source": 'ahrefs, july 2025',
        "url": 'https://ahrefs.com/blog/do-ai-assistants-prefer-to-cite-fresh-content/',
    },
    # Pentra's own method (rule 2a, Oct 19 2026; the homepage says the same).
    "pentra-four-checks": {
        "text": ("Pentra's Autopilot holds an AI-written article for four objective reasons only: a broken page, "
                 "leftover pipeline text, a statistic its cited source doesn't contain, and a price, plan or product "
                 "number the business didn't confirm. Editorial and fact scores are notes, not blockers."),
        "source": "pentra's own publishing checks",
        "url": "https://pentra.dev",
    },
    # B7 (Oct 23 2026): verified on each publisher's own page that day.
    "brightlocal-maps-vs-ai-2026": {
        "text": ("BrightLocal tracked 1,342 business locations over 60 days with about 200,000 local, non-branded "
                 "prompts. Google Maps mentioned the tracked business in 66% of searches; AI Overviews 38%, "
                 "ChatGPT 33%, Google AI Mode 32%. Business websites were 93% of the unique domains the AI tools "
                 "cited and 42% of all citations. The page does not give the dates of the 60 days."),
        "source": "brightlocal, september 2026",
        "url": "https://www.brightlocal.com/research/local-ai-visibility-study/",
    },
    "brightlocal-review-thresholds-2026": {
        "text": ("BrightLocal's Local Consumer Review Survey 2026 (1,002 US adults, published Feb 11, 2026): 47% of "
                 "consumers won't use a business with fewer than 20 reviews, and only 9% would use one with five "
                 "or fewer. 68% will only use a business rated four stars or higher, up from 55% in 2025. It "
                 "measures which business customers choose, not rankings."),
        "source": "brightlocal, february 2026",
        "url": "https://www.brightlocal.com/research/local-consumer-review-survey/",
    },
    "whitespark-local-pack-factors-2026": {
        "text": ("Whitespark's 2026 Local Search Ranking Factors (published Nov 6, 2025): 47 local search experts "
                 "scored 187 factors. Their top five for the Google local pack and Maps: the primary Google "
                 "Business Profile category, how close the business is to the searcher, keywords in the profile's "
                 "business title, a physical address in the city searched, and being open at the time of the "
                 "search. It is an expert survey, not measured ranking data, and an expert on the same page lists "
                 "keyword-stuffing the profile name as spam that can get a profile suspended."),
        "source": "whitespark, november 2025",
        "url": "https://whitespark.ca/local-search-ranking-factors/",
    },
    "semrush-aio-trigger-rates-2025": {
        "text": ("Semrush (10 million+ keywords, updated Dec 2025): AI Overviews showed for 6.49% of queries in "
                 "January 2025, 24.61% in July and 15.69% in November. For keywords that began showing an AI "
                 "Overview, the zero-click rate went from 33.75% to 31.53%. The page does not state the country "
                 "or device."),
        "source": "semrush, december 2025",
        "url": "https://www.semrush.com/blog/semrush-ai-overviews-study/",
    },
    "yext-ai-citations-2025": {
        "text": ("Yext analyzed 6.8 million citations from ChatGPT, Gemini and Perplexity (July 1 to Aug 31, 2025): "
                 "44% pointed to websites, 42% to listings, 8% to reviews and social, and about 2% to forums such "
                 "as Reddit."),
        "source": "yext, october 2025",
        "url": "https://yext.com/about/news-media/ai-citations-release",
    },
}

# What each format does (the research playbook's F1-F7 that run without a
# person; receipts and reader questions need one). max_chars is the gate.
# Oct 23 2026 (X limited the account on Oct 22): shorter, plain posts with no
# label opening ("10-minute SEO win:") and no promised time.
POST_FORMATS: dict[str, dict] = {
    "search_console_check": {
        "structure": ("line one: what the reader will find out, said plainly. then the steps as short lines (at most "
                      "four), naming the real screens from the cards. then what to do with the result. then one line "
                      "on why, naming its source."),
        "max_chars": 800,
    },
    "study_translated": {
        "structure": ("line one: who studied what, with the sample and date. then the one number that matters. "
                      "then what it means for a small business with a handful of people. then one action this "
                      "week. then one caveat."),
        "max_chars": 700,
    },
    "myth_vs_source": {
        "structure": ("line one: the common advice in quotes, with no person named. then what the source says, as "
                      "a number or a short quote. then what to do instead."),
        "max_chars": 550,
    },
    "teardown": {
        "structure": ("line one: the example and where it comes from. then two concrete observations about it. "
                      "then the principle in one line. then how to apply it on your own site."),
        "max_chars": 650,
    },
    "pre_publish_check": {
        "structure": ("line one: before an AI-written page goes live, check four things. then the four checks as "
                      "short lines. then what can wait. then one line naming Google's guidance as the source. "
                      "this is pentra's own method, so \"we\" is fine after line one."),
        "max_chars": 700,
    },
    "two_findings": {
        "structure": ("finding a with its source. finding b with its source. why both can hold, labeled as our "
                      "reading. what to do."),
        "max_chars": 700,
    },
    # News posts (Oct 24 2026, bot/news.py): an official Google Search change, posted within hours.
    "search_news": {
        "structure": ("line one: what Google changed or announced, in plain words. then the detail that matters "
                      "for a small business site, from the card. then what to do or check, only if a card says "
                      "it; otherwise when to look again. then the source in plain words with its date."),
        "max_chars": 600,
    },
}

# Two posts a day; the plan repeats afterwards with a new example each time.
POST_PLAN: list[tuple[str, list[str]]] = [
    ("search_console_check", ["howto:page-two-rewrite", "ahrefs-aio-top10"]),
    ("study_translated", ["pew-ai-summary-clicks", "ahrefs-aio-triggers"]),
    ("myth_vs_source", ["ahrefs-schema", "google-ignore-list"]),
    ("search_console_check", ["google-genai-report", "google-genai-report-details", "google-genai-control"]),
    ("two_findings", ["lilyray-listicles", "ahrefs-chatgpt-listicles"]),
    ("pre_publish_check", ["pentra-four-checks", "google-ai-content"]),
    ("search_console_check", ["howto:crawled-not-indexed"]),
    ("study_translated", ["seer-aio-ctr"]),
    ("teardown", ["google-commodity"]),
    ("myth_vs_source", ["google-no-special-requirements", "google-ignore-list"]),
    ("search_console_check", ["howto:queries-without-a-page", "google-query-fanout"]),
    ("study_translated", ["ahrefs-aio-ctr-58", "ahrefs-ctr-benchmark"]),
    ("search_console_check", ["howto:inspect-new-page"]),
    ("search_console_check", ["howto:link-from-pages-that-mention-it", "backlinko-page-two"]),
    # Week 2 (B7, Oct 22 2026 cards).
    ("study_translated", ["brightlocal-ai-local-recs-45pct-2026"]),
    ("search_console_check", ["search-console-branded-queries-filter"]),
    ("myth_vs_source", ["google-faq-rich-results-retired-2026"]),
    ("study_translated", ["sparktoro-zero-click-68pct-2026"]),
    ("two_findings", ["ahrefs-brand-mentions-ai-overviews", "ahrefs-schema"]),
    ("study_translated", ["pew-chatbots-search-42pct-2026"]),
    ("study_translated", ["google-ai-mode-queries-3x-longer", "google-query-fanout"]),
    ("study_translated", ["ahrefs-ai-cites-fresher-content"]),
    # Week 3 (B7, Oct 23 2026): new Search Console how-tos and local studies.
    ("study_translated", ["brightlocal-maps-vs-ai-2026"]),
    ("search_console_check", ["howto:duplicate-canonical"]),
    ("study_translated", ["brightlocal-review-thresholds-2026"]),
    ("search_console_check", ["howto:discovered-not-indexed"]),
    ("study_translated", ["whitespark-local-pack-factors-2026"]),
    ("search_console_check", ["howto:sitemap-status"]),
    ("two_findings", ["yext-ai-citations-2025", "brightlocal-maps-vs-ai-2026"]),
    ("search_console_check", ["howto:links-top-pages"]),
    ("study_translated", ["semrush-aio-trigger-rates-2025"]),
    ("search_console_check", ["howto:cwv-poor-first"]),
]


def card(card_id: str) -> dict[str, str]:
    """A card by id; "howto:<id>" is one of the checked Search Console how-tos."""
    if card_id.startswith("howto:"):
        howto = next(c for c in HOW_TO_CARDS if c["id"] == card_id.split(":", 1)[1])
        return {"text": howto["text"], "source": "search console", "url": ""}
    return EVIDENCE_CARDS[card_id]


def plan_entry(index: int) -> tuple[str, list[dict[str, str]]]:
    format_id, card_ids = POST_PLAN[index % len(POST_PLAN)]
    return format_id, [{"id": cid, **card(cid)} for cid in card_ids]
