"""Propose → review → commit for chat-triggered enrichment.

The assistant can *propose* an enrichment but cannot *commit* it — committing is a
user action via the UI, keeping a human in the loop on every write.

Research is long (crawl + vision + LLM, sometimes minutes with rate limits), so it
runs in the BACKGROUND: `propose_enrichment` starts a task and returns immediately
with a pending proposal id; the client polls `get_proposal` until it's ready, then
the user commits. This keeps the /chat request fast (no fetch timeouts).
"""

import asyncio
import uuid
from contextvars import ContextVar

from app.agents.enrichment.enrich import enrich
from app.graph import queries
from app.graph.driver import get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.tools.graph_tools import proposal_sink

# proposal_id -> {proposal_id, status: pending|ready|error, name, record?, ...}
PROPOSALS: dict[str, dict] = {}

# Proposals started during the current chat turn (read by the /chat endpoint).
turn_proposals: ContextVar[list | None] = ContextVar("nebula_turn_proposals", default=None)


async def _run_proposal(proposal_id: str, name: str, website: str, topic: str) -> None:
    """Background worker: research the company (capture, don't write) and fill in
    the proposal."""
    sink: list = []
    token = proposal_sink.set(sink)
    try:
        result = await enrich(name, website, topic, verbose=False)
        if not sink:
            PROPOSALS[proposal_id].update(status="error", error="no record produced")
            return
        record = sink[-1]
        existing = await queries.get_company(get_driver(), record["name"])
        PROPOSALS[proposal_id].update(
            status="ready",
            name=record["name"],
            exists=existing is not None,
            summary=result.summary,
            record=record,
        )
    except Exception as exc:  # noqa: BLE001 — surface research failures to the client
        PROPOSALS[proposal_id].update(status="error", error=str(exc))
    finally:
        proposal_sink.reset(token)


async def propose_enrichment(name: str, website: str, topic: str = "AI-native engineering") -> dict:
    """Start researching a company in the BACKGROUND to prepare a proposed graph
    update for the user to review. This returns immediately and does NOT save
    anything. Call it when the user asks to research, add, enrich, or update a
    company. After calling, tell the user you've STARTED researching and a proposal
    will appear shortly for them to review and commit — do not claim you saved
    anything, and don't wait for it. If you don't have the website, ask first."""
    proposal_id = uuid.uuid4().hex[:8]
    PROPOSALS[proposal_id] = {"proposal_id": proposal_id, "status": "pending", "name": name}
    asyncio.create_task(_run_proposal(proposal_id, name, website, topic))

    collected = turn_proposals.get()
    if collected is not None:
        collected.append({"proposal_id": proposal_id, "status": "pending", "name": name})

    return {"proposal_id": proposal_id, "name": name, "status": "researching in the background"}


def get_proposal(proposal_id: str) -> dict | None:
    """Current state of a proposal (for the client to poll)."""
    return PROPOSALS.get(proposal_id)


async def commit_proposal(proposal_id: str) -> dict:
    """Write a reviewed, ready proposal to the graph. Called by the UI, not the agent."""
    proposal = PROPOSALS.get(proposal_id)
    if proposal is None or proposal.get("status") != "ready":
        return {"error": "proposal not found or not ready"}
    record = CompanyRecord.model_validate(proposal["record"])
    await upsert_company(get_driver(), record)
    proposal["committed"] = True
    return {"committed": record.name}
