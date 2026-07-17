"""Propose → review → commit for acquisition research (story #43).

Mirrors the person-enrichment machinery (``app.agents.people.proposals``): a
durable ``acquisition_proposal`` job researches a company's M&A history in the
background, stores the provenance-filtered proposed deals on the Job node (status
``ready``), and an explicit commit endpoint applies them. NOTHING writes an
:ACQUIRED edge except the commit step — the human-in-the-loop guarantee.

Durable (stored in the graph via ``app.graph.jobs``) so it survives Cloud Run
scale-to-zero, and budget-capped (``acquisition_research``) so a scheduled/ambient
caller can't run away with spend. The review surface is the API: the status
endpoint returns the proposed deals + their citations + a diff against what's
already stored; the commit endpoint applies them.
"""

import logging
import uuid

from app.agents.deals.build import build_acquisition_record, canonicalize_record, diff_acquisitions
from app.graph.deal_models import AcquisitionRecord
from app.agents.deals.research import research_acquisitions
from app.budget import budget_for, use_budget
from app.genai_retry import QuotaExhausted, run_with_quota_retry
from app.graph import jobs
from app.graph.acquisitions import canonical_names, get_acquisitions, upsert_acquisitions
from app.graph.driver import get_driver
from app.graph.queries import get_company

logger = logging.getLogger("nebula.ma.proposals")


async def propose_acquisitions(company: str, *, enqueue_delay: float = 0.0) -> dict:
    """Start researching a company's acquisition history in the BACKGROUND.

    Returns immediately with a job id to poll; nothing is written. 404-shaped
    ``error`` when the subject company isn't in the graph — M&A research extends a
    tracked company, it doesn't invent one (the acquirer/target *counterparties*,
    by contrast, are allowed to MERGE as stubs on commit and feed the backlog).
    """
    driver = get_driver()
    existing = await get_company(driver, company)
    if existing is None:
        return {"error": f"no company named {company!r} to research acquisitions for"}

    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "acquisition_proposal",
        {"job_id": job_id, "status": "pending", "company": company},
    )
    await jobs.enqueue(job_id, delay=enqueue_delay)
    return {"job_id": job_id, "company": company, "status": "researching in the background"}


async def execute_acquisition_proposal_job(job_id: str) -> None:
    """Job runner: research the company's deals (capture, don't write), filter to
    cited facts, and store the proposal on the job for review."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    run_budget = budget_for("acquisition_research", job.get("budget"))
    try:
        with use_budget(run_budget):
            research = await run_with_quota_retry(lambda: research_acquisitions(job["company"]))
        record = build_acquisition_record(research, job["company"])
        # Canonicalise counterparty names through the alias map BEFORE diffing —
        # stored edges are alias-resolved at write time, so diffing raw researched
        # names against them would show a repeat deal under a variant name as
        # "new" (PR #98 review finding). The stored record then carries canonical
        # names too, keeping the review surface and the eventual write consistent.
        names = [n for d in record.deals for n in (d.acquirer, d.target)]
        record = canonicalize_record(record, await canonical_names(driver, names))
        existing = await get_acquisitions(driver, job["company"])
        diff = diff_acquisitions(existing, record)
        await jobs.update_job(
            job_id,
            {
                **job,
                "record": record.model_dump(),
                "diff": diff,
                "outcome": (
                    f"proposal ready for {job['company']} ({len(diff)} new/changed deal(s))"
                    if record.has_facts()
                    else f"no cited acquisitions found for {job['company']}"
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


async def get_acquisition_proposal(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


# How many newest acquisition-proposal jobs the listing scans before giving up on
# finding `limit` uncommitted ones. Committed jobs accumulate forever in the store,
# so the scan window must comfortably exceed any realistic committed backlog.
_LIST_SCAN_CAP = 500


async def list_acquisition_proposals(company: str | None = None, *, limit: int = 50) -> list[dict]:
    """Read-only: acquisition proposals awaiting review (#133), newest first.

    The SPA review card lists these so a reviewer can open a proposal, check each
    deal's provenance, then commit or discard. Reuses the durable job store — NO
    writes and NO new commit path; the detail + citations come from
    :func:`get_acquisition_proposal` and the write stays the existing commit step.

    ``committed`` proposals are excluded (already reviewed); ``pending`` (still
    researching), ``ready`` (awaiting a decision) and ``error`` proposals are
    returned so the UI can show in-flight research and surface failures. Optional
    ``company`` narrows to one subject (the drawer's per-company section). Rows are
    compact — counts + the subject + status — with the full record fetched on demand.
    """
    driver = get_driver()
    # Over-fetch, then filter: committed proposals are never deleted (their status
    # stays "ready" by design; `committed` lives in the job data), so they'd
    # permanently occupy a Cypher-side LIMIT window and eventually push live
    # proposals out of it — the review card would silently self-hide. Newest-first
    # plus the early break below keeps the per-job fetches bounded in practice.
    summaries = await jobs.list_jobs(driver, type="acquisition_proposal", limit=_LIST_SCAN_CAP)
    rows: list[dict] = []
    for summary in summaries:
        if len(rows) >= limit:
            break
        job = await jobs.get_job(summary["id"])
        if job is None or job.get("committed"):
            continue
        subject = job.get("company")
        if company is not None and subject != company:
            continue
        deals = (job.get("record") or {}).get("deals") or []
        rows.append(
            {
                "job_id": summary["id"],
                "company": subject,
                "status": summary["status"],
                "deal_count": len(deals),
                "new_count": len(job.get("diff") or []),
                "outcome": job.get("outcome"),
                "error": job.get("error"),
                "committed": bool(job.get("committed")),
                "created_at": summary.get("createdAt"),
            }
        )
    return rows


async def commit_acquisition_proposal(job_id: str) -> dict:
    """Write a reviewed, ready acquisition proposal to the graph (the user's
    approval). Called by the UI/API, never by the agent. The stored record already
    carries only cited deals (and only cited amounts — provenance filtered at build
    time), so the write is safe. Flips job status to ``committed`` so it becomes
    prunable past retention (un-committed ``ready`` jobs are kept)."""
    job = await jobs.get_job(job_id)
    if job is None or job.get("status") != "ready":
        return {"error": "proposal not found or not ready"}

    record = AcquisitionRecord.model_validate(job["record"])
    if not record.has_facts():
        return {"error": "nothing to commit — no cited acquisitions in this proposal"}

    result = await upsert_acquisitions(get_driver(), record)
    await jobs.update_job(job_id, {**job, "committed": True}, status="committed")
    return {"committed": record.company, "deals": result.get("deals")}
