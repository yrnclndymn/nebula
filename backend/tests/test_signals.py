"""Signal model + write path + read queries (#33).

The canonicalisation and model tests are pure (no DB). The upsert/read tests need
Neo4j and skip cleanly when it's unreachable, so `make test` stays green in CI
without a database. Fixture data is fictional (Acme/Globex).
"""

import asyncio

import pytest
from pydantic import ValidationError

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import SignalRecord, canonicalise_url
from app.graph.schema import apply_schema
from app.graph.signals import (
    parse_published_at,
    recent_signals,
    signals_for_company,
    upsert_signal,
)

# --- Pure: URL canonicalisation --------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # http -> https
        ("http://example.com/path", "https://example.com/path"),
        # host lowercased, scheme lowercased; path case preserved
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        ("http://EXAMPLE.com/A/B", "https://example.com/A/B"),
        # scheme-less input defaults to https
        ("example.com/story", "https://example.com/story"),
        # trailing slash stripped (incl. bare root)
        ("https://example.com/story/", "https://example.com/story"),
        ("https://example.com/", "https://example.com"),
        # fragment dropped
        ("https://example.com/story#section-2", "https://example.com/story"),
        # utm_* stripped, real params kept
        ("https://example.com/s?utm_source=x&utm_medium=y&id=5", "https://example.com/s?id=5"),
        # fbclid / gclid stripped
        ("https://example.com/s?fbclid=abc", "https://example.com/s"),
        ("https://example.com/s?gclid=abc&q=1", "https://example.com/s?q=1"),
        # everything at once
        (
            "HTTP://News.Example.COM/Article/?utm_campaign=z&fbclid=1&ref=nl#top",
            "https://news.example.com/Article?ref=nl",
        ),
        # empty in, empty out
        ("", ""),
        ("   ", ""),
    ],
)
def test_canonicalise_url(raw, expected):
    assert canonicalise_url(raw) == expected


def test_canonicalise_url_is_idempotent():
    messy = "HTTP://Example.com/Story/?utm_source=x&id=9#frag"
    once = canonicalise_url(messy)
    assert canonicalise_url(once) == once


def test_canonicalise_url_dedupes_variants():
    """Tracking tags, casing, fragment and trailing slash all collapse to one key."""
    variants = [
        "https://example.com/story",
        "http://example.com/story/",
        "https://Example.com/story?utm_source=newsletter",
        "https://example.com/story/#comments",
        "https://example.com/story?fbclid=xyz",
    ]
    canon = {canonicalise_url(v) for v in variants}
    assert canon == {"https://example.com/story"}


# --- Pure: SignalRecord model ----------------------------------------------


def test_signal_record_validates_kind():
    assert SignalRecord(url="https://x.example/a", title="t", kind="blog").kind == "blog"
    with pytest.raises(ValidationError):
        SignalRecord(url="https://x.example/a", title="t", kind="tweet")


def test_signal_record_canonical_url():
    rec = SignalRecord(url="HTTP://Example.com/a/?utm_source=x", title="t")
    assert rec.canonical_url() == "https://example.com/a"


# --- Pure: published-at parsing --------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["2026-06-01", "2026-06-01T12:30:00", "2026-06-01T12:30:00Z", "June 1, 2026", "2026/06/01"],
)
def test_parse_published_at_parses(raw):
    dt = parse_published_at(raw)
    assert dt is not None and dt.year == 2026 and dt.month == 6 and dt.day == 1
    assert dt.tzinfo is not None  # always aware


@pytest.mark.parametrize("raw", [None, "", "   ", "last Tuesday", "sometime in spring"])
def test_parse_published_at_gives_up(raw):
    assert parse_published_at(raw) is None


# --- Integration (needs Neo4j) ---------------------------------------------

MARK = "__pytest_signal__"
ACME = f"Acme {MARK}"
GLOBEX = f"Globex {MARK}"
SRC = f"https://{MARK}.example.com/src"


async def _neo4j_available() -> bool:
    try:
        await check_connectivity()
        return True
    except Exception:
        return False


async def _cleanup(driver):
    async with driver.session() as session:
        await session.run(f"MATCH (s:Signal) WHERE s.url CONTAINS '{MARK}' DETACH DELETE s")
        await session.run(
            "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=[ACME, GLOBEX]
        )
        await session.run(f"MATCH (src:Source) WHERE src.url CONTAINS '{MARK}' DETACH DELETE src")


def test_upsert_dedup_and_mention_union():
    async def scenario():
        if not await _neo4j_available():
            return "skip"
        driver = get_driver()
        await apply_schema(driver)
        await _cleanup(driver)

        # First capture: story with a tracking param, mentions Acme.
        canon = await upsert_signal(
            driver,
            SignalRecord(
                url=f"https://{MARK}.example.com/story?utm_source=nl",
                title="Original title",
                kind="news",
                published_at="2026-06-01",
                summary="v1",
                source=SRC,
            ),
            companies=[ACME],
        )
        # Second capture: same canonical URL (trailing slash + fragment), updated
        # title, mentions Globex — must update in place and union the mention.
        canon2 = await upsert_signal(
            driver,
            SignalRecord(
                url=f"https://{MARK}.example.com/story/#top",
                title="Updated title",
                kind="news",
                published_at="2026-06-02",
                summary="v2",
                source=SRC,
            ),
            companies=[GLOBEX],
        )

        async with driver.session() as session:
            result = await session.run(
                f"""
                MATCH (s:Signal) WHERE s.url CONTAINS '{MARK}'
                OPTIONAL MATCH (c:Company)-[:MENTIONED_IN]->(s)
                OPTIONAL MATCH (s)-[:FROM_SOURCE]->(src:Source)
                RETURN count(DISTINCT s) AS signals, s.title AS title,
                       collect(DISTINCT c.name) AS companies,
                       count(DISTINCT src) AS sources
                """
            )
            row = (await result.single()).data()
        await _cleanup(driver)
        await close_driver()
        return canon, canon2, row

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    canon, canon2, row = out
    assert canon == canon2 == f"https://{MARK}.example.com/story"
    assert row["signals"] == 1  # deduped, not duplicated
    assert row["title"] == "Updated title"  # updated in place
    assert set(row["companies"]) == {ACME, GLOBEX}  # mentions unioned
    assert row["sources"] == 1


def test_read_queries_order_newest_first():
    async def scenario():
        if not await _neo4j_available():
            return "skip"
        driver = get_driver()
        await apply_schema(driver)
        await _cleanup(driver)

        jan = f"https://{MARK}.example.com/jan"
        mar = f"https://{MARK}.example.com/mar"
        jun = f"https://{MARK}.example.com/jun"
        blog = f"https://{MARK}.example.com/blog"
        for url, date, kind in [
            (jan, "2026-01-15", "news"),
            (jun, "2026-06-15", "news"),
            (mar, "2026-03-15", "news"),
            (blog, "2026-04-15", "blog"),
        ]:
            await upsert_signal(
                driver,
                SignalRecord(url=url, title=url, kind=kind, published_at=date),
                companies=[ACME],
            )

        per_company = await signals_for_company(driver, ACME, limit=10)
        recent = await recent_signals(driver, limit=10)
        blogs_only = await recent_signals(driver, limit=10, kind="blog")

        await _cleanup(driver)
        await close_driver()
        return per_company, recent, blogs_only, (jan, mar, jun, blog)

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    per_company, recent, blogs_only, (jan, mar, jun, blog) = out

    news_order = [s["url"] for s in per_company if s["url"] in (jan, mar, jun)]
    assert news_order == [jun, mar, jan]  # newest publishedAt first
    recent_news = [s["url"] for s in recent if s["url"] in (jan, mar, jun)]
    assert recent_news == [jun, mar, jan]
    assert [s["url"] for s in blogs_only] == [blog]  # kind filter
    assert all(s["kind"] == "blog" for s in blogs_only)
