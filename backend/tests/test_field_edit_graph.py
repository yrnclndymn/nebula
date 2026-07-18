"""Integration test for the user field-edit write (#149). Skips when Neo4j is
unreachable, so `make test` stays green without a database (CI's Neo4j service is
the arbiter). Run locally with `make db-ephemeral` (export NEO4J_URI) or `make
db-up`. Fictional names only (public repo)."""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.field_edit import ValidatedEdit, apply_field_edit
from app.graph.schema import apply_schema

TEST_COMPANY = "Nebula FieldEdit Co __pytest__"


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


def test_apply_field_edit_writes_property_edge_and_marker(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral` or `make db-up`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await session.run("MERGE (c:Company {name: $name})", name=TEST_COMPANY)

        # headcount with a source: property + CITES(origin='user') + userEdited
        ok = await apply_field_edit(
            driver,
            TEST_COMPANY,
            ValidatedEdit(field="headcount", value=250, source_url="https://acme.example/about"),
        )
        assert ok is True

        # yearFounded without a source: property + userEdited, no new edge
        ok = await apply_field_edit(
            driver,
            TEST_COMPANY,
            ValidatedEdit(field="yearFounded", value=1994, source_url=None),
        )
        assert ok is True

        async with driver.session() as session:
            row = await (
                await session.run(
                    "MATCH (c:Company {name: $name}) "
                    "OPTIONAL MATCH (c)-[r:CITES {field: 'headcount'}]->(s:Source) "
                    "RETURN c.headcount AS headcount, c.yearFounded AS yearFounded, "
                    "       c.userEdited AS userEdited, r.origin AS origin, "
                    "       r.value AS citeValue, s.url AS source",
                    name=TEST_COMPANY,
                )
            ).single()
        assert row["headcount"] == 250
        assert row["yearFounded"] == 1994
        assert row["origin"] == "user"
        assert row["citeValue"] == "250"
        assert row["source"] == "https://acme.example/about"
        assert set(row["userEdited"]) == {"headcount", "yearFounded"}

        # re-editing the same field must not duplicate it in userEdited, and the
        # NEW source must REPLACE the field's prior CITES edge, not stack a second
        # one for the detail read to show alongside it (PR #160 review).
        await apply_field_edit(
            driver,
            TEST_COMPANY,
            ValidatedEdit(field="headcount", value=300, source_url="https://acme.example/team"),
        )
        async with driver.session() as session:
            edited = await (
                await session.run(
                    "MATCH (c:Company {name: $name}) "
                    "OPTIONAL MATCH (c)-[r:CITES {field: 'headcount'}]->(s:Source) "
                    "RETURN c.userEdited AS e, count(r) AS edges, collect(s.url) AS sources",
                    name=TEST_COMPANY,
                )
            ).single()
        assert sorted(edited["e"]) == ["headcount", "yearFounded"]
        assert edited["edges"] == 1
        assert edited["sources"] == ["https://acme.example/team"]

        # unknown company → False (route maps to 404)
        missing = await apply_field_edit(
            driver,
            "No Such Co __pytest__",
            ValidatedEdit(field="yearFounded", value=2000, source_url=None),
        )
        assert missing is False

        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company {name: $name}) "
                "OPTIONAL MATCH (c)-[:CITES]->(s:Source) DETACH DELETE c, s",
                name=TEST_COMPANY,
            )

    try:
        event_loop.run_until_complete(scenario())
    finally:
        # The module-global driver was created on THIS loop; close it so later
        # test files (e.g. test_health's TestClient) build a fresh one on their
        # own loop instead of hitting "Event loop is closed" (same pattern as
        # test_cache_sanitize's round-trip test).
        event_loop.run_until_complete(close_driver())
