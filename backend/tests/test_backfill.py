"""Back-fill's missing-only scope (needs Neo4j)."""

import asyncio

import pytest

from app.agents.assistant import backfill
from app.agents.assistant.backfill import (
    _applicable_companies,
    enqueue_backfill,
    get_backfill,
    turn_backfills,
)
from app.graph import queries
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


def test_company_scope_selects_one():
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
                "  SET a.website='https://a.example', a.kind='service_provider' "
                "MERGE (b:Company {name:$b}) "
                "  SET b.website='https://b.example', b.kind='service_provider' "
                "MERGE (a)-[:TAGGED_AS]->(t) MERGE (b)-[:TAGGED_AS]->(t)",
                t="__bfcotest__",
                a="__bf_one__",
                b="__bf_two__",
            )
        scoped = {
            c["name"]
            for c in await _applicable_companies(d, "service_provider", None, None, "__bf_one__")
        }
        no_match = await _applicable_companies(d, "service_provider", None, None, "__bf_absent__")
        async with d.session() as s:
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a, $b] DETACH DELETE c",
                a="__bf_one__",
                b="__bf_two__",
            )
            await s.run("MATCH (t:Topic {name:$t}) DELETE t", t="__bfcotest__")
        await close_driver()
        return scoped, no_match

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    scoped, no_match = out
    assert scoped == {"__bf_one__"}  # only the named company, not its sibling
    assert no_match == []  # a name that matches nothing selects nothing


def test_structured_scope_filters_by_condition():
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
                "  SET a.website='https://a.example', a.kind='service_provider', "
                "      a.headcount=500, a.hqCountry='Wonderland' "
                "MERGE (b:Company {name:$b}) "
                "  SET b.website='https://b.example', b.kind='service_provider', "
                "      b.headcount=50, b.hqCountry='Wonderland' "
                "MERGE (a)-[:TAGGED_AS]->(t) MERGE (b)-[:TAGGED_AS]->(t)",
                t="__bfscopetest__",
                a="__bf_big__",
                b="__bf_small__",
            )
        big = {
            c["name"]
            for c in await _applicable_companies(
                d,
                "service_provider",
                None,
                None,
                None,
                [{"field": "headcount", "op": ">", "value": 200}],
            )
        }
        # Composes with the fixed country scope.
        big_uk = {
            c["name"]
            for c in await _applicable_companies(
                d,
                "service_provider",
                "Wonderland",
                None,
                None,
                [{"field": "headcount", "op": ">=", "value": 500}],
            )
        }
        async with d.session() as s:
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a, $b] DETACH DELETE c",
                a="__bf_big__",
                b="__bf_small__",
            )
            await s.run("MATCH (t:Topic {name:$t}) DELETE t", t="__bfscopetest__")
        await close_driver()
        return big, big_uk

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    big, big_uk = out
    assert big == {"__bf_big__"}  # only the >200-headcount company
    assert big_uk == {"__bf_big__"}  # condition composes with country scope


def test_enqueue_rejects_hostile_scope():
    # Injection safety at the ceremony boundary: a scope carrying an unknown
    # field (Cypher smuggled into it) is rejected before anything is enqueued.
    # No Neo4j needed — parse_scope fails before any DB access.
    hostile = '[{"field": "headcount} SET c.pwned=true //", "op": ">", "value": 1}]'
    out = asyncio.run(enqueue_backfill("anyField", conditions=hostile))
    assert "error" in out
    assert "scope" in out["error"]
    assert "job_id" not in out


def test_enqueue_threads_scope_into_job_payload(monkeypatch):
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        # A real field def + one applicable company so the enqueue reaches the
        # payload-build step; stub jobs.enqueue so the runner never fires.
        await queries.add_field_def(
            d, "__bfScopeField__", "Scope Field", "desc", "service_provider", "text"
        )
        async with d.session() as s:
            await s.run(
                "MERGE (t:Topic {name:$t}) "
                "MERGE (c:Company {name:$c}) "
                "  SET c.website='https://c.example', c.kind='service_provider', c.headcount=300 "
                "MERGE (c)-[:TAGGED_AS]->(t)",
                t="__bfscopepayload__",
                c="__bf_payload_co__",
            )

        async def _noop_enqueue(job_id, delay=0.0):
            return None

        monkeypatch.setattr(backfill.jobs, "enqueue", _noop_enqueue)
        collected: list = []
        token = turn_backfills.set(collected)
        try:
            result = await enqueue_backfill(
                "__bfScopeField__",
                conditions='[{"field": "headcount", "op": ">", "value": 200}]',
            )
            job = await get_backfill(result["job_id"])
        finally:
            turn_backfills.reset(token)
            async with d.session() as s:
                await s.run("MATCH (c:Company {name:$c}) DETACH DELETE c", c="__bf_payload_co__")
                await s.run("MATCH (t:Topic {name:$t}) DELETE t", t="__bfscopepayload__")
                await s.run("MATCH (fd:FieldDef {name:$n}) DELETE fd", n="__bfScopeField__")
            await close_driver()
        return result, job, collected

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    result, job, collected = out
    expected = [{"field": "headcount", "op": ">", "value": 200}]
    assert result["companies"] == 1  # the 300-headcount company matched >200
    assert job["scope"] == expected  # auditable scope stored on the job payload
    assert collected and collected[0]["scope"] == expected  # surfaced for the review card
