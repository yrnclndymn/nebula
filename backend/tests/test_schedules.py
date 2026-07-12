"""Scheduler tick: endpoint auth, due-work selection, and tick idempotence.

Auth is a pure unit test (no Neo4j). Selection/idempotence need the graph and
skip gracefully when it's absent (CI is the arbiter — see CLAUDE.md)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.graph import jobs, schedules
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.main import app

STALE_URL = "__pytest_sched_stale__"
FRESH_URL = "__pytest_sched_fresh__"
RET_PREFIX = "__pytest_retention__"
SIG_PREFIX = "__pytest_signal__"
SIG_COMPANIES = ["Acme __pytest_sig__", "Globex __pytest_sig__"]


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


# --- Job-history retention (#49): prune old jobs, KEEP un-reviewed proposals ---


def test_job_prune_retention_and_uncommitted_exception():
    """Old jobs past retention are deleted, but a ready-but-uncommitted proposal
    (un-reviewed work) is spared regardless of age. Skip-guarded on Neo4j."""

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        old = settings.job_retention_days + 5
        # (id suffix, type, status, age_days, dataJson payload)
        seed = [
            ("old_done", "cache_prune", "done", old, {"pruned": 3}),
            ("old_err", "proposal", "error", old, {"name": "Initech __pytest__", "error": "x"}),
            # Committed proposal keeps node status "ready" on purpose — still prunable.
            (
                "old_committed",
                "proposal",
                "ready",
                old,
                {"name": "Acme __pytest__", "committed": True},
            ),
            # Ready + never committed = un-reviewed work → the exception, kept.
            ("old_uncommitted", "proposal", "ready", old, {"name": "Globex __pytest__"}),
            # Recent uncommitted proposal: inside retention anyway.
            ("new_uncommitted", "proposal", "ready", 0, {"name": "Umbrella __pytest__"}),
            # Review finding: OTHER job types also await review at status='ready'.
            # An old ready backfill (rows awaiting commit) must be spared…
            ("old_backfill_ready", "backfill", "ready", old, {"rows": []}),
            # …while a committed resolution flips status='committed' → prunable.
            ("old_res_committed", "resolution", "committed", old, {"committed": True}),
        ]
        async with driver.session() as session:
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=RET_PREFIX
            )
            await session.run(
                "UNWIND $rows AS r CREATE (j:Job {id: r.id, type: r.type, status: r.status, "
                "dataJson: r.data, createdAt: datetime() - duration({days: r.age})})",
                rows=[
                    {
                        "id": f"{RET_PREFIX}_{sfx}",
                        "type": t,
                        "status": st,
                        "age": age,
                        "data": json.dumps(d),
                    }
                    for (sfx, t, st, age, d) in seed
                ],
            )
        due_before = await schedules._prunable_jobs_exist(driver)

        # Run the prune (its own recent job node is inside retention, so spared).
        prune_id = f"{RET_PREFIX}_prune"
        await jobs.create_job(prune_id, "job_prune", {"status": "pending"})
        await schedules.run_job_prune(prune_id)
        prune_job = await jobs.get_job(prune_id)

        async with driver.session() as session:
            r = await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p RETURN collect(j.id) AS ids", p=RET_PREFIX
            )
            surviving = set((await r.single())["ids"])
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=RET_PREFIX
            )
        await close_driver()
        return due_before, prune_job, surviving

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    due_before, prune_job, surviving = out

    assert due_before is True
    # The prune ran and recorded a human-readable outcome + count (>= our 3 old
    # jobs; a shared DB may hold other prunable jobs too).
    assert prune_job["status"] == "done"
    assert prune_job["pruned"] >= 3
    assert "old job" in prune_job["outcome"]
    # The un-reviewed (ready + uncommitted) proposal survived DESPITE its age —
    # the exception — as did the recent one and the prune job itself.
    assert f"{RET_PREFIX}_old_uncommitted" in surviving
    assert f"{RET_PREFIX}_new_uncommitted" in surviving
    assert f"{RET_PREFIX}_prune" in surviving
    # Everything else past retention was pruned (done, errored, committed proposal).
    assert f"{RET_PREFIX}_old_done" not in surviving
    assert f"{RET_PREFIX}_old_err" not in surviving
    assert f"{RET_PREFIX}_old_committed" not in surviving
    # Broadened exception (review finding): ready non-proposal jobs are
    # protected; committed resolutions (status flipped) are prunable.
    assert f"{RET_PREFIX}_old_backfill_ready" in surviving
    assert f"{RET_PREFIX}_old_res_committed" not in surviving


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


def test_signal_prune_enforces_caps_and_protects_unreviewed(monkeypatch):
    """The signal_prune runner deletes signals past the count/age caps, keeps a
    shared story that's still within cap for another company, and never deletes a
    signal cited by un-reviewed work. Skip-guarded on Neo4j."""
    # Small caps so fixtures stay tiny; real defaults are far larger.
    monkeypatch.setattr(settings, "signal_max_per_company", 2)
    monkeypatch.setattr(settings, "signal_max_age_days", 365)

    acme, globex = SIG_COMPANIES

    def u(name: str) -> str:
        return f"{SIG_PREFIX}{name}"

    # (url, kind, age_days, companies) — Acme news group has 6 entries (cap 2).
    seed = [
        (u("n0"), "news", 1, [acme]),  # rank 1 → keep
        (u("n1"), "news", 2, [acme]),  # rank 2 → keep
        (u("n2"), "news", 3, [acme]),  # rank 3 → over count → prune
        (u("old"), "news", 400, [acme]),  # over count AND too old → prune
        (u("b0"), "blog", 1, [acme]),  # separate kind, rank 1 → keep
        (u("shared"), "news", 9, [acme, globex]),  # Acme-overflow but Globex rank 1 → keep
        (u("orphan"), "news", 1, []),  # no company → clears no cap → prune
        (u("cited"), "news", 400, [acme]),  # too old, but cited by un-reviewed job → keep
    ]
    job_id = f"{SIG_PREFIX}prune"
    unreviewed_job = f"{SIG_PREFIX}proposal"

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        driver = get_driver()
        links = [{"url": url, "company": c} for (url, _k, _a, cs) in seed for c in cs]
        async with driver.session() as session:
            await _clean_signals(session)
            await session.run(
                "UNWIND $rows AS r CREATE (s:Signal {url: r.url, kind: r.kind, "
                "capturedAt: datetime() - duration({days: r.age})})",
                rows=[{"url": url, "kind": k, "age": a} for (url, k, a, _cs) in seed],
            )
            await session.run(
                "UNWIND $links AS l MATCH (s:Signal {url: l.url}) "
                "MERGE (c:Company {name: l.company}) MERGE (c)-[:MENTIONED_IN]->(s)",
                links=links,
            )
            # An un-reviewed proposal (ready, not committed) citing the `cited` URL.
            await session.run(
                "CREATE (:Job {id: $id, type: 'proposal', status: 'ready', "
                "dataJson: $data, createdAt: datetime()})",
                id=unreviewed_job,
                data=json.dumps({"name": acme, "evidence": u("cited")}),
            )

        due_before = await schedules._prunable_signals_exist(driver)

        await jobs.create_job(job_id, "signal_prune", {"status": "pending"})
        await schedules.run_signal_prune(job_id)
        prune_job = await jobs.get_job(job_id)

        async with driver.session() as session:
            r = await session.run(
                "MATCH (s:Signal) WHERE s.url STARTS WITH $p RETURN collect(s.url) AS urls",
                p=SIG_PREFIX,
            )
            surviving = set((await r.single())["urls"])
            await _clean_signals(session)
            await session.run(
                "MATCH (j:Job) WHERE j.id STARTS WITH $p DETACH DELETE j", p=SIG_PREFIX
            )
        await close_driver()
        return due_before, prune_job, surviving

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    due_before, prune_job, surviving = out

    assert due_before is True  # Acme news group of 6 > cap 2
    assert prune_job["status"] == "done"
    # Deleted exactly the three: over-count n2, too-old `old`, and the orphan.
    assert prune_job["pruned"] == 3
    assert prune_job["prunedByKind"] == {"news": 3}
    assert prune_job["protected"] == 1  # the cited signal was spared
    assert "pruned 3 signals" in prune_job["outcome"]
    assert prune_job["graphSize"]["nodeCap"] == 200_000
    # Survivors: within-cap news, the blog, the shared story, and the cited one.
    assert surviving == {u("n0"), u("n1"), u("b0"), u("shared"), u("cited")}


async def _clean_signals(session):
    await session.run("MATCH (s:Signal) WHERE s.url STARTS WITH $p DETACH DELETE s", p=SIG_PREFIX)
    await session.run(
        "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c", names=SIG_COMPANIES
    )


def test_committed_flag_serializer_canary():
    """The retention predicate matches '"committed": true' as a substring of the
    job's serialized dataJson. This canary pins the coupling: if the serializer
    ever changes shape (separators, casing), this fails before retention breaks."""
    import json

    assert '"committed": true' in json.dumps({"committed": True})
    assert '"committed": true' in json.dumps({"a": 1, "committed": True, "b": 2})
