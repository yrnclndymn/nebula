"""Graph integration for the acquisition thesis (#193): the write + read paths.

Skips when Neo4j is unreachable, so `make test` stays green without a database
(CI, with its Neo4j service, is the arbiter). Run locally with an ephemeral DB:
`make db-ephemeral` then `NEO4J_URI=... make test`.

Verifies: the seed MERGEs the three ThesisRule nodes idempotently (re-seed does
not duplicate), the read-back returns rules with an evidence count (0 for the
freshly-seeded, human-authored rules), and attaching SUPPORTED_BY provenance edges
raises that count and is itself idempotent. Abstract kinds + a fictional source
URL only (public-repo rule).
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.schema import apply_schema
from app.graph.thesis import (
    SEED_RULES,
    get_thesis_rules,
    seed_thesis,
    upsert_thesis_rule,
)

SEED_KEYS = {r.rule_key for r in SEED_RULES}
EV_SRC = "https://news.example/pytest193-deal"


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
        "MATCH (tr:ThesisRule) WHERE tr.ruleKey IN $keys DETACH DELETE tr", keys=list(SEED_KEYS)
    )
    await session.run("MATCH (s:Source {url: $url}) DETACH DELETE s", url=EV_SRC)


def test_seed_is_idempotent_and_reads_back_with_evidence_counts(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        first = await seed_thesis(driver)
        await seed_thesis(driver)  # re-seed must MERGE, not duplicate

        rules = await get_thesis_rules(driver)
        seeded = [r for r in rules if r["rule_key"] in SEED_KEYS]

        async with driver.session() as session:
            res = await session.run(
                "MATCH (tr:ThesisRule) WHERE tr.ruleKey IN $keys RETURN count(tr) AS n",
                keys=list(SEED_KEYS),
            )
            node_count = (await res.single())["n"]
            await _cleanup(session)
        await close_driver()
        return first, seeded, node_count

    first, seeded, node_count = event_loop.run_until_complete(scenario())
    assert first["rules"] == 3
    assert node_count == 3  # re-seed did not create duplicates
    assert len(seeded) == 3
    for r in seeded:
        assert r["origin"] == "user"
        assert r["statement"].strip()
        assert r["evidence_count"] == 0  # freshly seeded, no observed deals yet


def test_supported_by_evidence_raises_count_idempotently(event_loop):
    if not event_loop.run_until_complete(_neo4j_available()):
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")

    async def scenario():
        driver = get_driver()
        await apply_schema(driver)
        async with driver.session() as session:
            await _cleanup(session)

        rule = SEED_RULES[0]
        await upsert_thesis_rule(driver, rule)
        # Attach the same provenance twice — SUPPORTED_BY MERGEs, so the count is 1.
        await upsert_thesis_rule(driver, rule, evidence=[EV_SRC])
        await upsert_thesis_rule(driver, rule, evidence=[EV_SRC])

        rules = await get_thesis_rules(driver)
        got = next(r for r in rules if r["rule_key"] == rule.rule_key)

        async with driver.session() as session:
            await _cleanup(session)
        await close_driver()
        return got

    got = event_loop.run_until_complete(scenario())
    assert got["evidence_count"] == 1
