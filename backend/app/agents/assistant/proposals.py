"""Propose → review → commit for chat-triggered enrichment.

The assistant can *propose* an enrichment but cannot *commit* it — committing is a
user action, keeping a human in the loop on every write.

Jobs are durable (stored in the graph, run via `app.graph.jobs`) so they survive
Cloud Run scale-to-zero: `propose_enrichment` creates a pending job and enqueues
it; the client polls `get_proposal` until ready; the user commits.
"""

import uuid
from contextvars import ContextVar

from app.agents.assistant.proposal_diff import (
    citation_matches_focus,
    compute_diff,
    field_label,
    resolve_focus,
)
from app.agents.assistant.reconcile import reconcile_people
from app.agents.enrichment.enrich import enrich
from app.graph import jobs, queries
from app.graph.driver import get_driver
from app.graph.models import Citation, CompanyRecord
from app.graph.repository import upsert_company
from app.tools.graph_tools import proposal_sink

# Proposals started during the current chat turn (read by the /chat endpoint).
turn_proposals: ContextVar[list | None] = ContextVar("nebula_turn_proposals", default=None)


async def propose_enrichment(
    name: str, website: str, topic: str = "AI-native engineering", focus: str = ""
) -> dict:
    """Start researching a company in the BACKGROUND to prepare a proposed graph
    update for the user to review. Returns immediately and does NOT save anything.
    Call it when the user asks to research, add, enrich, or update a company. After
    calling, tell the user you've STARTED researching and a proposal will appear
    shortly to review and commit — don't wait for it, and never claim you saved
    anything. If you don't have the website, ask first.

    focus: the SINGLE field the user asked about, if they named one — e.g.
    "headcount", "hq", "funding", "linkedin", "year founded", "revenue", "about".
    Leave it "" for a general "research/update <Company>" with no specific field.
    A focused proposal leads the review card with that field and commits just it."""
    proposal_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        proposal_id,
        "proposal",
        {
            "proposal_id": proposal_id,
            "status": "pending",
            "name": name,
            "website": website,
            "topic": topic,
            "focus": focus,
        },
    )
    await jobs.enqueue(proposal_id)

    collected = turn_proposals.get()
    if collected is not None:
        collected.append({"proposal_id": proposal_id, "status": "pending", "name": name})
    return {"proposal_id": proposal_id, "name": name, "status": "researching in the background"}


async def run_proposal_job(proposal_id: str) -> None:
    """Job runner: research the company (capture, don't write) and fill in the job."""
    job = await jobs.get_job(proposal_id)
    if job is None:
        return
    sink: list = []
    token = proposal_sink.set(sink)
    try:
        result = await enrich(job["name"], job["website"], job["topic"], verbose=False)
        if not sink:
            await jobs.update_job(
                proposal_id, {**job, "error": "no record produced"}, status="error"
            )
            return
        record = sink[-1]
        existing = await queries.get_company(get_driver(), record["name"])
        # Reconcile proposed leaders against existing people so name-variants
        # (Andy/Andrew) merge instead of creating duplicate :Person nodes; the
        # record we store writes the reconciled (deduped) leadership.
        leadership = reconcile_people(
            existing["leadership"] if existing else [], record.get("leadership", [])
        )
        record["leadership"] = leadership["reconciled"]
        focus_key = resolve_focus(job.get("focus"))
        diff = compute_diff(existing, record, leadership)
        await jobs.update_job(
            proposal_id,
            {
                **job,
                "name": record["name"],
                "exists": existing is not None,
                "summary": result.summary,
                "record": record,
                "focus_key": focus_key,
                "focus_label": field_label(focus_key) if focus_key else "",
                "diff": diff,
            },
            status="ready",
        )
    except Exception as exc:  # noqa: BLE001 — surface research failures to the client
        await jobs.update_job(proposal_id, {**job, "error": str(exc)}, status="error")
    finally:
        proposal_sink.reset(token)


async def get_proposal(proposal_id: str) -> dict | None:
    return await jobs.get_job(proposal_id)


def _focus_record(job: dict) -> CompanyRecord | None:
    """A minimal record that writes ONLY the focused field (+ its citations), so a
    "just update the headcount" request never rewrites HQ, about, etc."""
    focus_key = job.get("focus_key")
    stored = job["record"]
    value = stored.get(focus_key) if focus_key else None
    if focus_key is None or value in (None, "", 0):
        return None
    citations = [
        Citation(**c)
        for c in stored.get("citations", [])
        if citation_matches_focus(c.get("field"), focus_key)
    ]
    return CompanyRecord(
        name=stored["name"], origin="agent", citations=citations, **{focus_key: value}
    )


async def commit_proposal(proposal_id: str, scope: str = "all") -> dict:
    """Write a reviewed, ready proposal to the graph. Called by the UI, not the agent.

    scope="focus" writes only the field the user asked about (used by focused
    proposals); scope="all" writes the full reconciled record. Both are idempotent,
    so committing focus then "all" is safe."""
    job = await jobs.get_job(proposal_id)
    if job is None or job.get("status") != "ready":
        return {"error": "proposal not found or not ready"}

    if scope == "focus":
        record = _focus_record(job)
        if record is None:
            return {"error": "no value found for the focused field"}
    else:
        record = CompanyRecord.model_validate(job["record"])

    await upsert_company(get_driver(), record)
    committed = set(job.get("committed_scopes", []))
    committed.add(scope)
    await jobs.update_job(
        proposal_id, {**job, "committed": True, "committed_scopes": sorted(committed)}
    )
    return {"committed": record.name, "scope": scope}
