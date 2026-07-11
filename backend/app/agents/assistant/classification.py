"""Client-kind classification review: propose end-customer stubs, let a human commit.

Many stub companies are end-customer organisations (banks, retailers, public
bodies) pulled in only via HAS_CLIENT — not ecosystem players worth researching.
This surfaces them for bulk labelling as kind='client' behind the same
propose→review→commit durable-job pattern as entity resolution and back-fill, so
it survives Cloud Run scale-to-zero and never writes silently.

`start_classification` creates a job and enqueues it; the runner scans stubs with
the client heuristic (`entity_resolution.list_client_stub_candidates`) and stores
the proposed candidates; the user reviews and commits an approved subset of names,
which is the only thing that touches the graph. The heuristic only proposes — a
company that is genuinely both (e.g. a cloud provider that is also someone's
client) already carries an ecosystem kind and is never a candidate.
"""

import uuid

from app.graph import entity_resolution as er
from app.graph import jobs
from app.graph.driver import get_driver


async def start_classification() -> dict:
    """Kick off a background scan for client-kind candidates and prepare a
    reviewable batch. Returns immediately; the scan runs as a durable job."""
    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "classification",
        {"job_id": job_id, "status": "pending", "candidates": [], "stub_count": 0},
    )
    await jobs.enqueue(job_id)
    return {"job_id": job_id, "status": "scanning in the background"}


async def run_classification_job(job_id: str) -> None:
    """Job runner: scan stubs, propose client-kind candidates, mark ready."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    try:
        candidates = await er.list_client_stub_candidates(driver)
        await jobs.update_job(
            job_id,
            {**job, "candidates": candidates, "stub_count": len(candidates)},
            status="ready",
        )
    except Exception as exc:  # noqa: BLE001 — surface scan failures to the client
        await jobs.update_job(job_id, {**job, "error": str(exc)}, status="error")


async def get_classification(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_classification(job_id: str, names: list[str]) -> dict:
    """Apply kind='client' to the user-approved names. Called by the UI, not the agent.

    Only names the reviewer approved are written; the graph mutation re-checks each
    is still an unclassified stub (so a company promoted since the scan is skipped,
    not mislabelled). Returns the count actually classified.
    """
    job = await jobs.get_job(job_id)
    if job is None or job.get("status") != "ready":
        return {"error": "classification job not found or not ready"}
    driver = get_driver()

    classified = await er.classify_as_client(driver, names)

    # Flip status so a stale double-POST is rejected by the ready-guard above.
    await jobs.update_job(job_id, {**job, "committed": True}, status="committed")
    return {"classified": classified}
