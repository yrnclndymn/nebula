"""Third-party news search for tracked companies (#35): the pure relevance /
entity-match filter (guards name collisions), plus the durable news-capture job
end-to-end.

The filter tests need no DB or network. The job test needs Neo4j and skips
cleanly when it's unreachable — the news search and the LLM confirm are stubbed
so it exercises the real write path (upsert_signal dedup + canonical-URL dedup
against site-sourced signals) without hitting the internet. Fixtures are
fictional (Acme / Globex / common-word names); no real tracked-company names.
"""

import asyncio

import pytest

from app.capture import news
from app.capture.news import NewsHit, entity_relevance
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.schema import apply_schema

# --- Pure: relevance / entity-match filter ---------------------------------


def test_multiword_name_in_snippet_only_is_relevant():
    # A multi-word name in the body is a strong enough subject signal on its own —
    # multi-word collisions are rare.
    r = entity_relevance(
        "Globex Systems",
        title="Enterprise software market heats up",
        summary="Globex Systems announced a new platform on Tuesday.",
    )
    assert r.relevant and r.score >= 2


def test_name_in_headline_is_relevant():
    r = entity_relevance("Acme", title="Acme launches a new product", summary="")
    assert r.relevant


def test_single_common_word_in_snippet_only_without_cue_is_filtered():
    # "Summit" is an ordinary English word — a bare body mention is almost
    # certainly a collision, so the fail-closed filter drops it.
    r = entity_relevance(
        "Summit",
        title="Leaders gather for the annual climate talks",
        summary="Officials reached the summit after long negotiations.",
    )
    assert not r.relevant


def test_single_common_word_in_snippet_with_business_cue_is_relevant():
    r = entity_relevance(
        "Summit",
        title="Tech roundup",
        summary="Summit, an AI startup, raised a Series B round this week.",
    )
    assert r.relevant


def test_single_common_word_in_headline_is_relevant():
    r = entity_relevance("Summit", title="Summit raises $40m", summary="")
    assert r.relevant


def test_no_mention_fails_closed():
    r = entity_relevance(
        "Acme",
        title="Globex opens a Berlin office",
        summary="The move expands Globex into Europe.",
    )
    assert not r.relevant and r.score == 0


def test_substring_is_not_a_match():
    # "Acme" must not match "Acmelabs" — matching is whole-word (word-boundary).
    r = entity_relevance(
        "Acme",
        title="Acmelabs ships a database",
        summary="Acmelabs is unrelated to the tracked company.",
    )
    assert not r.relevant


def test_alias_matches():
    r = entity_relevance(
        "Acme",
        title="ACME Corporation reports growth",
        summary="",
        aliases=("Acme Corporation",),
    )
    assert r.relevant


def test_website_brand_token_matches_when_name_absent():
    # The article names the brand ("Globex") which we derive from the website even
    # though the tracked name string differs slightly.
    r = entity_relevance(
        "Globex Inc",
        title="Globex hits one million users",
        summary="",
        website="https://globex.example",
    )
    assert r.relevant


def test_normalisation_is_case_and_punctuation_insensitive():
    r = entity_relevance(
        "Acme",
        title="ACME, INC. announces results",
        summary="",
    )
    assert r.relevant


# --- Pure: record conversion ------------------------------------------------


def test_to_record_sets_outlet_as_source_and_news_kind():
    hit = NewsHit(
        title="Acme raises Series B",
        url="https://www.example-news.com/tech/acme-series-b?utm_source=nl",
        published_at="2026-06-01T09:30:00+00:00",
        summary="Acme closed $40m.",
        outlet="Example News",
    )
    record = news._to_record(hit, "Acme")
    assert record is not None
    assert record.kind == "news"
    # Source is the third-party outlet (host-based identity), NOT the company.
    assert record.source == "https://example-news.com"
    # canonical URL drops the utm_* tracking param.
    assert record.canonical_url().endswith("/tech/acme-series-b")


def test_to_record_drops_untitled_or_unlinked():
    assert news._to_record(NewsHit(title="", url="https://x.example/a"), "Acme") is None
    assert news._to_record(NewsHit(title="A story", url=""), "Acme") is None


def test_outlet_source_prefers_domain_shaped_outlet_over_aggregator_host():
    """On aggregator redirect URLs the article host is the aggregator; a
    domain-shaped DDGS outlet identifies the real publisher instead. A display-name
    outlet is never fabricated into a domain (#86 review)."""
    redirect = NewsHit(
        title="t",
        url="https://www.msn.example/en-us/news/story-id",
        outlet="realpaper.example",
    )
    assert news._outlet_source(redirect) == "https://realpaper.example"
    display_name = NewsHit(
        title="t",
        url="https://www.msn.example/en-us/news/story-id",
        outlet="Real Paper Weekly",
    )
    assert news._outlet_source(display_name) == "https://msn.example"


def test_llm_filter_fails_safe_on_error(monkeypatch):
    """A quota/model error must return the pure shortlist unchanged — the job
    never depends on the LLM confirm (#86 review)."""
    hits = [NewsHit(title="Acme ships", url="https://a.example/1")]

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated 429")

    monkeypatch.setattr(news, "generate_with_retry", boom)
    assert asyncio.run(news.llm_filter_subjects("Acme", hits)) == hits


# --- Integration: durable news-capture job (needs Neo4j; net + LLM stubbed) --

# The company name is the subject term the (fictional) articles actually use, so
# the entity-match filter can fire; test isolation rides on the MARK embedded in
# the Signal/Source URLs (+ the exact company name) rather than in the name.
MARK = "__pytest_news__"
ACME = "Acme"


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
        await session.run("MATCH (j:Job) WHERE j.type = 'news_capture' DETACH DELETE j")


def test_news_capture_job_writes_signals_filters_and_dedupes(monkeypatch):
    outlet = f"{MARK}-outlet.example.com"
    website = f"https://{MARK}-acme.example.com"

    # Two on-topic hits (Acme is the subject) and one collision (a foreign
    # "Acme" the filter must drop — no business cue, body mention only).
    relevant_hits = [
        NewsHit(
            title="Acme ships v2",
            url=f"https://{outlet}/acme-v2?utm_source=nl",
            published_at="Mon, 01 Jun 2026 09:30:00 GMT",
            summary="Acme, a software company, launched version 2.",
            outlet="Outlet A",
        ),
        NewsHit(
            title="Acme hires a new CTO",
            url=f"https://{outlet}/acme-cto",
            published_at="2026-04-15T12:00:00+00:00",
            summary="The startup Acme named a CTO.",
            outlet="Outlet A",
        ),
    ]
    collision = NewsHit(
        title="Road runner spotted in the desert",
        url=f"https://{outlet}/roadrunner",
        published_at="2026-05-01",
        summary="An acme of natural beauty was seen at the canyon.",
        outlet="Outlet A",
    )

    async def fake_search_news(query, max_results=10):
        return [*relevant_hits, collision]

    # LLM confirm is stubbed to a pass-through (identity) so the job exercises the
    # pure filter's decision without a network/LLM call.
    async def fake_llm_filter(name, hits):
        return hits

    monkeypatch.setattr(news, "search_news", fake_search_news)
    monkeypatch.setattr(news, "llm_filter_subjects", fake_llm_filter)

    async def scenario():
        if not await _neo4j_available():
            return "skip"
        driver = get_driver()
        await apply_schema(driver)
        await _cleanup(driver)
        async with driver.session() as session:
            await session.run("CREATE (c:Company {name: $n, website: $w})", n=ACME, w=website)

        first = await news.enqueue_news_capture(ACME)
        await news.execute_news_capture_job(first["job_id"])
        job1 = await news.get_news_capture(first["job_id"])

        # Re-run: same hits -> canonical-URL dedup, nothing new.
        second = await news.enqueue_news_capture(ACME)
        await news.execute_news_capture_job(second["job_id"])
        job2 = await news.get_news_capture(second["job_id"])

        async with driver.session() as session:
            result = await session.run(
                f"MATCH (:Company {{name: $n}})-[:MENTIONED_IN]->(s:Signal) "
                f"WHERE s.url CONTAINS '{MARK}' "
                "OPTIONAL MATCH (s)-[:FROM_SOURCE]->(src:Source) "
                "RETURN count(DISTINCT s) AS signals, count(DISTINCT src) AS sources, "
                "collect(DISTINCT s.url) AS urls, collect(DISTINCT s.kind) AS kinds",
                n=ACME,
            )
            row = (await result.single()).data()
        await _cleanup(driver)
        await close_driver()
        return job1, job2, row

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")
    job1, job2, row = out
    # Only the two on-topic hits are stored; the collision is filtered out.
    assert row["signals"] == 2
    assert job1["captured"] == 2 and job1["new"] == 2
    assert job2["captured"] == 2 and job2["new"] == 0  # rerun dedupes
    assert job1["status"] == "done" and "2" in job1["outcome"]
    assert row["kinds"] == ["news"]  # third-party coverage stored as news
    assert row["sources"] >= 1  # the outlet recorded as provenance
    # utm_* was canonicalised away.
    assert any(url.endswith("/acme-v2") for url in row["urls"])
    assert not any("roadrunner" in url for url in row["urls"])
