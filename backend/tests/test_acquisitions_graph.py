"""Graph integration for acquisitions (#43): the commit write path.

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

Verifies the reviewed-commit write: the ACQUIRED edge with its dates/amount/thesis
+ provenance, unknown counterparties MERGE'd as :Company stubs (origin 'agent', so
they feed the backlog), the read-back for the review/diff surface, and idempotence.
Fictional companies only (public-repo rule).
"""

import asyncio

import pytest

from app.agents.deals.models import AcquisitionRecord, Deal
from app.graph.acquisitions import get_acquisitions, upsert_acquisitions
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.graph.schema import apply_schema

ACQ = "Nebula Acquirer Co __pytest43__"
TGT = "Nebula Target Co __pytest43__"
SRC = "https://news.example/pytest43-deal"
AMT_SRC = "https://filings.example/pytest43-terms"


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
    await session.run("MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=[ACQ, TGT])


def test_commit_writes_edge_stub_and_provenance(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        # Seed only the acquirer as a tracked company; the target is unknown and
        # must MERGE as a stub (feeds the backlog).
        await upsert_company(driver, CompanyRecord(name=ACQ))

        record = AcquisitionRecord(
            company=ACQ,
            deals=[
                Deal(
                    acquirer=ACQ,
                    target=TGT,
                    announced_at="2024-02-01",
                    closed_at="2024-05-01",
                    amount="$1.2 billion",
                    currency="USD",
                    thesis="Expand the platform.",
                    source=SRC,
                    amount_source=AMT_SRC,
                )
            ],
        )
        action = await upsert_acquisitions(driver, record)
        snapshot = await get_acquisitions(driver, ACQ)

        async with driver.session() as session:
            res = await session.run(
                """
                MATCH (acq:Company {name: $acq})-[r:ACQUIRED]->(tgt:Company {name: $tgt})
                RETURN r.amount AS amount, r.currency AS currency, r.thesis AS thesis,
                       r.source AS source, r.amountSource AS amountSource,
                       r.announcedAt AS announced, tgt.origin AS targetOrigin
                """,
                acq=ACQ,
                tgt=TGT,
            )
            row = dict(await res.single())
            await _cleanup(session)
        await close_driver()
        return action, snapshot, row

    action, snapshot, row = event_loop.run_until_complete(scenario())
    assert action["action"] == "written" and action["deals"] == 1
    assert row["amount"] == "$1.2 billion" and row["currency"] == "USD"
    assert row["thesis"] == "Expand the platform."
    assert row["source"] == SRC and row["amountSource"] == AMT_SRC
    assert row["announced"] == "2024-02-01"
    assert row["targetOrigin"] == "agent"  # unknown target stubbed for the backlog
    # Read-back covers both directions and feeds the review/diff surface.
    assert any(d["acquirer"] == ACQ and d["target"] == TGT for d in snapshot)


def test_commit_is_idempotent(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)
        await upsert_company(driver, CompanyRecord(name=ACQ))
        record = AcquisitionRecord(
            company=ACQ,
            deals=[Deal(acquirer=ACQ, target=TGT, amount="$1B", source=SRC, amount_source=AMT_SRC)],
        )
        await upsert_acquisitions(driver, record)
        await upsert_acquisitions(driver, record)  # second commit must not duplicate

        async with driver.session() as session:
            res = await session.run(
                """
                MATCH (acq:Company {name: $acq})-[r:ACQUIRED]->(tgt:Company {name: $tgt})
                RETURN count(r) AS edges, count(DISTINCT tgt) AS targets
                """,
                acq=ACQ,
                tgt=TGT,
            )
            counts = dict(await res.single())
            await _cleanup(session)
        await close_driver()
        return counts

    counts = event_loop.run_until_complete(scenario())
    assert counts["edges"] == 1  # ACQUIRED MERGE'd on (acquirer, target), not duplicated
    assert counts["targets"] == 1
