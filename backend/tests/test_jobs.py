"""Graph-backed job store round-trip (needs Neo4j) + the job-listing query and
`GET /jobs` endpoint that rehydrate research activity after a refresh (#66).

The listing query is skip-guarded on Neo4j; the endpoint auth + summary-shape
tests mock the query so they run anywhere. Fictional company names only."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.graph import jobs
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.main import app

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


# --- list_jobs: filters, newest-first order, compact summary (needs Neo4j) ----

LIST_PREFIX = "__pytest_listjobs__"
# Distinct timestamps so ORDER BY createdAt DESC is deterministic in the test.
SEED = [
    {
        "id": f"{LIST_PREFIX}_p1",
        "type": "proposal",
        "status": "pending",
        "ts": "2026-07-10T12:00:00Z",
        "data": {"name": "Acme __pytest__"},
    },
    {
        "id": f"{LIST_PREFIX}_p2",
        "type": "proposal",
        "status": "ready",
        "ts": "2026-07-10T11:00:00Z",
        # A big record must NOT leak into the summary.
        "data": {
            "name": "Globex __pytest__",
            "discovered_website": "globex.example",
            "record": {"about": "x" * 500},
        },
    },
    {
        "id": f"{LIST_PREFIX}_p3",
        "type": "proposal",
        "status": "error",
        "ts": "2026-07-10T10:00:00Z",
        "data": {"name": "Initech __pytest__", "error": "no website found"},
    },
    {
        "id": f"{LIST_PREFIX}_b1",
        "type": "backfill",
        "status": "ready",
        "ts": "2026-07-10T13:00:00Z",  # newest overall, but a different type
        "data": {"name": "Backfill __pytest__"},
    },
]


def test_list_jobs_filters_order_summary():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        async with driver.session() as session:
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=LIST_PREFIX
            )
            await session.run(
                "UNWIND $rows AS r CREATE (j:Job {id: r.id, type: r.type, status: r.status, "
                "dataJson: r.data, createdAt: datetime(r.ts)})",
                rows=[{**r, "data": json.dumps(r["data"])} for r in SEED],
            )
        proposals = await jobs.list_jobs(driver, type="proposal")
        ready = await jobs.list_jobs(driver, type="proposal", status="ready")
        limited = await jobs.list_jobs(driver, type="proposal", limit=1)
        async with driver.session() as session:
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=LIST_PREFIX
            )
        await close_driver()
        return proposals, ready, limited

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    proposals, ready, limited = out

    # type filter excludes the backfill job; newest-first by createdAt.
    assert [j["id"] for j in proposals] == [
        f"{LIST_PREFIX}_p1",
        f"{LIST_PREFIX}_p2",
        f"{LIST_PREFIX}_p3",
    ]

    # Every row has the compact shape and never the raw dataJson / full record.
    for j in proposals:
        assert set(j) == {"id", "type", "status", "createdAt", "summary"}
        assert "record" not in j and "dataJson" not in j
        assert set(j["summary"]) <= {"name", "discovered_website", "error"}

    # Type-aware summary: names carried through; null fields pruned; error surfaced.
    assert proposals[0]["summary"] == {"name": "Acme __pytest__"}
    assert proposals[1]["summary"] == {
        "name": "Globex __pytest__",
        "discovered_website": "globex.example",
    }
    assert proposals[2]["summary"]["error"] == "no website found"

    # status filter and limit.
    assert [j["id"] for j in ready] == [f"{LIST_PREFIX}_p2"]
    assert [j["id"] for j in limited] == [f"{LIST_PREFIX}_p1"]


# --- GET /jobs: auth + summary wiring (query mocked; no DB / network) ---------


def test_list_jobs_endpoint_requires_auth():
    settings.require_auth = True
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/jobs?type=proposal")
        assert resp.status_code == 401  # missing bearer token, rejected by verify_user
    finally:
        settings.require_auth = False


def test_list_jobs_endpoint_passes_filters_and_returns_summaries(monkeypatch):
    from app.api import routes

    captured: dict = {}

    async def fake_list_jobs(driver, *, type=None, status=None, limit=50):
        captured.update(type=type, status=status, limit=limit)
        return [
            {
                "id": "j1",
                "type": "proposal",
                "status": "ready",
                "createdAt": "2026-07-10T00:00:00Z",
                "summary": {"name": "Acme __pytest__", "discovered_website": "acme.example"},
            }
        ]

    monkeypatch.setattr(routes.jobs, "list_jobs", fake_list_jobs)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/jobs?type=proposal&status=ready&limit=5")
    assert resp.status_code == 200
    assert captured == {"type": "proposal", "status": "ready", "limit": 5}
    body = resp.json()
    assert body[0]["summary"] == {"name": "Acme __pytest__", "discovered_website": "acme.example"}
    assert "record" not in body[0] and "dataJson" not in body[0]
