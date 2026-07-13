"""MCP server exposing the Nebula research graph.

Lets an MCP client (Claude Code, Claude Desktop, or any agent) query — and grow —
your company graph directly. Runs over stdio; register it in `.mcp.json`.

Course Day 2 (Tools & Interoperability): the same graph the FastAPI app and the
ADK agent use is now also an *interoperable* MCP tool surface. Read tools reuse
`app.graph.queries`; enrichment reuses the assistant's propose→review→commit flow
(`app.agents.assistant.proposals`) so an MCP write stays human-in-the-loop, exactly
like a chat-initiated one — no MCP client gets a silent direct-write to the graph.

Tools:
  search_companies   filtered list of researched companies
  get_company        one company with its relationships
  list_topics        research topics
  list_company_types company-type vocabulary
  graph_overview     node/edge counts
  run_cypher         ad-hoc READ-ONLY Cypher (guarded) for multi-hop exploration
  enrich_company     research a company → create a PROPOSAL to review (does not write)
  proposal_status    poll a proposal until it's ready to review
  commit_proposal    write a reviewed proposal to the graph (explicit id required)

Note: the module is named `mcp_server`, not `mcp`, to avoid shadowing the `mcp`
package.
"""

from mcp.server.fastmcp import FastMCP

from app.agents.assistant import proposals
from app.agents.enrichment.enrich import enrich
from app.config import settings
from app.graph import queries
from app.graph.driver import get_driver

mcp = FastMCP("nebula")


@mcp.tool()
async def search_companies(
    topic: str | None = None,
    query: str | None = None,
    company_type: str | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
) -> list[dict]:
    """Search researched companies. All filters optional: topic, free-text query
    (name/description), company_type (e.g. B-Corp/ESOP), and a headcount range.
    Returns each company's fields plus partner/client/leader counts."""
    return await queries.list_companies(
        get_driver(),
        topic=topic,
        q=query,
        company_type=company_type,
        headcount_min=headcount_min,
        headcount_max=headcount_max,
    )


@mcp.tool()
async def get_company(name: str) -> dict:
    """Full detail for one company: properties plus partners, clients, and
    leadership (with titles). Returns an error dict if not found."""
    company = await queries.get_company(get_driver(), name)
    return company or {"error": f"No company named {name!r}"}


@mcp.tool()
async def list_topics() -> list[str]:
    """List the research topics (e.g. 'AI-native engineering', 'SAP ecosystem')."""
    return await queries.list_topics(get_driver())


@mcp.tool()
async def list_company_types() -> list[str]:
    """List the company-type vocabulary present in the graph."""
    return await queries.list_company_types(get_driver())


@mcp.tool()
async def graph_overview() -> dict:
    """Node and edge counts — a quick orientation of what's in the graph."""
    return await queries.graph_overview(get_driver())


@mcp.tool()
async def run_cypher(query: str) -> list[dict]:
    """Run a READ-ONLY Cypher query for ad-hoc multi-hop exploration and return up
    to 200 rows. Write clauses (CREATE/MERGE/DELETE/SET/…) are rejected.

    Schema: (:Company {name,website,hqLocation,headcount,yearFounded,funding,...}),
    (:Person), (:Topic), (:CompanyType). Edges: (Company)-[:PARTNERS_WITH]-(Company),
    (Company)-[:HAS_CLIENT]->(Company), (Person)-[:LEADS {title}]->(Company),
    (Company)-[:TAGGED_AS]->(Topic), (Company)-[:CLASSIFIED_AS]->(CompanyType)."""
    return await queries.run_read_cypher(get_driver(), query)


@mcp.tool()
async def enrich_company(name: str, website: str, topic: str = "AI-native engineering") -> dict:
    """Research a company from its name + website and PROPOSE a graph update for a
    human to review — this does NOT write to the graph. Returns a proposal id and a
    status; the research (web search + fetch + LLM, ~30-60s) runs in the background
    as a durable job. Poll `proposal_status(proposal_id)` until it's 'ready' (or
    'error'), then either review and commit it in the Nebula SPA or call
    `commit_proposal(proposal_id)`. This keeps a human in the loop on every write,
    exactly like the chat assistant's propose→review→commit flow.

    (A legacy direct-write mode — save straight to the graph with no review — exists
    only when the server is started with MCP_ENRICH_DIRECT_WRITE explicitly enabled;
    it is OFF by default.)"""
    if settings.mcp_enrich_direct_write:
        # Opt-in escape hatch (defaults OFF, see app/config.py): restore the old
        # bypass-review behaviour of saving straight to the graph.
        result = await enrich(name, website, topic, verbose=False)
        return {"written": name, "summary": result.summary, "review": False}
    started = await proposals.propose_enrichment(name, website, topic)
    return {
        "proposal_id": started["proposal_id"],
        "name": started["name"],
        "status": started["status"],
        "review": True,
        "next": "poll proposal_status(proposal_id); then commit in the SPA "
        "or via commit_proposal(proposal_id)",
    }


@mcp.tool()
async def proposal_status(proposal_id: str) -> dict:
    """Check a background enrichment proposal created by `enrich_company`. Returns
    its status ('pending' = still researching, 'ready' = reviewable, 'error'), plus
    the research summary and the field-by-field diff once ready, and whether it has
    already been committed. Poll until status is 'ready' or 'error'."""
    job = await proposals.get_proposal(proposal_id)
    if job is None:
        return {"error": f"unknown proposal {proposal_id!r}"}
    view = {
        "proposal_id": proposal_id,
        "status": job.get("status"),
        "name": job.get("name"),
        "summary": job.get("summary"),
        "diff": job.get("diff"),
        "error": job.get("error"),
        "committed": job.get("committed", False),
    }
    # Drop null fields (e.g. no diff/summary yet while pending) to keep it compact.
    return {k: v for k, v in view.items() if v is not None}


@mcp.tool()
async def commit_proposal(proposal_id: str, scope: str = "all") -> dict:
    """Commit a REVIEWED enrichment proposal to the graph — the human-in-the-loop
    approval step. Requires the proposal_id returned by `enrich_company`; only a
    proposal whose status is 'ready' is committed (never auto-committed, and an
    unknown or not-yet-ready id is refused). scope 'all' writes the full researched
    record; 'focus' writes just the single asked-about field. Review the diff via
    `proposal_status` first. Returns what was written, or an error."""
    if not proposal_id or not proposal_id.strip():
        return {"error": "a proposal_id is required"}
    return await proposals.commit_proposal(proposal_id.strip(), scope)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
