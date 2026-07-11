"""Periodic-work registry + the schedule tick.

The durable job runner (`app.graph.jobs`) is *pull*: something has to decide what
to enqueue and when. Cloud Scheduler provides the heartbeat by POSTing
`/jobs/schedule-tick` (OIDC-authed, same as the Cloud Tasks callback); this module
provides the *policy* — a declarative registry of periodic job types and, for
each, a cheap "is there due work?" check. `run_tick` walks the registry and
enqueues a durable `:Job` for every schedule that is both due (cadence elapsed)
and has work to do.

Adding a future ambient job type is meant to be *data, not code*: append a
`Schedule` to `SCHEDULES` with its cadence, a due-check, and a runner.

Idempotence (Cloud Scheduler retries, and ticks can overlap a slow cadence): the
cadence guard keys off the most recent `:Job` of that type in the graph. Once a
tick enqueues one, its node exists with a fresh `createdAt`, so a re-tick inside
the cadence window skips it — no double-enqueue. (A guard against two *truly
simultaneous* ticks would need a graph-level lock; Scheduler retries are spaced,
so the last-run check is sufficient here and keeps the tick cheap.)
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from neo4j import AsyncDriver

from app.config import settings
from app.graph import jobs
from app.graph.driver import get_driver

logger = logging.getLogger("nebula.schedules")


@dataclass(frozen=True)
class Schedule:
    """One periodic job type.

    - `job_type`   — the `:Job` type; its runner is dispatched by `run_scheduled`.
    - `cadence_days` — minimum spacing between runs; also the idempotence window.
    - `is_due`     — cheap graph check: is there actual work right now? Keeps the
                     tick from enqueuing empty no-op jobs every cadence. Defaults
                     to "always" for schedules that should just run on cadence.
    - `build_payload` — initial `dataJson` for the created job.
    - `run`        — the async runner, invoked with the job id by `run_scheduled`.
    """

    job_type: str
    cadence_days: float
    run: Callable[[str], Awaitable[None]]
    is_due: Callable[[AsyncDriver], Awaitable[bool]] = field(default=lambda _driver: _always_due())
    build_payload: Callable[[], dict] = field(default=dict)


async def _always_due() -> bool:
    return True


async def _cadence_elapsed(driver: AsyncDriver, job_type: str, cadence_days: float) -> bool:
    """False if a job of this type was created within the cadence window. This is
    the idempotence guard: a just-enqueued job blocks a re-tick's duplicate.
    Errored jobs don't count — a failed run shouldn't block its retry for the
    whole cadence window; the next tick re-enqueues it."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (j:Job {type: $type}) "
            "WHERE j.status <> 'error' "
            "AND j.createdAt >= datetime() - duration({seconds: $secs}) "
            "RETURN count(j) AS n",
            type=job_type,
            secs=cadence_days * 86400,
        )
        record = await result.single()
    return record["n"] == 0


async def run_tick() -> dict:
    """Select due work across the registry and enqueue a durable job for each.
    Returns a summary (enqueued/skipped) for logging. Safe to call repeatedly:
    the cadence guard makes a double-tick a no-op within the window."""
    driver = get_driver()
    enqueued: list[str] = []
    skipped: list[str] = []
    for sched in SCHEDULES:
        if not await _cadence_elapsed(driver, sched.job_type, sched.cadence_days):
            skipped.append(f"{sched.job_type}:cadence")
            continue
        if not await sched.is_due(driver):
            skipped.append(f"{sched.job_type}:no-work")
            continue
        job_id = f"{sched.job_type}-{uuid.uuid4().hex[:8]}"
        await jobs.create_job(
            job_id, sched.job_type, {"status": "pending", **sched.build_payload()}
        )
        await jobs.enqueue(job_id)
        enqueued.append(job_id)
        logger.info("schedule tick enqueued %s (%s)", job_id, sched.job_type)
    return {"enqueued": enqueued, "skipped": skipped}


async def run_scheduled(job_id: str, job_type: str) -> None:
    """Dispatch a scheduled job to its registered runner (called by jobs.run_job
    for any type this registry owns). A runner that raises marks its job errored
    (instead of leaving it pending forever); with errored jobs excluded from the
    cadence guard, the next tick retries it."""
    for sched in SCHEDULES:
        if sched.job_type == job_type:
            try:
                await sched.run(job_id)
            except Exception as exc:  # noqa: BLE001 — surface the failure on the job
                logger.exception("scheduled job %s (%s) failed", job_id, job_type)
                job = await jobs.get_job(job_id)
                await jobs.update_job(job_id, {**(job or {}), "error": str(exc)}, status="error")
            return


def owns(job_type: str) -> bool:
    return any(s.job_type == job_type for s in SCHEDULES)


# --- Example schedule: prune the crawl cache -----------------------------------
# A concrete, safe wiring that proves the path end to end. The crawl cache
# (:Page / :SiteClients, see app/graph/cache.py) is read-through with a TTL — stale
# entries are ignored on read but never deleted, so they accumulate. This prunes
# entries older than the TTL. It's harmless: anything pruned would be re-fetched.

# Prune anything older than double the read TTL — well past the point it could be
# served — so pruning never races a still-usable entry.
_PRUNE_AGE_DAYS = settings.cache_ttl_days * 2


async def _stale_cache_exists(driver: AsyncDriver) -> bool:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (n) WHERE (n:Page OR n:SiteClients) "
            "AND n.fetchedAt < datetime() - duration({days: $days}) "
            "RETURN count(n) AS n LIMIT 1",
            days=_PRUNE_AGE_DAYS,
        )
        record = await result.single()
    return record["n"] > 0


async def run_cache_prune(job_id: str) -> None:
    """Delete crawl-cache entries older than the prune age; record the count."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (n) WHERE (n:Page OR n:SiteClients) "
            "AND n.fetchedAt < datetime() - duration({days: $days}) "
            "DETACH DELETE n RETURN count(n) AS pruned",
            days=_PRUNE_AGE_DAYS,
        )
        pruned = (await result.single())["pruned"]
    job = await jobs.get_job(job_id)
    # `outcome` is the standard human-readable completion line the activity page
    # (#49) shows for a finished run; `pruned` stays for the numeric detail.
    await jobs.update_job(
        job_id,
        {**(job or {}), "pruned": pruned, "outcome": f"pruned {pruned} stale cache entries"},
        status="done",
    )
    logger.info("cache prune %s removed %d stale entries", job_id, pruned)


# --- Job-history retention -------------------------------------------------------
# The activity page (#48) reads :Job nodes, so history grows without bound. This
# schedule prunes jobs older than `settings.job_retention_days`, with ONE
# exception: a proposal job that is `ready` but not yet `committed` is un-reviewed
# work — deleting it would silently discard a prepared enrichment the user never
# got to accept or reject. Committed proposals keep node status "ready" on purpose
# (the two-step focus/all commit), so status alone can't tell the two apart; the
# committed flag lives in dataJson, matched here as a string (json.dumps emits a
# stable `"committed": true`). Everything else past retention — done/errored/
# pending jobs of any type, and committed proposals — is fair game.
# A job is deletable past retention UNLESS it is a ready-but-uncommitted proposal
# (the kept exception). This is the "OK to delete" half of the WHERE clause.
_RETENTION_DELETABLE = (
    "NOT (j.type = 'proposal' AND j.status = 'ready' "
    "AND NOT j.dataJson CONTAINS '\"committed\": true')"
)


async def _prunable_jobs_exist(driver: AsyncDriver) -> bool:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (j:Job) "
            "WHERE j.createdAt < datetime() - duration({days: $days}) "
            f"AND {_RETENTION_DELETABLE} "
            "RETURN count(j) AS n LIMIT 1",
            days=settings.job_retention_days,
        )
        record = await result.single()
    return record["n"] > 0


async def run_job_prune(job_id: str) -> None:
    """Delete :Job nodes older than the retention window, keeping ready-but-
    uncommitted proposal jobs (un-reviewed work). Records the count + outcome.

    The prune job deletes itself only on a *later* run: it's created fresh each
    tick, so it's inside the retention window and the age filter spares it."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (j:Job) "
            "WHERE j.createdAt < datetime() - duration({days: $days}) "
            f"AND {_RETENTION_DELETABLE} "
            "DETACH DELETE j RETURN count(j) AS pruned",
            days=settings.job_retention_days,
        )
        pruned = (await result.single())["pruned"]
    job = await jobs.get_job(job_id)
    await jobs.update_job(
        job_id,
        {**(job or {}), "pruned": pruned, "outcome": f"pruned {pruned} old job records"},
        status="done",
    )
    logger.info("job prune %s removed %d old job records", job_id, pruned)


SCHEDULES: list[Schedule] = [
    Schedule(
        job_type="cache_prune",
        cadence_days=7,
        run=run_cache_prune,
        is_due=_stale_cache_exists,
    ),
    Schedule(
        job_type="job_prune",
        cadence_days=1,
        run=run_job_prune,
        is_due=_prunable_jobs_exist,
    ),
]
