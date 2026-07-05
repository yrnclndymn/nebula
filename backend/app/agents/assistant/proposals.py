"""Propose → review → commit for chat-triggered enrichment.

The assistant can *propose* an enrichment (research a company, capture what it
would write) but cannot *commit* it — committing is a user action via the UI. This
keeps a human in the loop on every write the chat agent initiates.

`propose_enrichment` runs the enrichment agent with `proposal_sink` set, so its
save_company captures the record instead of writing. The proposal is stashed in
PROPOSALS and surfaced to the current chat turn via `turn_proposals`; the /chat
endpoint returns it for review, and /proposals/commit writes it.
"""

import uuid
from contextvars import ContextVar

from app.agents.enrichment.enrich import enrich
from app.graph import queries
from app.graph.driver import get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.tools.graph_tools import proposal_sink

# proposal_id -> proposal dict. In-memory; fine for a single-user local tool.
PROPOSALS: dict[str, dict] = {}

# Proposals created during the current chat turn (read by the /chat endpoint).
turn_proposals: ContextVar[list | None] = ContextVar("nebula_turn_proposals", default=None)


async def propose_enrichment(name: str, website: str, topic: str = "AI-native engineering") -> dict:
    """Research a company from the web and PREPARE a proposed graph update for the
    user to review. This does NOT save anything — it only prepares a proposal the
    user must approve. Call this when the user asks to research, add, enrich, or
    update a company. After calling it, tell the user you've prepared a proposal
    for them to review and commit (do not claim you saved anything)."""
    sink: list = []
    token = proposal_sink.set(sink)
    try:
        result = await enrich(name, website, topic, verbose=False)
    finally:
        proposal_sink.reset(token)

    if not sink:
        return {"error": f"Could not research {name!r}; no record was produced."}

    record = sink[-1]
    existing = await queries.get_company(get_driver(), record["name"])
    proposal_id = uuid.uuid4().hex[:8]
    proposal = {
        "proposal_id": proposal_id,
        "name": record["name"],
        "exists": existing is not None,
        "summary": result.summary,
        "record": record,
    }
    PROPOSALS[proposal_id] = proposal

    collected = turn_proposals.get()
    if collected is not None:
        collected.append(proposal)

    return {
        "proposal_id": proposal_id,
        "name": record["name"],
        "will": "update existing company" if existing else "create new company",
        "status": "proposed — shown to the user for review; awaiting their commit",
    }


async def commit_proposal(proposal_id: str) -> dict:
    """Write a previously proposed record to the graph. Called by the UI, not the agent."""
    proposal = PROPOSALS.get(proposal_id)
    if proposal is None:
        return {"error": "unknown or expired proposal"}
    record = CompanyRecord.model_validate(proposal["record"])
    await upsert_company(get_driver(), record)
    proposal["committed"] = True
    return {"committed": record.name}
