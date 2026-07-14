"""Graph integration for the person page + expertise summary (#42).

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

Verifies: the person-page read (identity + roles + linked-signals timeline), the
expertise store/read round-trip WITH its generation date + sources, and that the
job runner writes a cited advisory summary. The LLM is monkeypatched so nothing
hits Gemini. Fictional people/companies only (public-repo rule).
"""

import asyncio

import pytest

from app.capture.people import PersonMention, resolve_mention
from app.graph import person_expertise
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord, Leader, SignalRecord
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema
from app.graph.signals import (
    person_signal_candidates,
    upsert_signal,
    write_person_signal_links,
)

CO = "Nebula Expertise Co __pytest42__"
NAME = "Dana Placeholder __pytest42__"
SIG_URL = "https://acme.example/pytest42-post"
SIG2_URL = "https://conf.example/pytest42-talk"


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
    await session.run("MATCH (c:Company) WHERE c.name = $n DETACH DELETE c", n=CO)
    await session.run("MATCH (p:Person) WHERE p.name = $n DETACH DELETE p", n=NAME)
    await session.run(
        "MATCH (s:Signal) WHERE s.url IN $urls DETACH DELETE s", urls=[SIG_URL, SIG2_URL]
    )
    await session.run("MATCH (src:Source) WHERE src.url CONTAINS 'pytest42' DETACH DELETE src")


async def _seed(driver) -> str:
    """Seed a leader + two linked signals; return the person's elementId."""
    await apply_schema(driver)
    async with driver.session() as session:
        await _cleanup(session)
    await upsert_company(
        driver, CompanyRecord(name=CO, leadership=[Leader(name=NAME, title="Head of Research")])
    )
    # Two signals authored/spoken by the leader, linked via the #41 write path.
    for url, title, kind, relation in (
        (SIG_URL, "Scaling graph databases", "blog", "AUTHORED"),
        (SIG2_URL, "Keynote on agents", "event", "SPOKE_AT"),
    ):
        await upsert_signal(
            driver, SignalRecord(url=url, title=title, kind=kind, source=url), companies=[CO]
        )
        candidates = await person_signal_candidates(driver, name=NAME, company=CO)
        link = resolve_mention(
            PersonMention(name=NAME, relation=relation),
            linkedin_eids=[],
            name_company_eids=candidates["name_company_eids"],
        )
        await write_person_signal_links(driver, url, [link], source=url)

    async with driver.session() as session:
        result = await session.run(
            "MATCH (p:Person {name: $n})-[:LEADS]->(:Company {name: $c}) RETURN elementId(p) AS eid",
            n=NAME,
            c=CO,
        )
        record = await result.single()
    return record["eid"]


def test_person_page_read_has_roles_and_signals(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        eid = await _seed(driver)
        person = await person_expertise.get_person(driver, eid)
        missing = await person_expertise.get_person(driver, "4:does-not-exist:999")
        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return person, missing

    person, missing = event_loop.run_until_complete(scenario())
    assert missing is None
    assert person["name"] == NAME
    assert person["expertise"] is None  # nothing generated yet
    assert any(
        r["company"] == CO and r["title"] == "Head of Research" for r in person["currentRoles"]
    )
    relations = sorted(s["relation"] for s in person["signals"])
    assert relations == ["AUTHORED", "SPOKE_AT"]


def test_expertise_job_writes_cited_summary(event_loop, monkeypatch):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    # No network: force the deterministic fallback rendering by failing the LLM.
    async def _boom(*args, **kwargs):
        raise RuntimeError("no LLM in tests")

    monkeypatch.setattr(person_expertise, "generate_with_retry", _boom)

    async def scenario():
        driver = get_driver()
        eid = await _seed(driver)
        enq = await person_expertise.enqueue_person_expertise(eid)
        await person_expertise.run_person_expertise_job(enq["job_id"])
        person = await person_expertise.get_person(driver, eid)
        job = await person_expertise.jobs.get_job(enq["job_id"])
        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return person, job

    person, job = event_loop.run_until_complete(scenario())
    assert job["status"] == "done"
    exp = person["expertise"]
    assert exp is not None
    assert exp["summary"]  # non-empty advisory text (fallback rendering)
    assert exp["generatedAt"]  # stored with a generation date
    # Every summary carries the ACTUAL signals it drew from (http(s) sources).
    assert sorted(exp["sources"]) == sorted([SIG_URL, SIG2_URL])


def test_enqueue_unknown_person_errors(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        result = await person_expertise.enqueue_person_expertise("4:nope:404")
        await close_driver()
        return result

    result = event_loop.run_until_complete(scenario())
    assert "error" in result
