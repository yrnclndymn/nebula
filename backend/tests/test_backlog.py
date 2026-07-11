"""Integration test for the ranked research backlog query (issue #30).

Seeds a small graph of researched companies that reference un-researched stubs via
HAS_CLIENT / PARTNERS_WITH, then checks the backlog ranking, the cloud/isv partner
boost, and the junk + kind='client' exclusions.

Skips when Neo4j is unreachable so `make test` stays green without a database; CI
(with its own Neo4j service) is the arbiter. Uses fictional company names only.
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.entity_resolution import flag_junk
from app.graph.models import CompanyRecord
from app.graph.queries import research_backlog
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

TOPIC = "__pytest_backlog_topic__"

# Researched mentioners (each tagged to TOPIC -> counts as "researched").
ACME = "Acme Backlog __pytest__"  # cloud_provider
GLOBEX = "Globex Backlog __pytest__"  # isv
INITECH = "Initech Backlog __pytest__"  # service_provider
UMBRELLA = "Umbrella Backlog __pytest__"  # service_provider
WAYNE = "Wayne Backlog __pytest__"  # service_provider
STARK = "Stark Backlog __pytest__"  # service_provider
MENTIONERS = [ACME, GLOBEX, INITECH, UMBRELLA, WAYNE, STARK]

# Un-researched stubs (created implicitly as bare :Company nodes when referenced).
STUB_ALPHA = "Alpha Stub __pytest__"  # 2 partner (cloud+isv) + 1 client -> score 7
STUB_BETA = "Beta Stub __pytest__"  # 2 client + 1 partner -> score 3
STUB_JUNK = "Junk Stub __pytest__"  # junk-flagged -> excluded
STUB_CLIENT = "Client Stub __pytest__"  # kind='client' -> excluded
STUBS = [STUB_ALPHA, STUB_BETA, STUB_JUNK, STUB_CLIENT]

ALL_NAMES = MENTIONERS + STUBS


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


def test_research_backlog(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-up`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)

        # Clear any leftovers from a prior aborted run so counts are exact.
        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=ALL_NAMES
            )

        # Researched mentioners referencing the stubs. Enrichment only writes edges
        # OUT of the company being researched, so these become inbound mentions on
        # the (un-researched) stubs.
        for rec in (
            CompanyRecord(
                name=ACME,
                topics=[TOPIC],
                kind="cloud_provider",
                partnerships=[STUB_ALPHA],
                clients=[STUB_JUNK, STUB_CLIENT],
            ),
            CompanyRecord(name=GLOBEX, topics=[TOPIC], kind="isv", partnerships=[STUB_ALPHA]),
            CompanyRecord(
                name=INITECH, topics=[TOPIC], kind="service_provider", clients=[STUB_ALPHA]
            ),
            CompanyRecord(
                name=UMBRELLA, topics=[TOPIC], kind="service_provider", clients=[STUB_BETA]
            ),
            CompanyRecord(name=WAYNE, topics=[TOPIC], kind="service_provider", clients=[STUB_BETA]),
            CompanyRecord(
                name=STARK, topics=[TOPIC], kind="service_provider", partnerships=[STUB_BETA]
            ),
        ):
            await upsert_company(driver, rec)

        # Apply the two exclusions: junk flag (entity resolution) and the end-customer
        # kind='client' classification (story #57) set straight on the stub node.
        await flag_junk(driver, [STUB_JUNK])
        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company {name: $name}) SET c.kind = 'client'", name=STUB_CLIENT
            )

        full = await research_backlog(driver, limit=1000, offset=0)
        page_first = await research_backlog(driver, limit=1, offset=0)
        page_rest = await research_backlog(driver, limit=2, offset=1)

        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=ALL_NAMES
            )
            await session.run("MATCH (t:Topic {name: $n}) DETACH DELETE t", n=TOPIC)
        await close_driver()
        return full, page_first, page_rest

    full, page_first, page_rest = event_loop.run_until_complete(scenario())

    by_name = {row["name"]: row for row in full}

    # Exclusions: junk and kind='client' stubs never appear.
    assert STUB_JUNK not in by_name
    assert STUB_CLIENT not in by_name

    # Both real stubs surface.
    assert STUB_ALPHA in by_name
    assert STUB_BETA in by_name

    # Alpha: 1 client (Initech) + 2 partners (Acme cloud, Globex isv), both boosted.
    alpha = by_name[STUB_ALPHA]
    assert alpha["mention_count"] == 3
    assert alpha["client_mentions"] == 1
    assert alpha["partner_mentions"] == 2
    assert alpha["cloud_isv_partner_mentions"] == 2
    assert alpha["rank_score"] == 7  # 1 + 2 + 2*2

    # Beta: 2 clients (Umbrella, Wayne) + 1 partner (Stark, service_provider — no boost).
    beta = by_name[STUB_BETA]
    assert beta["mention_count"] == 3
    assert beta["client_mentions"] == 2
    assert beta["partner_mentions"] == 1
    assert beta["cloud_isv_partner_mentions"] == 0
    assert beta["rank_score"] == 3  # 2 + 1 + 0

    # Ranking: the boosted stub outranks the equally-mentioned but unboosted one.
    names = [row["name"] for row in full]
    assert names.index(STUB_ALPHA) < names.index(STUB_BETA)

    # Pagination is a deterministic slice of the same total order.
    assert page_first == full[:1]
    assert page_rest == full[1:3]
