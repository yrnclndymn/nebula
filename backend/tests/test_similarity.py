"""Integration test for the explainable similarity search (issue #32).

Seeds a small graph of researched companies that share clients / partners / topics
with a focus company (plus shared kind / country), then checks the overlap
components, the weighted score, deterministic ordering, and the junk + kind='client'
+ un-researched exclusions.

Skips when Neo4j is unreachable so `make test` stays green without a database; CI
(with its own Neo4j service) is the arbiter. Uses fictional company names only.
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.entity_resolution import flag_junk
from app.graph.models import CompanyRecord
from app.graph.queries import similar_companies
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

T1 = "__pytest_sim_topic_1__"
T2 = "__pytest_sim_topic_2__"

# Focus company: isv in "Freedonia", clients {C1, C2}, partner {P1}, topics {T1}.
XERITH = "Xerith Sim __pytest__"

# Candidates (all researched -> TAGGED_AS a topic).
ALPHA = "Alpha Sim __pytest__"  # shares everything -> score 13
BRAVO = "Bravo Sim __pytest__"  # 1 shared client only -> score 3
DELTA = "Delta Sim __pytest__"  # same kind + same country only -> score 2
GAMMA = "Gamma Sim __pytest__"  # 1 shared topic only -> score 2

# Excluded candidates (each would otherwise score high).
JUNKER = "Junker Sim __pytest__"  # junk-flagged
CLIENTY = "Clienty Sim __pytest__"  # kind='client'
STUBBY = "Stubby Sim __pytest__"  # un-researched (no topic)

# Shared neighbour stubs (never researched, so never candidates themselves).
C1 = "Customer One Sim __pytest__"
C2 = "Customer Two Sim __pytest__"
P1 = "Partner One Sim __pytest__"

RESEARCHED = [XERITH, ALPHA, BRAVO, DELTA, GAMMA, JUNKER, CLIENTY, STUBBY]
ALL_NAMES = RESEARCHED + [C1, C2, P1]


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


def test_similar_companies(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-up`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)

        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=ALL_NAMES
            )

        for rec in (
            CompanyRecord(
                name=XERITH, topics=[T1], kind="isv", clients=[C1, C2], partnerships=[P1]
            ),
            # Shares 2 clients, 1 partner, 1 topic, same kind, same country -> 13.
            CompanyRecord(name=ALPHA, topics=[T1], kind="isv", clients=[C1, C2], partnerships=[P1]),
            # 1 shared client, nothing else -> 3.
            CompanyRecord(name=BRAVO, topics=[T2], kind="service_provider", clients=[C1]),
            # Same kind + same country only -> 2.
            CompanyRecord(name=DELTA, topics=[T2], kind="isv"),
            # 1 shared topic only -> 2.
            CompanyRecord(name=GAMMA, topics=[T1], kind="service_provider"),
            # Would score ~13 but junk-flagged below -> excluded.
            CompanyRecord(
                name=JUNKER, topics=[T1], kind="isv", clients=[C1, C2], partnerships=[P1]
            ),
            # Would score high but kind='client' -> excluded.
            CompanyRecord(
                name=CLIENTY, topics=[T1], kind="client", clients=[C1, C2], partnerships=[P1]
            ),
            # Shares clients but has no topic (un-researched) -> excluded.
            CompanyRecord(name=STUBBY, kind="isv", clients=[C1, C2]),
        ):
            await upsert_company(driver, rec)

        # Countries: Xerith / Alpha / Delta in Freedonia (same_country hits);
        # everyone else elsewhere. Set straight on the node (hqCountry isn't a
        # CompanyRecord field). Junk flag applied via entity resolution.
        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names SET c.hqCountry = 'Freedonia'",
                names=[XERITH, ALPHA, DELTA, JUNKER, CLIENTY],
            )
        await flag_junk(driver, [JUNKER])

        results = await similar_companies(driver, XERITH, limit=5)
        top2 = await similar_companies(driver, XERITH, limit=2)
        unknown = await similar_companies(driver, "No Such Company __pytest__")

        async with driver.session() as session:
            await session.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=ALL_NAMES
            )
            await session.run(
                "MATCH (t:Topic) WHERE t.name IN $names DETACH DELETE t", names=[T1, T2]
            )
        await close_driver()
        return results, top2, unknown

    results, top2, unknown = event_loop.run_until_complete(scenario())

    # Unknown company -> None (route turns this into a 404).
    assert unknown is None

    by_name = {row["name"]: row for row in results}

    # Exclusions: junk, kind='client', and un-researched never appear.
    assert JUNKER not in by_name
    assert CLIENTY not in by_name
    assert STUBBY not in by_name
    # The focus company is never similar to itself.
    assert XERITH not in by_name
    # Shared-neighbour stubs (clients/partners) aren't researched -> not candidates.
    assert C1 not in by_name and C2 not in by_name and P1 not in by_name

    # Alpha overlaps on every component.
    alpha = by_name[ALPHA]
    assert alpha["shared_clients"] == 2
    assert alpha["shared_partners"] == 1
    assert alpha["shared_topics"] == 1
    assert alpha["same_kind"] is True
    assert alpha["same_country"] is True
    assert alpha["score"] == 13  # 3*2 + 3*1 + 2*1 + 1 + 1

    # Bravo: a single shared client, nothing else.
    bravo = by_name[BRAVO]
    assert bravo["shared_clients"] == 1
    assert bravo["shared_partners"] == 0
    assert bravo["shared_topics"] == 0
    assert bravo["same_kind"] is False
    assert bravo["same_country"] is False
    assert bravo["score"] == 3

    # Delta: same kind + same country only.
    delta = by_name[DELTA]
    assert (delta["shared_clients"], delta["shared_partners"], delta["shared_topics"]) == (0, 0, 0)
    assert delta["same_kind"] is True and delta["same_country"] is True
    assert delta["score"] == 2

    # Gamma: one shared topic only.
    gamma = by_name[GAMMA]
    assert gamma["shared_topics"] == 1
    assert gamma["same_kind"] is False and gamma["same_country"] is False
    assert gamma["score"] == 2

    # Deterministic ordering: score desc, then name asc (Delta before Gamma on the tie).
    names = [row["name"] for row in results]
    assert names == [ALPHA, BRAVO, DELTA, GAMMA]

    # The cap is honoured: top-2 is the same leading slice.
    assert [row["name"] for row in top2] == [ALPHA, BRAVO]
