"""People-in-signals extraction + matching (#41).

The extraction heuristics (bylines / quotes / speakers), the name validator and
the matching-precedence function are all pure — no DB, no network — so they are
tested directly. The edge-write path needs Neo4j and skips cleanly when it is
unreachable (CI's Neo4j service is the arbiter). Fixture data is fictional
(Acme/Globex, invented person names).
"""

import asyncio

import pytest

from app.capture.people import (
    AUTHORED,
    QUOTED_IN,
    SPOKE_AT,
    PersonMention,
    extract_bylines,
    extract_people,
    extract_quoted,
    extract_speakers,
    looks_like_person_name,
    resolve_mention,
)

# --- Pure: name validation --------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Jane Doe", "John Q. Public", "Jean-Luc Picard", "Sam O'Neil", "Maria Del Rio"],
)
def test_looks_like_person_name_accepts(name):
    assert looks_like_person_name(name)


@pytest.mark.parametrize(
    "text",
    [
        "Jane",  # single token — never confident
        "Acme Corp",  # org suffix
        "Globex Technologies",  # org suffix
        "AI Summit",  # capitalised non-name words
        "Bank Of America",  # stopword token
        "Jane Doe 3",  # digits
        "",  # empty
        "the team",  # lowercase / stopword
        "Quarterly Report",  # non-name words
    ],
)
def test_looks_like_person_name_rejects(text):
    assert not looks_like_person_name(text)


# --- Pure: byline extraction (blog authors) --------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("By Jane Doe", ["Jane Doe"]),
        ("Written by John Smith", ["John Smith"]),
        ("Posted by Maria Garcia on the company blog", ["Maria Garcia"]),
        ("Authored by Alex Rivera", ["Alex Rivera"]),
        ("By Jane Doe and John Smith", ["Jane Doe", "John Smith"]),
        ("Author: Sam Lee", ["Sam Lee"]),
        # No byline / non-name tails yield nothing.
        ("A product built by the Acme team", []),
        ("Ten lessons learned this quarter", []),
    ],
)
def test_extract_bylines(text, expected):
    assert extract_bylines(text) == expected


# --- Pure: quote extraction (people quoted in articles) ---------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ('"We are thrilled," said John Smith.', ["John Smith"]),
        ("Jane Doe said the rollout went well.", ["Jane Doe"]),
        ("According to Maria Garcia, adoption doubled.", ["Maria Garcia"]),
        ("said Jane Doe, the chief executive", ["Jane Doe"]),
        ("Alex Rivera added that hiring continues.", ["Alex Rivera"]),
        ("The market grew sharply this year.", []),
    ],
)
def test_extract_quoted(text, expected):
    assert extract_quoted(text) == expected


# --- Pure: speaker extraction (event speakers) ------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Featuring Jane Doe and John Smith", ["Jane Doe", "John Smith"]),
        ("Keynote by Alex Rivera", ["Alex Rivera"]),
        (
            "Speakers: Jane Doe, John Smith, Maria Garcia",
            ["Jane Doe", "John Smith", "Maria Garcia"],
        ),
        ("Presented by Sam Lee", ["Sam Lee"]),
        ("Join us for a full day of talks", []),
    ],
)
def test_extract_speakers(text, expected):
    assert extract_speakers(text) == expected


# --- Pure: extract_people dispatch by kind ----------------------------------


def test_extract_people_blog_byline_and_quote():
    mentions = extract_people(
        "blog",
        title="Scaling our platform",
        summary='By Jane Doe. "It was hard," said John Smith.',
    )
    by_relation = {(m.name, m.relation) for m in mentions}
    assert ("Jane Doe", AUTHORED) in by_relation
    assert ("John Smith", QUOTED_IN) in by_relation


def test_extract_people_news_quotes_only():
    mentions = extract_people(
        "news",
        title="Acme raises a round",
        summary="According to Maria Garcia, the funding closed.",
    )
    assert mentions == [PersonMention(name="Maria Garcia", relation=QUOTED_IN)]


def test_extract_people_event_speakers():
    mentions = extract_people("event", title="Annual Summit", summary="Keynote by Alex Rivera")
    assert mentions == [PersonMention(name="Alex Rivera", relation=SPOKE_AT)]


def test_extract_people_dedupes_author_over_quote():
    # An author quoting themselves collapses to the stronger AUTHORED relation.
    mentions = extract_people("blog", title="", summary="By Jane Doe. Jane Doe said hello.")
    assert mentions == [PersonMention(name="Jane Doe", relation=AUTHORED)]


def test_extract_people_unknown_kind_is_empty():
    assert extract_people("tweet", title="By Jane Doe", summary="") == []


# --- Pure: matching precedence (LinkedIn > name@company > flag) --------------


def test_resolve_linkedin_match_is_confident():
    m = PersonMention("Jane Doe", QUOTED_IN, linkedin="https://linkedin.com/in/janedoe")
    link = resolve_mention(m, linkedin_eids=["eid-1"], name_company_eids=["eid-2", "eid-3"])
    assert link.target_eid == "eid-1"
    assert link.flagged is False


def test_resolve_unique_name_at_company_is_confident():
    m = PersonMention("Jane Doe", QUOTED_IN)
    link = resolve_mention(m, linkedin_eids=[], name_company_eids=["eid-2"])
    assert link.target_eid == "eid-2"
    assert link.flagged is False


def test_resolve_ambiguous_name_is_flagged_not_linked():
    m = PersonMention("Jane Doe", QUOTED_IN)
    link = resolve_mention(m, linkedin_eids=[], name_company_eids=["eid-2", "eid-3"])
    assert link.target_eid is None
    assert link.flagged is True
    assert "ambiguous" in (link.reason or "").lower()


def test_resolve_unknown_name_is_flagged_stub():
    m = PersonMention("Jane Doe", QUOTED_IN)
    link = resolve_mention(m, linkedin_eids=[], name_company_eids=[])
    assert link.target_eid is None
    assert link.flagged is True
    assert link.reason  # carries a human-readable reason


def test_resolve_linkedin_hint_without_graph_node_falls_through():
    # LinkedIn on the mention but no node keyed on it: fall to name@company.
    m = PersonMention("Jane Doe", QUOTED_IN, linkedin="https://linkedin.com/in/janedoe")
    link = resolve_mention(m, linkedin_eids=[], name_company_eids=["eid-9"])
    assert link.target_eid == "eid-9"
    assert link.flagged is False


# --- Integration (needs Neo4j) ---------------------------------------------

MARK = "__pytest_people_signal__"
ACME = f"Acme {MARK}"
SIGNAL_URL = f"https://{MARK}.example.com/story"


async def _neo4j_available() -> bool:
    from app.graph.driver import check_connectivity

    try:
        await check_connectivity()
        return True
    except Exception:
        return False


async def _cleanup(driver):
    async with driver.session() as session:
        await session.run(f"MATCH (s:Signal) WHERE s.url CONTAINS '{MARK}' DETACH DELETE s")
        await session.run(f"MATCH (p:Person) WHERE p.name CONTAINS '{MARK}' DETACH DELETE p")
        await session.run("MATCH (c:Company {name: $name}) DETACH DELETE c", name=ACME)


def test_link_people_confident_and_flagged():
    """A quote matching the company's sole same-name leader links confidently; an
    unknown name links to a flagged stub, never silently onto a real person."""

    async def scenario():
        if not await _neo4j_available():
            return "skip"
        from app.graph.driver import close_driver, get_driver
        from app.graph.models import SignalRecord
        from app.graph.schema import apply_schema
        from app.graph.signals import (
            person_signal_candidates,
            upsert_signal,
            write_person_signal_links,
        )

        leader = f"Jane Doe {MARK}"
        stranger = f"John Smith {MARK}"
        driver = get_driver()
        await apply_schema(driver)
        await _cleanup(driver)

        async with driver.session() as session:
            await session.run(
                "MERGE (c:Company {name: $c}) MERGE (p:Person {name: $p}) MERGE (p)-[:LEADS]->(c)",
                c=ACME,
                p=leader,
            )

        await upsert_signal(
            driver,
            SignalRecord(url=SIGNAL_URL, title="t", kind="news", summary="s"),
            companies=[ACME],
        )

        resolved = []
        for name in (leader, stranger):
            cand = await person_signal_candidates(
                driver, name=name, company=ACME, linkedin_canon=None
            )
            resolved.append(
                resolve_mention(
                    PersonMention(name, QUOTED_IN),
                    linkedin_eids=cand["linkedin_eids"],
                    name_company_eids=cand["name_company_eids"],
                )
            )
        counts = await write_person_signal_links(driver, SIGNAL_URL, resolved, source=None)
        # Idempotent: a second write must not duplicate edges or stubs.
        await write_person_signal_links(driver, SIGNAL_URL, resolved, source=None)

        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (p:Person)-[r:QUOTED_IN]->(s:Signal {url: $url})
                RETURN p.name AS name, r.flagged AS flagged, p.origin AS origin
                ORDER BY name
                """,
                url=SIGNAL_URL,
            )
            rows = [dict(rec) async for rec in result]
            leader_edges = await session.run(
                "MATCH (:Person {name: $p})-[r:QUOTED_IN]->(:Signal {url: $url}) "
                "RETURN count(r) AS n",
                p=leader,
                url=SIGNAL_URL,
            )
            leader_count = (await leader_edges.single())["n"]

        await _cleanup(driver)
        await close_driver()
        return counts, rows, leader_count, leader, stranger

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    counts, rows, leader_count, leader, stranger = out

    assert counts == {"linked": 1, "flagged": 1}
    assert leader_count == 1  # idempotent — no duplicate edge on re-write
    by_name = {r["name"]: r for r in rows}
    assert by_name[leader]["flagged"] is False  # confident match, unflagged
    assert by_name[stranger]["flagged"] is True  # unknown name -> flagged
    assert by_name[stranger]["origin"] == "signal-capture"  # a stub, not a real person
