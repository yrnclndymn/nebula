"""Integration test for the graph-view neighbourhood query (issue #50).

Skips when Neo4j is unreachable so `make test` stays green without a database;
CI (with its own Neo4j service) is the arbiter. Uses fictional company names.
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord, Leader
from app.graph.queries import company_neighbourhood
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

CENTER = "Acme Graph __pytest__"
PARTNER = "Globex Graph __pytest__"
CLIENT = "Initech Graph __pytest__"
TOPIC = "__pytest_graph_topic__"
CTYPE = "__pytest_graph_type__"
PERSON = "Ada Graph __pytest__"


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


def test_company_neighbourhood(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-up`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        await upsert_company(
            driver,
            CompanyRecord(
                name=CENTER,
                topics=[TOPIC],
                company_types=[CTYPE],
                partnerships=[PARTNER],
                clients=[CLIENT],
                leadership=[Leader(name=PERSON, title="CEO")],
            ),
        )
        result = await company_neighbourhood(driver, CENTER)
        missing = await company_neighbourhood(driver, "no-such-company __pytest__")

        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c",
                names=[CENTER, PARTNER, CLIENT],
            )
            await session.run("MATCH (p:Person {name:$n}) DETACH DELETE p", n=PERSON)
            await session.run("MATCH (t:Topic {name:$n}) DETACH DELETE t", n=TOPIC)
            await session.run("MATCH (ct:CompanyType {name:$n}) DETACH DELETE ct", n=CTYPE)
        await close_driver()
        return result, missing

    result, missing = event_loop.run_until_complete(scenario())

    assert missing is None
    assert result is not None
    assert result["center"] == f"Company:{CENTER}"

    nodes = {n["id"]: n for n in result["nodes"]}
    for node_id in (
        f"Company:{CENTER}",
        f"Company:{PARTNER}",
        f"Company:{CLIENT}",
        f"Topic:{TOPIC}",
        f"CompanyType:{CTYPE}",
        f"Person:{PERSON}",
    ):
        assert node_id in nodes, node_id

    # The seed company is researched (tagged to a topic); pulled-in stubs are not.
    assert nodes[f"Company:{CENTER}"]["researched"] is True
    assert nodes[f"Company:{PARTNER}"]["researched"] is False
    assert nodes[f"Person:{PERSON}"]["kind"] == "Person"

    edges = {(e["source"], e["target"], e["type"]) for e in result["edges"]}
    assert (f"Company:{CENTER}", f"Company:{CLIENT}", "HAS_CLIENT") in edges
    assert (f"Company:{CENTER}", f"Company:{PARTNER}", "PARTNERS_WITH") in edges
    assert (f"Company:{CENTER}", f"Topic:{TOPIC}", "TAGGED_AS") in edges
    assert (f"Company:{CENTER}", f"CompanyType:{CTYPE}", "CLASSIFIED_AS") in edges
    # LEADS points from the person into the company.
    assert (f"Person:{PERSON}", f"Company:{CENTER}", "LEADS") in edges
