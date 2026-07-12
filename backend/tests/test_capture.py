"""Company-site signal capture (#34): feed discovery/parsing, date normalisation,
section detection (all pure), plus the durable capture job end-to-end.

The pure tests need no DB. The job test needs Neo4j and skips cleanly when it's
unreachable — network + LLM are stubbed so it exercises the real write path
(upsert_signal dedup) without hitting the internet. Fixtures are fictional
(Acme/Globex).
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.capture import job as capture_job
from app.capture.dates import normalise_date
from app.capture.feeds import discover_feeds, parse_feed
from app.capture.sections import classify_section, find_section_pages
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.schema import apply_schema

# --- Pure: RSS/Atom autodiscovery ------------------------------------------

_HOME_HTML = """
<html><head>
  <link rel="stylesheet" href="/site.css">
  <link rel="alternate" type="application/rss+xml" title="News" href="/news/feed.xml">
  <link rel="alternate" type="application/atom+xml" title="Blog" href="https://acme.example/blog/atom">
  <link rel="alternate" type="text/html" href="/mobile">
  <link rel="alternate" type="application/rss+xml" href="/news/feed.xml">
</head><body>hi</body></html>
"""


def test_discover_feeds_finds_rss_and_atom_absolutises_and_dedupes():
    feeds = discover_feeds(_HOME_HTML, "https://acme.example/")
    assert feeds == [
        "https://acme.example/news/feed.xml",  # relative -> absolute
        "https://acme.example/blog/atom",  # already absolute
    ]  # the text/html alternate is ignored; the duplicate rss link is collapsed


def test_discover_feeds_empty_when_none():
    assert discover_feeds("<html><head></head><body>x</body></html>", "https://acme.example") == []
    assert discover_feeds("", "https://acme.example") == []


# --- Pure: feed parsing (RSS 2.0 + Atom) -----------------------------------

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Acme News</title>
  <item>
    <title>Acme raises Series B</title>
    <link>https://acme.example/news/series-b</link>
    <pubDate>Mon, 01 Jun 2026 09:30:00 GMT</pubDate>
    <description>Acme closed &lt;b&gt;$40m&lt;/b&gt; to expand.</description>
  </item>
  <item>
    <title>Acme opens Berlin office</title>
    <link>https://acme.example/news/berlin</link>
    <pubDate>Tue, 15 Apr 2026 12:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Globex Blog</title>
  <entry>
    <title>Scaling our platform</title>
    <link rel="alternate" href="https://globex.example/blog/scaling"/>
    <published>2026-05-20T10:00:00Z</published>
    <summary>How we scaled.</summary>
  </entry>
  <entry>
    <title>Hiring engineers</title>
    <link href="https://globex.example/blog/hiring"/>
    <updated>2026-03-02T08:00:00Z</updated>
    <content>We are hiring.</content>
  </entry>
</feed>
"""


def test_parse_rss():
    items = parse_feed(_RSS, "https://acme.example/news/feed.xml")
    assert [i.title for i in items] == ["Acme raises Series B", "Acme opens Berlin office"]
    assert items[0].url == "https://acme.example/news/series-b"
    assert items[0].published_at == "Mon, 01 Jun 2026 09:30:00 GMT"
    assert "$40m" in items[0].summary and "<b>" not in items[0].summary  # HTML stripped
    assert items[1].summary is None  # no description


def test_parse_atom_prefers_alternate_link_and_published():
    items = parse_feed(_ATOM, "https://globex.example/")
    assert [i.title for i in items] == ["Scaling our platform", "Hiring engineers"]
    assert items[0].url == "https://globex.example/blog/scaling"
    assert items[0].published_at == "2026-05-20T10:00:00Z"
    assert items[0].summary == "How we scaled."
    # second entry falls back to <updated> and <content>
    assert items[1].published_at == "2026-03-02T08:00:00Z"
    assert items[1].url == "https://globex.example/blog/hiring"


def test_parse_feed_relative_links_absolutised():
    rss = (
        '<rss version="2.0"><channel><item><title>T</title>'
        "<link>/news/x</link></item></channel></rss>"
    )
    items = parse_feed(rss, "https://acme.example/news/feed.xml")
    assert items[0].url == "https://acme.example/news/x"


def test_parse_feed_bad_xml_returns_empty():
    assert parse_feed("not xml at all", "https://acme.example") == []
    assert parse_feed("", "https://acme.example") == []


# --- Pure: date normalisation ----------------------------------------------

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_normalise_rfc822_feed_date():
    iso = normalise_date("Mon, 01 Jun 2026 09:30:00 GMT")
    assert iso is not None and iso.startswith("2026-06-01T09:30:00")


@pytest.mark.parametrize(
    "raw, y, m, d",
    [
        ("2026-06-01", 2026, 6, 1),  # ISO delegated to parse_published_at
        ("June 1, 2026", 2026, 6, 1),  # human format delegated
    ],
)
def test_normalise_delegates_to_existing_parser(raw, y, m, d):
    iso = normalise_date(raw)
    dt = datetime.fromisoformat(iso)
    assert (dt.year, dt.month, dt.day) == (y, m, d)


@pytest.mark.parametrize(
    "raw, expected_day",
    [
        ("today", 10),
        ("yesterday", 9),
        ("2 days ago", 8),
        ("1 week ago", 3),
    ],
)
def test_normalise_relative_dates(raw, expected_day):
    iso = normalise_date(raw, now=_NOW)
    dt = datetime.fromisoformat(iso)
    assert dt.month == 6 and dt.day == expected_day


@pytest.mark.parametrize("raw", [None, "", "   ", "at some point", "whenever"])
def test_normalise_gives_up(raw):
    assert normalise_date(raw, now=_NOW) is None


# --- Pure: section (news/blog/events) index-page detection -----------------


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://acme.example/news", "news"),
        ("https://acme.example/newsroom", "news"),
        ("https://acme.example/press-releases", "news"),
        ("https://acme.example/blog", "blog"),
        ("https://acme.example/insights", "blog"),
        ("https://acme.example/events", "event"),
        ("https://acme.example/webinars", "event"),
        ("https://acme.example/about", None),
        ("https://acme.example/careers", None),
    ],
)
def test_classify_section(url, kind):
    assert classify_section(url) == kind


def test_find_section_pages_groups_and_caps():
    links = [
        {"url": "https://acme.example/news", "text": "News"},
        {"url": "https://acme.example/newsroom", "text": "Newsroom"},
        {"url": "https://acme.example/news-archive", "text": "Archive"},  # 3rd news -> capped
        {"url": "https://acme.example/blog", "text": "Blog"},
        {"url": "https://acme.example/about", "text": "About Us"},
        {"url": "https://acme.example/news", "text": "News"},  # duplicate url
    ]
    grouped = find_section_pages(links, limit_per_kind=2)
    assert grouped["news"] == ["https://acme.example/news", "https://acme.example/newsroom"]
    assert grouped["blog"] == ["https://acme.example/blog"]
    assert "event" not in grouped


# --- Pure: same-site guard ---------------------------------------------------


@pytest.mark.parametrize(
    ("item_url", "kept"),
    [
        ("https://acme.example/news/a", True),  # same domain
        ("https://blog.acme.example/post", True),  # own subdomain (blogs live there)
        ("https://www.acme.example/news/b", True),  # www-stripped
        ("https://evil.example/acme.example", False),  # foreign domain
        ("https://notacme.example/x", False),  # suffix trick, not a subdomain
    ],
)
def test_build_record_same_site_guard(item_url, kept):
    item = capture_job.FeedItem(title="Acme ships", url=item_url)
    record = capture_job._build_record(item, "news", "https://acme.example/feed", "acme.example")
    assert (record is not None) is kept


# --- Integration: durable capture job (needs Neo4j; network/LLM stubbed) ----

MARK = "__pytest_capture__"
ACME = f"Acme {MARK}"


async def _neo4j_available() -> bool:
    try:
        await check_connectivity()
        return True
    except Exception:
        return False


async def _cleanup(driver):
    async with driver.session() as session:
        await session.run(f"MATCH (s:Signal) WHERE s.url CONTAINS '{MARK}' DETACH DELETE s")
        await session.run("MATCH (c:Company) WHERE c.name = $n DETACH DELETE c", n=ACME)
        await session.run(f"MATCH (src:Source) WHERE src.url CONTAINS '{MARK}' DETACH DELETE src")
        await session.run("MATCH (j:Job) WHERE j.type = 'signal_capture' DETACH DELETE j")


def test_capture_job_writes_signals_and_dedupes_on_rerun(monkeypatch):
    domain = f"{MARK}.example.com"
    home_url = f"https://{domain}/"
    feed_url = f"https://{domain}/news/feed.xml"
    home_html = (
        '<html><head><link rel="alternate" type="application/rss+xml" '
        f'href="{feed_url}"></head><body>home</body></html>'
    )
    feed_xml = (
        '<rss version="2.0"><channel>'
        f"<item><title>Acme ships v2</title><link>https://{domain}/news/v2</link>"
        "<pubDate>Mon, 01 Jun 2026 09:30:00 GMT</pubDate>"
        "<description>Big release.</description></item>"
        f"<item><title>Acme hires CTO</title><link>https://{domain}/news/cto?utm_source=nl</link>"
        "<pubDate>Tue, 15 Apr 2026 12:00:00 GMT</pubDate></item>"
        "</channel></rss>"
    )

    async def fake_fetch_raw(url: str):
        if url.rstrip("/") == home_url.rstrip("/"):
            return home_html
        if url == feed_url:
            return feed_xml
        return None

    # fetch_page (the cached section-crawl path) is never needed here because the
    # feed yields items; stub it to be safe so no real network is touched.
    async def fake_fetch_page(url: str):
        return {"url": url, "text": "", "links": [], "images": [], "social": {}}

    monkeypatch.setattr(capture_job, "_fetch_raw", fake_fetch_raw)
    monkeypatch.setattr(capture_job, "fetch_page", fake_fetch_page)

    async def scenario():
        if not await _neo4j_available():
            return "skip"
        driver = get_driver()
        await apply_schema(driver)
        await _cleanup(driver)
        async with driver.session() as session:
            await session.run("CREATE (c:Company {name: $n, website: $w})", n=ACME, w=home_url)

        first = await capture_job.start_signal_capture(ACME)
        await capture_job.run_signal_capture_job(first["job_id"])
        job1 = await capture_job.get_signal_capture(first["job_id"])

        # Re-run: same feed -> no new signals, still deduped to 2.
        second = await capture_job.start_signal_capture(ACME)
        await capture_job.run_signal_capture_job(second["job_id"])
        job2 = await capture_job.get_signal_capture(second["job_id"])

        async with driver.session() as session:
            result = await session.run(
                f"MATCH (:Company {{name: $n}})-[:MENTIONED_IN]->(s:Signal) "
                f"WHERE s.url CONTAINS '{MARK}' "
                "OPTIONAL MATCH (s)-[:FROM_SOURCE]->(src:Source) "
                "RETURN count(DISTINCT s) AS signals, "
                "count(DISTINCT src) AS sources, "
                "collect(DISTINCT s.url) AS urls",
                n=ACME,
            )
            row = (await result.single()).data()
        await _cleanup(driver)
        await close_driver()
        return job1, job2, row

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    job1, job2, row = out
    assert row["signals"] == 2  # two feed items captured
    assert job1["captured"] == 2 and job1["new"] == 2  # first run: both new
    assert job2["captured"] == 2 and job2["new"] == 0  # rerun: dedup, nothing new
    assert job1["status"] == "done" and "2" in job1["outcome"]
    # the utm_* tracking param on the CTO link was canonicalised away
    assert any(url.endswith("/news/cto") for url in row["urls"])
    assert row["sources"] >= 1  # provenance recorded
