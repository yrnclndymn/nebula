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

from app.agents.deals import proposals as deal_proposals


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
    calling, tell the user you've STARTED researching and a proposal will appear for
    review — never claim you recorded, saved, or added an acquisition. Never record
    an acquisition via the about field, a partner/client edge, or a custom column;
    this proposal flow is the only correct path. Returns an ``error`` instead when
    the company isn't tracked yet — relay that (check the exact name) rather than
    claiming research started.
    """
    return await deal_proposals.propose_acquisitions(company)
