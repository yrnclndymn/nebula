"""Chat-proposed merges: the tool proposes (never writes), the scoped job commits
through the resolution path with the researched-variant guard relaxed, and the
survivor is chosen to protect researched data. Graph paths skip without Neo4j.
Fictional names only.
"""

import asyncio

import pytest

from app.agents.assistant.merge import propose_merge, start_merge, turn_merges
from app.agents.assistant.resolution import commit_resolution
from app.graph import jobs
from app.graph.driver import check_connectivity, close_driver, get_driver

# --- Wiring + instructions (pure, no DB) -------------------------------------


def test_assistant_wires_up_merge_tool():
    from app.agents.assistant.agent import root_agent

    names = {getattr(t, "__name__", getattr(t, "name", None)) for t in root_agent.tools}
    assert "propose_merge" in names


def test_instruction_has_no_invented_capabilities_rule():
    from app.agents.assistant.agent import _INSTRUCTION

    lowered = _INSTRUCTION.lower()
    # The rule the story requires: never invent capabilities it doesn't have.
    assert "never invent capabilities" in lowered
    # And that it must say so plainly rather than improvise a fake plan.
    assert "say so plainly" in lowered
    # The merge tool is advertised so "merge X and Y" routes to propose_merge.
    assert "propose_merge" in _INSTRUCTION


# --- propose_merge proposes, never writes (needs Neo4j) ----------------------

A = "Acme __mgtest__"
B = "Acme Inc __mgtest__"


def test_propose_merge_creates_job_without_merging():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        async with d.session() as s:
            await s.run("MATCH (c:Company) WHERE c.name IN [$a,$b] DETACH DELETE c", a=A, b=B)
            # Both bare stubs so the survivor choice is left to the user's canonical.
            await s.run("MERGE (:Company {name:$a}) MERGE (:Company {name:$b})", a=A, b=B)

        merges: list = []
        token = turn_merges.set(merges)
        try:
            result = await propose_merge(A, [B])
        finally:
            turn_merges.reset(token)

        job = await jobs.get_job(result["job_id"])
        async with d.session() as s:
            r = await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a,$b] RETURN count(c) AS n", a=A, b=B
            )
            still_there = (await r.single())["n"]
            await s.run("MATCH (j:Job {id:$id}) DELETE j", id=result["job_id"])
            await s.run("MATCH (c:Company) WHERE c.name IN [$a,$b] DETACH DELETE c", a=A, b=B)
        await close_driver()
        return result, job, merges, still_there

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    result, job, merges, still_there = out
    assert still_there == 2  # the invariant that matters: nothing merged/deleted
    assert result["canonical"] == A
    assert job is not None and job["status"] == "ready"  # committable straight away
    assert job["scoped_merge"] is True  # marker that relaxes the guard on commit
    assert len(merges) == 1 and merges[0]["job_id"] == result["job_id"]  # surfaced in the turn


def test_start_merge_keeps_researched_node_as_survivor():
    """The user named a bare stub as canonical, but a variant is already
    researched — start_merge promotes the researched node to survivor so its data
    is not deleted."""
    stub = "Globex __mgtest__"
    researched = "Globex Inc __mgtest__"

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        async with d.session() as s:
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a,$b] DETACH DELETE c", a=stub, b=researched
            )
            await s.run(
                "MERGE (:Company {name:$a}) "
                "MERGE (v:Company {name:$b}) SET v.website = 'https://example.invalid'",
                a=stub,
                b=researched,
            )
        result = await start_merge(stub, [researched])  # user named the stub as canonical
        async with d.session() as s:
            await s.run("MATCH (j:Job {id:$id}) DELETE j", id=result["job_id"])
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a,$b] DETACH DELETE c", a=stub, b=researched
            )
        await close_driver()
        return result

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    assert out["canonical"] == researched  # survivor swapped to the researched node
    assert researched in out["canonical_reason"]


def test_scoped_commit_merges_researched_variant():
    """The scoped merge job relaxes the promoted-variant guard: a RESEARCHED variant
    is merged (unlike the scan flow), the survivor keeps its own values, the
    variant's unique props fill gaps, its edges re-point, and its name becomes an
    alias. Contrast test_merge_skips_variant_promoted_since_scan (scan keeps guard).
    """
    canon = "Initech __mgtest__"
    variant = "Initech LLC __mgtest__"
    client = "Umbrella __mgtest__"

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        async with d.session() as s:
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a,$b,$c] DETACH DELETE c",
                a=canon,
                b=variant,
                c=client,
            )
            # Both researched (website). The variant carries a unique prop + an edge.
            await s.run(
                """
                MERGE (canon:Company {name:$canon}) SET canon.website='https://canon.invalid'
                MERGE (v:Company {name:$v})
                  SET v.website='https://variant.invalid', v.headcount=9
                MERGE (client:Company {name:$client})
                MERGE (v)-[:HAS_CLIENT]->(client)
                """,
                canon=canon,
                v=variant,
                client=client,
            )
        # Build the scoped job exactly as the tool does, then commit via the
        # resolution path the UI uses.
        started = await start_merge(canon, [variant])
        commit = await commit_resolution(
            started["job_id"],
            [{"action": "merge", "canonical": canon, "variants": [variant]}],
        )
        async with d.session() as s:
            row = await (
                await s.run(
                    """
                    MATCH (canon:Company {name:$canon})
                    RETURN canon.website AS website, canon.headcount AS headcount,
                           canon.aliases AS aliases,
                           EXISTS { (canon)-[:HAS_CLIENT]->(:Company {name:$client}) } AS hasClient,
                           EXISTS { (:Company {name:$v}) } AS variantLives
                    """,
                    canon=canon,
                    v=variant,
                    client=client,
                )
            ).single()
            await s.run("MATCH (j:Job {id:$id}) DELETE j", id=started["job_id"])
            await s.run(
                "MATCH (c:Company) WHERE c.name IN [$a,$b,$c] DETACH DELETE c",
                a=canon,
                b=variant,
                c=client,
            )
        await close_driver()
        return commit, row

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    commit, row = out
    assert commit["merged"] == 1  # researched variant WAS merged (guard relaxed)
    assert row["variantLives"] is False  # variant node deleted
    assert row["website"] == "https://canon.invalid"  # survivor's own value kept, not overwritten
    assert row["headcount"] == 9  # variant's unique prop filled the gap
    assert variant in row["aliases"]  # variant name recorded as an alias
    assert row["hasClient"] is True  # variant's edge re-pointed onto the survivor
