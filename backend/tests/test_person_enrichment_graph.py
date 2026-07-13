"""Graph integration for person enrichment (#40): the commit write path.

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

Verifies the reviewed-commit write: current title onto the LEADS edge, bio/links
onto the node, prior roles as HELD_ROLE edges (unknown employer MERGE'd as a stub),
LinkedIn attached as the canonical identity, and provenance as (Person)-[:CITES]->
(Source). Fictional people/companies only (public-repo rule).
"""

import asyncio

import pytest

from app.graph.person_models import PersonCitation, PersonRecord, PriorRole
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord, Leader
from app.graph.person_enrichment import get_person_scoped, upsert_person
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

CO = "Nebula Test Co __pytest40__"
PRIOR_CO = "Nebula Prior Co __pytest40__"
NAME = "Jane Placeholder __pytest40__"
SLUG = "jane-placeholder-pytest40graph"
CANON = f"https://www.linkedin.com/in/{SLUG}"


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _neo4j_available() -> bool:
    try:
        await check_connectivity()
        return True
    except Exception:
        return False


async def _cleanup(session) -> None:
    await session.run(
        "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=[CO, PRIOR_CO]
    )
    await session.run(
        "MATCH (p:Person) WHERE p.name = $name OR p.linkedin CONTAINS $slug DETACH DELETE p",
        name=NAME,
        slug=SLUG,
    )
    await session.run("MATCH (s:Source) WHERE s.url CONTAINS 'pytest40' DETACH DELETE s")


def test_commit_writes_profile_history_links_and_citations(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        # Seed: a name-only leader of the scoping company (as company enrichment
        # would have produced) — no LinkedIn yet, no bio.
        await upsert_company(
            driver,
            CompanyRecord(name=CO, leadership=[Leader(name=NAME, title="Chief Exec")]),
        )

        record = PersonRecord(
            name=NAME,
            company=CO,
            title="CEO",  # updates the LEADS edge title
            bio="Leads the company.",
            linkedin=CANON,
            personal_site="https://jane.example",
            talks=["https://conf.example/talk"],
            prior_roles=[
                PriorRole(
                    company=PRIOR_CO,
                    title="VP Eng",
                    from_year=2015,
                    to_year=2020,
                    source="https://prior.example/pytest40",
                )
            ],
            citations=[
                PersonCitation(field="title", value="CEO", source="https://co.example/pytest40"),
                PersonCitation(field="bio", value="Leads", source="https://co.example/pytest40"),
                PersonCitation(field="linkedin", value=CANON, source="https://co.example/pytest40"),
            ],
        )
        action = await upsert_person(driver, record)

        snapshot = await get_person_scoped(driver, NAME, CO)

        async with driver.session() as session:
            res = await session.run(
                """
                MATCH (p:Person {linkedin: $canon})
                OPTIONAL MATCH (p)-[:CITES]->(s:Source)
                OPTIONAL MATCH (p)-[hr:HELD_ROLE]->(pc:Company)
                RETURN p.bio AS bio, p.personalSite AS site, p.talks AS talks,
                       count(DISTINCT s) AS sources,
                       collect(DISTINCT {co: pc.name, title: hr.title, from: hr.from}) AS roles
                """,
                canon=CANON,
            )
            row = dict(await res.single())
            # The person is now keyed on the canonical URL (identity attached).
            res = await session.run(
                "MATCH (p:Person {linkedin: $canon})-[l:LEADS]->(:Company {name: $co}) "
                "RETURN l.title AS title",
                canon=CANON,
                co=CO,
            )
            leads = await res.single()
            await _cleanup(session)
        await close_driver()
        return action, snapshot, row, leads

    action, snapshot, row, leads = event_loop.run_until_complete(scenario())
    assert action["action"] == "written"
    assert row["bio"] == "Leads the company."
    assert row["site"] == "https://jane.example"
    assert row["talks"] == ["https://conf.example/talk"]
    assert row["sources"] == 1  # all three citations share one Source URL
    assert leads is not None and leads["title"] == "CEO"  # LEADS title updated
    roles = [r for r in row["roles"] if r["co"]]
    assert len(roles) == 1 and roles[0]["co"] == PRIOR_CO and roles[0]["title"] == "VP Eng"
    # The read-back snapshot reflects the committed facts (review/diff surface).
    assert snapshot["bio"] == "Leads the company."
    assert snapshot["linkedin"] == CANON
    assert any(r["company"] == PRIOR_CO for r in snapshot["prior_roles"])


def test_commit_is_idempotent(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
        await upsert_company(
            driver,
            CompanyRecord(name=CO, leadership=[Leader(name=NAME, title="Chief Exec")]),
        )
        record = PersonRecord(
            name=NAME,
            company=CO,
            bio="Leads the company.",
            prior_roles=[
                PriorRole(company=PRIOR_CO, title="VP Eng", source="https://prior.example/pytest40")
            ],
            citations=[
                PersonCitation(field="bio", value="Leads", source="https://co.example/pytest40")
            ],
        )
        await upsert_person(driver, record)
        await upsert_person(driver, record)  # second commit must not duplicate

        async with driver.session() as session:
            res = await session.run(
                """
                MATCH (p:Person {name: $name})-[:LEADS]->(:Company {name: $co})
                OPTIONAL MATCH (p)-[hr:HELD_ROLE]->(:Company)
                OPTIONAL MATCH (p)-[cit:CITES]->(:Source)
                RETURN count(DISTINCT p) AS people, count(DISTINCT hr) AS roles,
                       count(DISTINCT cit) AS cites
                """,
                name=NAME,
                co=CO,
            )
            counts = dict(await res.single())
            await _cleanup(session)
        await close_driver()
        return counts

    counts = event_loop.run_until_complete(scenario())
    assert counts["people"] == 1
    assert counts["roles"] == 1  # HELD_ROLE MERGE'd, not duplicated
    assert counts["cites"] == 1  # CITES MERGE'd on (field, source)


def test_propose_refuses_unknown_person(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        from app.agents.people.proposals import propose_person

        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
        result = await propose_person("Nobody __pytest40__", CO)
        await close_driver()
        return result

    result = event_loop.run_until_complete(scenario())
    assert "error" in result  # can't enrich a person who leads no such company
