"""Integration test for the graph upsert. Skips when Neo4j is unreachable, so
`make test` stays green without a database (e.g. in CI). Run locally with
`make db-up` first to exercise it.
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord, Leader
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

TEST_COMPANY = "Nebula Test Co __pytest__"
TEST_PARTNER = "Nebula Test Partner __pytest__"


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


def test_upsert_company_roundtrip(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-up`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        record = CompanyRecord(
            name=TEST_COMPANY,
            headcount=42,
            topics=["AI-native engineering"],
            company_types=["B-Corp"],
            partnerships=[TEST_PARTNER],
            leadership=[Leader(name="Ada Test", title="CEO")],
        )
        await upsert_company(driver, record)

        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Company {name: $name})
                OPTIONAL MATCH (c)-[:PARTNERS_WITH]->(p:Company)
                OPTIONAL MATCH (c)-[:TAGGED_AS]->(t:Topic)
                OPTIONAL MATCH (person:Person)-[r:LEADS]->(c)
                RETURN c.headcount AS headcount, p.name AS partner,
                       t.name AS topic, person.name AS leader, r.title AS title
                """,
                name=TEST_COMPANY,
            )
            row = await result.single()

        # Clean up both nodes we created.
        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c",
                names=[TEST_COMPANY, TEST_PARTNER],
            )
            await session.run("MATCH (p:Person {name: 'Ada Test'}) DETACH DELETE p")

        await close_driver()
        return row

    row = event_loop.run_until_complete(scenario())
    assert row["headcount"] == 42
    assert row["partner"] == TEST_PARTNER
    assert row["topic"] == "AI-native engineering"
    assert row["leader"] == "Ada Test"
    assert row["title"] == "CEO"
