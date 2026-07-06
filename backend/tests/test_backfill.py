"""Back-fill's missing-only scope (needs Neo4j)."""

import asyncio

import pytest

from app.agents.assistant.backfill import _applicable_companies
from app.graph.driver import check_connectivity, close_driver, get_driver


def test_missing_only_skips_already_filled():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        async with d.session() as s:
            await s.run(
                "MERGE (t:Topic {name:$t}) "
                "MERGE (a:Company {name:$a}) "
                "  SET a.website='https://a.example', a.kind='service_provider', a.bfTestField='v' "
                "MERGE (b:Company {name:$b}) "
                "  SET b.website='https://b.example', b.kind='service_provider' "
                "MERGE (a)-[:TAGGED_AS]->(t) MERGE (b)-[:TAGGED_AS]->(t)",
                t="__bftest__",
                a="__bf_has__",
                b="__bf_nof__",
            )
        everyone = {c["name"] for c in await _applicable_companies(d, "service_provider", None)}
        missing = {
            c["name"]
            for c in await _applicable_companies(d, "service_provider", None, "bfTestField")
        }
        async with d.session() as s:
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a, $b] DETACH DELETE c",
                a="__bf_has__",
                b="__bf_nof__",
            )
            await s.run("MATCH (t:Topic {name:$t}) DELETE t", t="__bftest__")
        await close_driver()
        return everyone, missing

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    everyone, missing = out
    assert {"__bf_has__", "__bf_nof__"} <= everyone  # both applicable without the filter
    assert "__bf_nof__" in missing  # the unfilled one is kept
    assert "__bf_has__" not in missing  # the already-filled one is skipped
