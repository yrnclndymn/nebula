"""Client-kind classification review: propose end-customer stubs, let a human commit.

Many stub companies are end-customer organisations (banks, retailers, public
bodies) pulled in only via HAS_CLIENT — not ecosystem players worth researching.
This surfaces them for bulk labelling as kind='client' behind the same
propose→review→commit durable-job pattern as entity resolution and back-fill
(the enqueue/execute/commit ceremony lives in `app.graph.jobs`), so it survives
Cloud Run scale-to-zero and never writes silently.

`enqueue_classification` creates a job and enqueues it; the runner scans stubs with
the client heuristic (`entity_resolution.list_client_stub_candidates`) and stores
the proposed candidates; the user reviews and commits an approved subset of names,
which is the only thing that touches the graph. The heuristic only proposes — a
company that is genuinely both (e.g. a cloud provider that is also someone's
client) already carries an ecosystem kind and is never a candidate.
"""

from app.graph import entity_resolution as er
from app.graph import jobs
from app.graph.driver import get_driver


async def enqueue_classification() -> dict:
    """Kick off a background scan for client-kind candidates and prepare a
    reviewable batch. Returns immediately; the scan runs as a durable job."""
    return await jobs.enqueue_scan_job("classification", {"candidates": [], "stub_count": 0})


async def execute_classification_job(job_id: str) -> None:
    """Job runner: scan stubs, propose client-kind candidates, mark ready."""

    async def scan(_job: dict) -> dict:
        candidates = await er.list_client_stub_candidates(get_driver())
        return {"candidates": candidates, "stub_count": len(candidates)}

    await jobs.execute_scan_job(job_id, scan)


async def get_classification(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_classification(job_id: str, names: list[str]) -> dict:
    """Apply kind='client' to the user-approved names. Called by the UI, not the agent.

    Only names the reviewer approved are written; the graph mutation re-checks each
    is still an unclassified stub (so a company promoted since the scan is skipped,
    not mislabelled). Returns the count actually classified.
    """
    job = await jobs.get_ready_job(job_id)
    if job is None:
        return {"error": "classification job not found or not ready"}

    classified = await er.classify_as_client(get_driver(), names)

    await jobs.mark_committed(job_id, job)
    return {"classified": classified}
