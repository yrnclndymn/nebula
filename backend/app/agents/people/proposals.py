"""Propose → review → commit for person enrichment (story #40).

Mirrors the company proposal machinery (``app.agents.assistant.proposals``): a
durable ``person_proposal`` job researches a person in the background, stores the
provenance-filtered proposed facts on the Job node (status ``ready``), and an
explicit commit endpoint applies them. NOTHING writes to a :Person except the
commit step — the human-in-the-loop guarantee.

Durable (stored in the graph via ``app.graph.jobs``) so it survives Cloud Run
scale-to-zero, and budget-capped (``person_enrichment``) so a scheduled/ambient
caller can't run away with spend. The review surface is the API: the status
endpoint returns the proposed facts + their citations + a diff against what's
already stored; the commit endpoint applies them.
"""

import logging
import uuid

from app.agents.people.build import build_person_record, diff_person
from app.agents.people.models import PersonRecord
from app.agents.people.research import research_person
from app.budget import budget_for, use_budget
from app.genai_retry import QuotaExhausted, run_with_quota_retry
from app.graph import jobs
from app.graph.driver import get_driver
from app.graph.person_enrichment import get_person_scoped, upsert_person

logger = logging.getLogger("nebula.people.proposals")


async def propose_person(
    name: str, company: str, *, focus: str = "", enqueue_delay: float = 0.0
) -> dict:
    """Start researching a person in the BACKGROUND to prepare a reviewable update.

    ``company`` scopes which person (a company they lead in the graph) so we never
    key on a bare global name (#87). Returns immediately with a job id to poll;
    nothing is written. 404-shaped ``error`` when no such person leads that company
    — enrichment extends an existing graph person, it doesn't invent one.
    """
    driver = get_driver()
    existing = await get_person_scoped(driver, name, company)
    if existing is None:
        return {"error": f"no person named {name!r} leading {company!r} to enrich"}

    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "person_proposal",
        {
            "job_id": job_id,
            "status": "pending",
            "name": name,
            "company": company,
            "focus": focus,
            # Seed the research with the person's already-known profile URL, if any.
            "linkedin": existing.get("linkedin"),
        },
    )
    await jobs.enqueue(job_id, delay=enqueue_delay)
    return {"job_id": job_id, "name": name, "status": "researching in the background"}


async def run_person_proposal_job(job_id: str) -> None:
    """Job runner: research the person (capture, don't write), filter to cited
    facts, and store the proposal on the job for review."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    run_budget = budget_for("person_enrichment", job.get("budget"))
    try:
        with use_budget(run_budget):
            research = await run_with_quota_retry(
                lambda: research_person(job["name"], job["company"], linkedin=job.get("linkedin"))
            )
        record = build_person_record(research, job["company"])
        existing = await get_person_scoped(driver, job["name"], job["company"])
        diff = diff_person(existing, record)
        await jobs.update_job(
            job_id,
            {
                **job,
                "record": record.model_dump(),
                "diff": diff,
                "exists": existing is not None,
                "outcome": (
                    f"proposal ready for {job['name']} ({len(diff)} change(s))"
                    if record.has_facts()
                    else f"no cited facts found for {job['name']}"
                ),
            },
            status="ready",
        )
    except QuotaExhausted as exc:
        await jobs.update_job(
            job_id,
            {**job, "error": exc.message, "error_detail": exc.detail},
            status="error",
        )
    except Exception as exc:  # noqa: BLE001 — surface research failures on the job
        await jobs.update_job(job_id, {**job, "error": str(exc)}, status="error")


async def get_person_proposal(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_person_proposal(job_id: str) -> dict:
    """Write a reviewed, ready person proposal to the graph (the user's approval).

    Called by the UI/API, never by the agent. The stored record already carries
    only cited facts (provenance filtered at build time), so the write is safe.
    Flips job status to ``committed`` — like the resolution/classification jobs —
    so it becomes prunable past retention (un-committed ``ready`` jobs are kept).
    """
    job = await jobs.get_job(job_id)
    if job is None or job.get("status") != "ready":
        return {"error": "proposal not found or not ready"}

    record = PersonRecord.model_validate(job["record"])
    if not record.has_facts():
        return {"error": "nothing to commit — no cited facts in this proposal"}

    result = await upsert_person(get_driver(), record)
    await jobs.update_job(job_id, {**job, "committed": True}, status="committed")
    return {"committed": record.name, "action": result.get("action")}
