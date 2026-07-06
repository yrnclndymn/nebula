"""Graph-backed job store round-trip (needs Neo4j)."""

import asyncio

import pytest

from app.graph import jobs
from app.graph.driver import check_connectivity, close_driver, get_driver

JOB_ID = "__pytest_job__"


def test_job_store_roundtrip():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        await jobs.create_job(JOB_ID, "proposal", {"status": "pending", "name": "Acme"})
        created = await jobs.get_job(JOB_ID)
        await jobs.update_job(JOB_ID, {"name": "Acme", "record": {"x": 1}}, status="ready")
        updated = await jobs.get_job(JOB_ID)
        async with get_driver().session() as session:
            await session.run("MATCH (j:Job {id: $id}) DELETE j", id=JOB_ID)
        gone = await jobs.get_job(JOB_ID)
        await close_driver()
        return created, updated, gone

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    created, updated, gone = out
    assert created["status"] == "pending" and created["type"] == "proposal"
    assert updated["status"] == "ready" and updated["record"] == {"x": 1}
    assert gone is None
