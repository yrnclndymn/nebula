"""Propose → review → commit for chat-triggered enrichment.

The assistant can *propose* an enrichment but cannot *commit* it — committing is a
user action, keeping a human in the loop on every write.

Jobs are durable (stored in the graph, run via `app.graph.jobs`) so they survive
Cloud Run scale-to-zero: `propose_enrichment` creates a pending job and enqueues
it; the client polls `get_proposal` until ready; the user commits.
"""

import uuid
from contextvars import ContextVar

from app.agents.enrichment.enrich import enrich
from app.graph import jobs, queries
from app.graph.driver import get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.tools.graph_tools import proposal_sink

# Proposals started during the current chat turn (read by the /chat endpoint).
turn_proposals: ContextVar[list | None] = ContextVar("nebula_turn_proposals", default=None)


async def propose_enrichment(name: str, website: str, topic: str = "AI-native engineering") -> dict:
    """Start researching a company in the BACKGROUND to prepare a proposed graph
    update for the user to review. Returns immediately and does NOT save anything.
    Call it when the user asks to research, add, enrich, or update a company. After
    calling, tell the user you've STARTED researching and a proposal will appear
    shortly to review and commit — don't wait for it, and never claim you saved
    anything. If you don't have the website, ask first."""
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
        await jobs.update_job(
            proposal_id,
            {
                **job,
                "name": record["name"],
                "exists": existing is not None,
                "summary": result.summary,
                "record": record,
            },
            status="ready",
        )
    except Exception as exc:  # noqa: BLE001 — surface research failures to the client
        await jobs.update_job(proposal_id, {**job, "error": str(exc)}, status="error")
    finally:
        proposal_sink.reset(token)


async def get_proposal(proposal_id: str) -> dict | None:
    return await jobs.get_job(proposal_id)


async def commit_proposal(proposal_id: str) -> dict:
    """Write a reviewed, ready proposal to the graph. Called by the UI, not the agent."""
    job = await jobs.get_job(proposal_id)
    if job is None or job.get("status") != "ready":
        return {"error": "proposal not found or not ready"}
    record = CompanyRecord.model_validate(job["record"])
    await upsert_company(get_driver(), record)
    await jobs.update_job(proposal_id, {**job, "committed": True})
    return {"committed": record.name}
