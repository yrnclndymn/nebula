"""Graph integration for the thesis evidence loop's reads (#196):
`gather_acquisition_evidence` and `last_committed_revision_at`.

Skips when Neo4j is unreachable, so `make test` stays green without a database (CI's
Neo4j service is the arbiter). Run locally with an ephemeral DB: `make db-ephemeral`
then `NEO4J_URI=... make test`.

Verifies: the evidence read returns both endpoints' kinds/headcounts + the deal's
cited thesis/source with a synthetic deal_id, the `since` filter narrows to deals
written after a cutoff, and the last-committed-revision read reflects committed
`thesis_revision` jobs. Fictional company names + example.com URLs only.
"""

import asyncio
import json

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.schema import apply_schema
from app.graph.thesis import gather_acquisition_evidence, last_committed_revision_at

TAG = "__pytest196__"
ACQ = f"Acme Cloud {TAG}"
TGT = f"Globex Consulting {TAG}"
SRC = "https://news.example/pytest196-deal"


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
    await session.run(f"MATCH (c:Company) WHERE c.name CONTAINS '{TAG}' DETACH DELETE c")
    await session.run(f"MATCH (j:Job) WHERE j.id STARTS WITH '{TAG}' DETACH DELETE j")


def test_gather_evidence_returns_kinds_sources_and_deal_ids(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
            await session.run(
                """
                CREATE (a:Company {name: $acq, kind: 'cloud_provider', headcount: 5000})
                CREATE (t:Company {name: $tgt, kind: 'service_provider', headcount: 200})
                CREATE (a)-[:ACQUIRED {announcedAt: '2026-05-01', thesis: 'Delivery capacity.',
                                       source: $src, updatedAt: datetime()}]->(t)
                """,
                acq=ACQ,
                tgt=TGT,
                src=SRC,
            )
        evidence = await gather_acquisition_evidence(driver)
        mine = [e for e in evidence if e["acquirer"] == ACQ]
        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return mine

    mine = event_loop.run_until_complete(scenario())
    assert len(mine) == 1
    deal = mine[0]
    assert deal["acquirer_kind"] == "cloud_provider"
    assert deal["target_kind"] == "service_provider"
    assert deal["acquirer_headcount"] == 5000
    assert deal["target_headcount"] == 200
    assert deal["thesis"] == "Delivery capacity."
    assert deal["source"] == SRC
    assert deal["deal_id"].startswith("d")  # synthetic id for LLM referencing


def test_since_filter_and_last_committed_revision(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
            # A deal written "in the past" relative to the cutoff below.
            await session.run(
                """
                CREATE (a:Company {name: $acq, kind: 'cloud_provider'})
                CREATE (t:Company {name: $tgt, kind: 'service_provider'})
                CREATE (a)-[:ACQUIRED {announcedAt: '2020-01-01', source: $src,
                                       updatedAt: datetime('2020-01-01T00:00:00Z')}]->(t)
                """,
                acq=ACQ,
                tgt=TGT,
                src=SRC,
            )

        no_since = await gather_acquisition_evidence(driver)
        # A cutoff after the deal's updatedAt filters it out.
        filtered = await gather_acquisition_evidence(driver, since="2021-01-01T00:00:00Z")

        # No committed thesis_revision job yet → None; then create one.
        before = await last_committed_revision_at(driver)
        job_id = f"{TAG}-job"
        async with driver.session() as session:
            await session.run(
                "CREATE (j:Job {id: $id, type: 'thesis_revision', status: 'committed', "
                "dataJson: $data, createdAt: datetime()})",
                id=job_id,
                data=json.dumps({"job_id": job_id}),
            )
        after = await last_committed_revision_at(driver)

        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return no_since, filtered, before, after

    no_since, filtered, before, after = event_loop.run_until_complete(scenario())
    assert any(e["acquirer"] == ACQ for e in no_since)  # visible without a cutoff
    assert all(e["acquirer"] != ACQ for e in filtered)  # filtered out by the since cutoff
    assert before is None  # no committed revision before we made one
    assert after is not None  # the committed job's createdAt is now the cutoff
