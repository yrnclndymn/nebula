"""Scheduler tick: endpoint auth, due-work selection, and tick idempotence.

Auth is a pure unit test (no Neo4j). Selection/idempotence need the graph and
skip gracefully when it's absent (CI is the arbiter — see CLAUDE.md)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.graph import jobs, schedules
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.main import app

STALE_URL = "__pytest_sched_stale__"
FRESH_URL = "__pytest_sched_fresh__"


def test_schedule_tick_rejects_unauthenticated():
    """With auth on, the tick endpoint (verify_task/OIDC) 401s without a token."""
    original = settings.require_auth
    settings.require_auth = True
    try:
        with TestClient(app) as client:
            resp = client.post("/jobs/schedule-tick")
        assert resp.status_code == 401
    finally:
        settings.require_auth = original


def test_tick_selection_and_idempotence(monkeypatch):
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"

        # Isolate selection from execution: record enqueues instead of running them.
        enqueued: list[str] = []

        async def fake_enqueue(job_id: str) -> None:
            enqueued.append(job_id)

        monkeypatch.setattr(jobs, "enqueue", fake_enqueue)

        driver = get_driver()
        async with driver.session() as session:
            # Clean slate: no prior cache_prune job (its node is the cadence guard),
            # no leftover test cache nodes.
            await session.run("MATCH (j:Job {type:'cache_prune'}) DETACH DELETE j")
            await session.run(
                "MATCH (n) WHERE n.url IN [$s,$f] DETACH DELETE n", s=STALE_URL, f=FRESH_URL
            )
            # One stale page (past the prune age) and one fresh page.
            await session.run(
                "CREATE (:Page {url:$s, fetchedAt: datetime() - duration({days:$old})})",
                s=STALE_URL,
                old=schedules._PRUNE_AGE_DAYS + 5,
            )
            await session.run("CREATE (:Page {url:$f, fetchedAt: datetime()})", f=FRESH_URL)

        stale_before = await schedules._stale_cache_exists(driver)
        first = await schedules.run_tick()
        second = await schedules.run_tick()  # cadence guard → no double-enqueue

        # Execute the enqueued prune so we can assert the runner's due-work logic.
        job_id = first["enqueued"][0]
        await jobs.run_job(job_id)
        pruned_job = await jobs.get_job(job_id)

        async with driver.session() as session:
            r = await session.run(
                "MATCH (n) WHERE n.url IN [$s,$f] RETURN collect(n.url) AS urls",
                s=STALE_URL,
                f=FRESH_URL,
            )
            surviving = (await r.single())["urls"]
            await session.run("MATCH (j:Job {type:'cache_prune'}) DETACH DELETE j")
            await session.run(
                "MATCH (n) WHERE n.url IN [$s,$f] DETACH DELETE n", s=STALE_URL, f=FRESH_URL
            )
        await close_driver()
        return {
            "stale_before": stale_before,
            "first": first,
            "second": second,
            "enqueued_count": len(enqueued),
            "pruned_job": pruned_job,
            "surviving": surviving,
        }

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")

    # Due work was detected and exactly one job enqueued on the first tick.
    assert out["stale_before"] is True
    assert len(out["first"]["enqueued"]) == 1
    # Second tick enqueued nothing (idempotent within the cadence window).
    assert out["second"]["enqueued"] == []
    assert any(s.startswith("cache_prune:cadence") for s in out["second"]["skipped"])
    assert out["enqueued_count"] == 1
    # The runner pruned the stale entry, kept the fresh one, and recorded the count.
    assert out["pruned_job"]["status"] == "done"
    assert out["pruned_job"]["pruned"] == 1
    assert out["surviving"] == [FRESH_URL]


def test_tick_skips_when_no_due_work(monkeypatch):
    """With no stale cache entries, the tick enqueues nothing (no empty jobs)."""

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"

        enqueued: list[str] = []
        monkeypatch.setattr(jobs, "enqueue", lambda job_id: _record(enqueued, job_id))

        driver = get_driver()
        async with driver.session() as session:
            await session.run("MATCH (j:Job {type:'cache_prune'}) DETACH DELETE j")
            await session.run(
                "MATCH (n) WHERE n.url IN [$s,$f] DETACH DELETE n", s=STALE_URL, f=FRESH_URL
            )
        result = await schedules.run_tick()
        async with driver.session() as session:
            await session.run("MATCH (j:Job {type:'cache_prune'}) DETACH DELETE j")
        await close_driver()
        return result, enqueued

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    result, enqueued = out
    assert result["enqueued"] == []
    assert any(s.startswith("cache_prune:no-work") for s in result["skipped"])
    assert enqueued == []


async def _record(bucket: list[str], job_id: str) -> None:
    bucket.append(job_id)


def test_errored_job_does_not_block_cadence(monkeypatch):
    """A failed run must not lock out retries for the whole cadence window: an
    errored job is excluded from the cadence guard, so the next tick re-enqueues."""

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"

        enqueued: list[str] = []
        monkeypatch.setattr(jobs, "enqueue", lambda job_id: _record(enqueued, job_id))

        driver = get_driver()
        async with driver.session() as session:
            await session.run("MATCH (j:Job {type:'cache_prune'}) DETACH DELETE j")
            await session.run(
                "MATCH (n) WHERE n.url IN [$s,$f] DETACH DELETE n", s=STALE_URL, f=FRESH_URL
            )
            await session.run(
                "CREATE (:Page {url:$s, fetchedAt: datetime() - duration({days:$old})})",
                s=STALE_URL,
                old=schedules._PRUNE_AGE_DAYS + 5,
            )

        first = await schedules.run_tick()
        job_id = first["enqueued"][0]

        # Simulate the runner failing: run_scheduled marks the job errored.
        async def boom(_job_id: str) -> None:
            raise RuntimeError("simulated runner failure")

        failing = schedules.Schedule(job_type="cache_prune", cadence_days=7, run=boom)
        monkeypatch.setattr(schedules, "SCHEDULES", [failing])
        await schedules.run_scheduled(job_id, "cache_prune")
        errored = await jobs.get_job(job_id)

        # The errored job must not satisfy the cadence guard: a re-tick re-enqueues.
        monkeypatch.setattr(
            schedules,
            "SCHEDULES",
            [
                schedules.Schedule(
                    job_type="cache_prune",
                    cadence_days=7,
                    run=schedules.run_cache_prune,
                    is_due=schedules._stale_cache_exists,
                )
            ],
        )
        second = await schedules.run_tick()

        async with driver.session() as session:
            await session.run("MATCH (j:Job {type:'cache_prune'}) DETACH DELETE j")
            await session.run(
                "MATCH (n) WHERE n.url IN [$s,$f] DETACH DELETE n", s=STALE_URL, f=FRESH_URL
            )
        await close_driver()
        return errored, second

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    errored, second = out
    assert errored["status"] == "error"
    assert "simulated runner failure" in errored["error"]
    assert len(second["enqueued"]) == 1  # retry allowed despite recent errored job
