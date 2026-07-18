"""Chat-triggered acquisition-research proposals (propose → review → commit, #126).

Observed in prod: asked to record "Acme acquired Globex", the assistant found both
companies but had no acquisition verb, so it offered workarounds — writing the
`about` field, misusing PARTNERS_WITH/HAS_CLIENT, or a custom "Acquisitions"
column. All three misrecord a fact the graph already models properly: a
first-class :ACQUIRED edge with the #43 propose→review→commit flow.

This tool is that missing verb. It delegates to the SAME background proposal the
API and SPA M&A view start (`app.agents.deals.proposals.propose_acquisitions`), so
the deal is researched and provenance-filtered (uncited amounts dropped) before
any human commit writes the edge. Nothing is written here — the assistant can
NEVER write an acquisition directly; a user's asserted deal is a research LEAD, not
a fact. The proposal surfaces in the same review path as the API-initiated ones.
"""

from contextvars import ContextVar

from app.agents.deals import proposals as deal_proposals

# Acquisition proposals started during the current chat turn (read by the /chat
# endpoint) so the review card surfaces inline in chat, the way turn_backfills /
# turn_merges do for their flows.
turn_acquisitions: ContextVar[list | None] = ContextVar("nebula_turn_acquisitions", default=None)


async def propose_acquisitions(company: str) -> dict:
    """Start researching a company's acquisition history to prepare a reviewable
    proposal. Call this when the user reports or asks to record an acquisition —
    "Acme acquired Globex", "record that X bought Y", "add X's acquisition of Y",
    "who has X acquired?".

    Pass company = a TRACKED company in the graph whose M&A history to research (the
    acquirer OR the target — research gathers deals it made AND deals where it was
    acquired). If the user asserts a specific deal, treat that assertion as a
    research LEAD, not a fact: the proposal verifies it against cited sources rather
    than trusting chat input, and drops any uncited amount.

    Returns immediately with a job id and does NOT write anything — only the user's
    commit on the review card writes the :ACQUIRED edge (human-in-the-loop). After
    calling, tell the user you've STARTED researching and the live review card is
    right here in the chat (it fills in as research finishes; commit or discard it
    there) — never claim you recorded, saved, or added an acquisition. Never record
    an acquisition via the about field, a partner/client edge, or a custom column;
    this proposal flow is the only correct path. Returns an ``error`` instead when
    the company isn't tracked yet — relay that (check the exact name) rather than
    claiming research started.
    """
    result = await deal_proposals.propose_acquisitions(company)
    # Surface a review card inline in chat: on a real proposal (not a 404-shaped
    # error), append its ref to the per-turn collector the /chat endpoint returns.
    if "error" not in result:
        collected = turn_acquisitions.get()
        if collected is not None:
            collected.append({"job_id": result["job_id"], "company": result["company"]})
    return result
