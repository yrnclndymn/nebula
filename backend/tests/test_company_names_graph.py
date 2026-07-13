"""Graph integration test for the live tracked-name list (#104).

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

Fictional names only (public-repo rule) — verifies name + alias inclusion and
that junk-flagged stubs are excluded.
"""

import asyncio

import pytest

from app.graph.company_names import list_company_names
from app.graph.driver import check_connectivity, get_driver
from app.graph.entity_resolution import add_aliases
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

CO = "Globex Test Co __pytest104__"
ALIAS = "Globex Holdings __pytest104__"
JUNK = "Read More __pytest104__"


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
        "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c",
        names=[CO, JUNK],
    )


def test_list_includes_names_and_aliases_excludes_junk(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        await upsert_company(driver, CompanyRecord(name=CO))
        await add_aliases(driver, CO, [ALIAS])
        # A junk-flagged stub must not appear in the sensor's list.
        async with driver.session() as session:
            await session.run("MERGE (c:Company {name: $name}) SET c.junk = true", name=JUNK)

        names = await list_company_names(driver)

        async with driver.session() as session:
            await _cleanup(session)
        return names

    names = event_loop.run_until_complete(scenario())
    assert CO in names
    assert ALIAS in names
    assert JUNK not in names
