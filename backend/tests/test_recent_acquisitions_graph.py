"""Graph integration for the space-level M&A feed (#45): `recent_acquisitions`.

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

Verifies the read-only feed powering the M&A page: newest-announced-first ordering,
the topic filter (either endpoint TAGGED_AS the topic), the acquirer filter, and
that provenance (`source`/`amount_source`) rides along so amounts can be shown with
a citation. Fictional companies only (public-repo rule).
"""

import asyncio

import pytest

from app.graph.acquisitions import recent_acquisitions, upsert_acquisitions
from app.graph.deal_models import AcquisitionRecord, Deal
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

TOPIC = "Nebula Space __pytest45__"
OTHER_TOPIC = "Nebula Other __pytest45__"
ACQ_A = "Nebula Buyer A __pytest45__"
ACQ_B = "Nebula Buyer B __pytest45__"
TGT_A = "Nebula Target A __pytest45__"
TGT_B = "Nebula Target B __pytest45__"
TGT_C = "Nebula Target C __pytest45__"  # receives the UNDATED deal
SRC = "https://news.example/pytest45-deal"
AMT_SRC = "https://filings.example/pytest45-terms"

_NAMES = [ACQ_A, ACQ_B, TGT_A, TGT_B, TGT_C]


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
    await session.run("MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=_NAMES)
    await session.run(
        "MATCH (t:Topic) WHERE t.name IN $names DETACH DELETE t", names=[TOPIC, OTHER_TOPIC]
    )


def test_recent_feed_orders_filters_and_carries_provenance(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        # Buyer A is in TOPIC; buyer B is in OTHER_TOPIC. Both make one dated deal.
        await upsert_company(driver, CompanyRecord(name=ACQ_A, topics=[TOPIC]))
        await upsert_company(driver, CompanyRecord(name=ACQ_B, topics=[OTHER_TOPIC]))
        await upsert_acquisitions(
            driver,
            AcquisitionRecord(
                company=ACQ_A,
                deals=[
                    Deal(
                        acquirer=ACQ_A,
                        target=TGT_A,
                        announced_at="2024-01-01",
                        amount="$100M",
                        currency="USD",
                        source=SRC,
                        amount_source=AMT_SRC,
                    )
                ],
            ),
        )
        await upsert_acquisitions(
            driver,
            AcquisitionRecord(
                company=ACQ_B,
                deals=[
                    Deal(
                        acquirer=ACQ_B,
                        target=TGT_B,
                        announced_at="2024-06-01",
                        source=SRC,
                    )
                ],
            ),
        )

        # An UNDATED deal (announced_at absent) must sort LAST, not first —
        # Cypher nulls are largest, a bare DESC floated it to the top (PR #118).
        await upsert_acquisitions(
            driver,
            AcquisitionRecord(
                company=ACQ_A,
                deals=[Deal(acquirer=ACQ_A, target=TGT_C, source=SRC)],
            ),
        )

        all_deals = await recent_acquisitions(driver, limit=50)
        by_topic = await recent_acquisitions(driver, limit=50, topic=TOPIC)
        by_acquirer = await recent_acquisitions(driver, limit=50, acquirer=ACQ_B)
        # Partial + differently-cased input must still match (live filter box —
        # exact equality regressed this; PR #118 review): use a mid-name slice.
        by_partial = await recent_acquisitions(driver, limit=50, acquirer=ACQ_B[2:14].upper())

        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return all_deals, by_topic, by_acquirer, by_partial

    all_deals, by_topic, by_acquirer, by_partial = event_loop.run_until_complete(scenario())

    # Narrow to our fixtures (a shared DB may hold other edges).
    ours = [d for d in all_deals if d["acquirer"] in (ACQ_A, ACQ_B)]
    assert {(d["acquirer"], d["target"]) for d in ours} == {
        (ACQ_A, TGT_A),
        (ACQ_B, TGT_B),
        (ACQ_A, TGT_C),
    }
    # Newest announced first: B (2024-06) precedes A (2024-01); the UNDATED deal
    # sorts last, never first (the null-ordering regression).
    order = [d["target"] for d in ours]
    assert order.index(TGT_B) < order.index(TGT_A) < order.index(TGT_C)

    # Topic filter: only the buyer-A deal (buyer A is TAGGED_AS TOPIC).
    topic_pairs = {(d["acquirer"], d["target"]) for d in by_topic}
    assert (ACQ_A, TGT_A) in topic_pairs
    assert (ACQ_B, TGT_B) not in topic_pairs

    # Acquirer filter narrows to that buyer's deals.
    assert {(d["acquirer"], d["target"]) for d in by_acquirer} == {(ACQ_B, TGT_B)}
    # Case-insensitive substring works too (live filter box types partials).
    assert {(d["acquirer"], d["target"]) for d in by_partial} == {(ACQ_B, TGT_B)}

    # Provenance rides along: the cited amount carries its amount_source.
    deal_a = next(d for d in ours if d["acquirer"] == ACQ_A)
    assert deal_a["amount"] == "$100M"
    assert deal_a["source"] == SRC and deal_a["amount_source"] == AMT_SRC
