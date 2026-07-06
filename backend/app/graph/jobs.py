"""Durable background jobs, stored in the graph so they survive Cloud Run
scale-to-zero (in-memory dicts + fire-and-forget tasks would not).

A job is a `(:Job {id, type, status, dataJson})` node whose `dataJson` is the
job's payload/result. `enqueue` triggers the work:
- "local"      → run inline via an asyncio task (dev: one long-lived process).
- "cloudtasks" → enqueue a Cloud Task that POSTs /jobs/run/{id}, which runs it in
                 a request that has CPU (survives scale-to-zero).

Both paths converge on `run_job`, which dispatches to the type's runner (late
imports avoid an import cycle). Poll/commit read the job from the graph.
"""

import asyncio
import json

from app.config import settings
from app.graph.driver import get_driver


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


async def run_job(job_id: str) -> None:
    """Execute a job by dispatching to its type's runner."""
    job = await get_job(job_id)
    if job is None:
        return
    if job["type"] == "proposal":
        from app.agents.assistant.proposals import run_proposal_job

        await run_proposal_job(job_id)
    elif job["type"] == "backfill":
        from app.agents.assistant.backfill import run_backfill_job

        await run_backfill_job(job_id)


async def enqueue(job_id: str) -> None:
    """Trigger a created job. Inline locally; via Cloud Tasks in prod."""
    if settings.job_mode == "cloudtasks":
        await _enqueue_cloud_task(job_id)
    else:
        asyncio.create_task(run_job(job_id))


async def _enqueue_cloud_task(job_id: str) -> None:
    # Imported lazily so local/dev doesn't need the Cloud Tasks client.
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksAsyncClient()
    parent = client.queue_path(
        settings.gcp_project, settings.cloud_tasks_location, settings.cloud_tasks_queue
    )
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.service_url}/jobs/run/{job_id}",
            "oidc_token": {"service_account_email": settings.tasks_service_account},
        }
    }
    await client.create_task(parent=parent, task=task)
