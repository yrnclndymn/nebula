"""Durable background jobs, stored in the graph so they survive Cloud Run
scale-to-zero (in-memory dicts + fire-and-forget tasks would not).

A job is a `(:Job {id, type, status, dataJson})` node whose `dataJson` is the
job's payload/result. `enqueue` triggers the work:
- "local"      → run inline via an asyncio task (dev: one long-lived process).
- "cloudtasks" → enqueue a Cloud Task that POSTs /jobs/run/{id}, which runs it in
                 a request that has CPU (survives scale-to-zero).

Both paths converge on `execute_job`, which dispatches to the type's runner (late
imports avoid an import cycle). Poll/commit read the job from the graph.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from neo4j import AsyncDriver

from app.config import settings
from app.graph.driver import get_driver

logger = logging.getLogger("nebula.jobs")


async def create_job(job_id: str, job_type: str, data: dict) -> None:
    async with get_driver().session() as session:
        await session.run(
            "CREATE (j:Job {id: $id, type: $type, status: $status, dataJson: $data, "
            "createdAt: datetime()})",
            id=job_id,
            type=job_type,
            status=data.get("status", "pending"),
            data=json.dumps(data),
        )


async def get_job(job_id: str) -> dict | None:
    async with get_driver().session() as session:
        result = await session.run(
            "MATCH (j:Job {id: $id}) RETURN j.type AS type, j.status AS status, j.dataJson AS data",
            id=job_id,
        )
        record = await result.single()
    if record is None:
        return None
    data = json.loads(record["data"])
    data["status"] = record["status"]  # node status is authoritative
    data["type"] = record["type"]
    return data


async def update_job(job_id: str, data: dict, status: str | None = None) -> None:
    async with get_driver().session() as session:
        await session.run(
            "MATCH (j:Job {id: $id}) SET j.dataJson = $data"
            + (", j.status = $status" if status else ""),
            id=job_id,
            data=json.dumps(data),
            status=status,
        )


async def delete_job(job_id: str) -> bool:
    """Dismiss a job: delete its node. Returns whether anything was deleted.
    The CALLER gates on status first (the endpoint refuses pending jobs — they're
    still queued to run); jobs are operational records, so a hard delete is fine
    (retention prunes them eventually anyway)."""
    async with get_driver().session() as session:
        result = await session.run(
            "MATCH (j:Job {id: $id}) DETACH DELETE j RETURN count(*) AS n", id=job_id
        )
        record = await result.single()
    return bool(record and record["n"])


# --- shared scan→review→commit ceremony ---------------------------------------
# The review flows (resolution, classification, …) all follow the same durable-job
# lifecycle: an endpoint ENQUEUES a scan job and returns a poll handle; the runner
# EXECUTES it later (inline locally, via a Cloud Tasks POST in prod — see
# `enqueue`) and marks it ready/errored; a commit endpoint then applies the
# reviewed result exactly once. These helpers hold that ceremony so each flow
# contributes only its scan and commit logic.


async def enqueue_scan_job(job_type: str, payload: dict) -> dict:
    """Producer half: create a pending scan job with `payload` as its initial
    (empty) result fields, trigger it, and return the standard poll-me response."""
    job_id = uuid.uuid4().hex[:8]
    await create_job(job_id, job_type, {"job_id": job_id, "status": "pending", **payload})
    await enqueue(job_id)
    return {"job_id": job_id, "status": "scanning in the background"}


async def execute_scan_job(job_id: str, scan) -> None:
    """Consumer half: run `scan(job)` (a coroutine returning the result fields to
    merge into the payload) and mark the job ready — or store the error and mark
    it errored. A vanished job is a no-op."""
    job = await get_job(job_id)
    if job is None:
        return
    try:
        result = await scan(job)
    except Exception as exc:  # noqa: BLE001 — surface scan failures to the client
        await update_job(job_id, {**job, "error": str(exc)}, status="error")
        return
    await update_job(job_id, {**job, **result}, status="ready")


async def get_ready_job(job_id: str) -> dict | None:
    """Commit-time guard: the job, provided it exists and is status='ready' —
    else None (unknown, still running, errored, or already committed)."""
    job = await get_job(job_id)
    if job is None or job.get("status") != "ready":
        return None
    return job


async def mark_committed(job_id: str, job: dict) -> None:
    """Flip a reviewed job to 'committed' so a stale double-POST is rejected by
    `get_ready_job` (the commit ops are idempotent-safe, but there is no reason
    to redo them)."""
    await update_job(job_id, {**job, "committed": True}, status="committed")


def _job_summary(
    job_id: str, job_type: str, status: str, created_at, data_json: str | None
) -> dict:
    """A compact, type-aware view of a job for the listing endpoint — enough to
    render an activity row without shipping the (potentially large) full dataJson.
    The per-id detail endpoints (`/proposals/{id}`, `/backfill/{id}`, …) still
    return the whole payload. dataJson is parsed server-side so the client never
    sees raw job internals; null fields are dropped to keep the summary small."""
    data = json.loads(data_json) if data_json else {}
    if job_type == "proposal":
        fields = {
            "name": data.get("name"),
            "discovered_website": data.get("discovered_website"),
            "error": data.get("error"),
            # Committed proposals keep node status "ready" ON PURPOSE — the
            # two-step focus/all commit re-commits the same job — so the list
            # must carry the committed flag for the UI to tell them apart.
            "committed": data.get("committed"),
            # Resolved focused field (None = full enrichment). Carried so the
            # frontend can do scope-aware per-name dedupe (issue #102).
            "focus_key": data.get("focus_key"),
        }
    else:
        # Generic fallback for other job types (backfill/resolution/…): surface a
        # human label + any error so the shared activity page (#48) can list them.
        fields = {"name": data.get("name"), "error": data.get("error")}
    # Fields common to every job type for the activity page (#48/#49): a
    # human-readable `outcome` line runners set on completion, done/total progress
    # where a runner tracks it, and the raw `error_detail` (collapsed in the UI)
    # when a friendly error has a raw dump behind it. All null-pruned below, so a
    # job that carries none of them keeps the same compact summary as before.
    fields.update(
        outcome=data.get("outcome"),
        done=data.get("done"),
        total=data.get("total"),
        error_detail=data.get("error_detail"),
    )
    summary = {k: v for k, v in fields.items() if v is not None}
    return {
        "id": job_id,
        "type": job_type,
        "status": status,
        "createdAt": created_at,
        "summary": summary,
    }


async def list_jobs(
    driver: AsyncDriver,
    *,
    type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Recent Job nodes, newest first, with optional type/status filters and a
    limit. Returns the compact per-job summary (see `_job_summary`) — NOT the full
    dataJson, which can be large. Designed for reuse by the agent-activity page
    (#48) and the backlog page's research-activity rehydration (#66)."""
    # Superseded jobs (an errored proposal cleared by a scope-aware retry, #102)
    # are dropped for every caller — the stale card must not resurface anywhere.
    conditions: list[str] = ["coalesce(j.superseded, false) = false"]
    params: dict = {"limit": limit}
    if type:
        conditions.append("j.type = $type")
        params["type"] = type
    if status:
        conditions.append("j.status = $status")
        params["status"] = status
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = (
        f"MATCH (j:Job) {where} "
        "RETURN j.id AS id, j.type AS type, j.status AS status, "
        "j.dataJson AS data, toString(j.createdAt) AS createdAt "
        "ORDER BY j.createdAt DESC LIMIT $limit"
    )
    async with driver.session() as session:
        result = await session.run(query, **params)
        records = [rec async for rec in result]
    return [
        _job_summary(rec["id"], rec["type"], rec["status"], rec["createdAt"], rec["data"])
        for rec in records
    ]


async def execute_job(job_id: str) -> None:  # noqa: C901 — flat dispatch table, one branch per job type
    """Execute a job by dispatching to its type's runner."""
    job = await get_job(job_id)
    if job is None:
        return
    if job["type"] == "proposal":
        from app.agents.assistant.proposals import execute_proposal_job

        await execute_proposal_job(job_id)
    elif job["type"] == "backfill":
        from app.agents.assistant.backfill import execute_backfill_job

        await execute_backfill_job(job_id)
    elif job["type"] == "resolution":
        from app.agents.assistant.resolution import execute_resolution_job

        await execute_resolution_job(job_id)
    elif job["type"] == "classification":
        from app.agents.assistant.classification import execute_classification_job

        await execute_classification_job(job_id)
    elif job["type"] == "signal_capture":
        from app.capture.job import execute_signal_capture_job

        await execute_signal_capture_job(job_id)
    elif job["type"] == "news_capture":
        from app.capture.news import execute_news_capture_job

        await execute_news_capture_job(job_id)
    elif job["type"] == "discovery":
        from app.agents.discovery.discovery import execute_discovery_job

        await execute_discovery_job(job_id)
    elif job["type"] == "person_proposal":
        from app.agents.people.proposals import execute_person_proposal_job

        await execute_person_proposal_job(job_id)
    elif job["type"] == "acquisition_proposal":
        from app.agents.deals.proposals import execute_acquisition_proposal_job

        await execute_acquisition_proposal_job(job_id)
    elif job["type"] == "thesis_revision":
        from app.agents.deals.thesis_revision import execute_thesis_revision_job

        await execute_thesis_revision_job(job_id)
    elif job["type"] == "person_expertise":
        # Derived expertise summary (#42) — a same-layer graph module; lazy import
        # keeps it out of jobs.py's import graph (person_expertise imports jobs).
        from app.graph.person_expertise import execute_person_expertise_job

        await execute_person_expertise_job(job_id)
    else:
        # Periodic job types (Cloud Scheduler → schedule-tick) dispatch via the
        # schedule registry; late import avoids a cycle (schedules imports jobs).
        from app.graph import schedules

        if schedules.owns(job["type"]):
            await schedules.execute_scheduled(job_id, job["type"])


async def enqueue(job_id: str, delay: float = 0.0) -> None:
    """Trigger a created job. Inline locally; via Cloud Tasks in prod. A failed
    enqueue must not be silent: log it and mark the job errored so the UI shows it
    (rather than a proposal that hangs 'pending' forever).

    `delay` (seconds) staggers the start so a batch of jobs doesn't all fire at
    once and burn the same minute's Gemini quota (issue #65): Cloud Tasks gets a
    `schedule_time`; local mode sleeps before running the inline task."""
    if settings.job_mode != "cloudtasks":
        asyncio.create_task(_execute_after(job_id, delay))
        return
    try:
        await _enqueue_cloud_task(job_id, delay)
        logger.info("enqueued Cloud Task for job %s (delay %.1fs)", job_id, delay)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cloud Tasks enqueue failed for job %s", job_id)
        job = await get_job(job_id)
        if job is not None:
            await update_job(job_id, {**job, "error": f"could not start: {exc}"}, status="error")


async def _execute_after(job_id: str, delay: float) -> None:
    """Local-mode staggered start: wait `delay` seconds, then run the job inline."""
    if delay > 0:
        await asyncio.sleep(delay)
    await execute_job(job_id)


async def _enqueue_cloud_task(job_id: str, delay: float = 0.0) -> None:
    # Imported lazily so local/dev doesn't need the Cloud Tasks client.
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksAsyncClient()
    parent = client.queue_path(
        settings.gcp_project, settings.cloud_tasks_location, settings.cloud_tasks_queue
    )
    url = f"{settings.service_url}/jobs/run/{job_id}"
    logger.info("creating Cloud Task -> %s (queue %s)", url, parent)
    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "oidc_token": {"service_account_email": settings.tasks_service_account},
        }
    }
    if delay > 0:
        # Stagger the batch: tell Cloud Tasks not to dispatch until `delay` from now.
        from google.protobuf import timestamp_pb2

        schedule = timestamp_pb2.Timestamp()
        schedule.FromDatetime(datetime.now(timezone.utc) + timedelta(seconds=delay))
        task["schedule_time"] = schedule
    await client.create_task(parent=parent, task=task)
