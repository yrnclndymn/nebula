"""MCP server exposing the Nebula research graph.

Lets an MCP client (Claude Code, Claude Desktop, or any agent) query — and grow —
your company graph directly. Runs over stdio; register it in `.mcp.json`.

Course Day 2 (Tools & Interoperability): the same graph the FastAPI app and the
ADK agent use is now also an *interoperable* MCP tool surface. Read tools reuse
`app.graph.queries`; `enrich_company` reuses the ADK agent (a long-running tool).

Tools:
  search_companies   filtered list of researched companies
  get_company        one company with its relationships
  list_topics        research topics
  list_company_types company-type vocabulary
  graph_overview     node/edge counts
  run_cypher         ad-hoc READ-ONLY Cypher (guarded) for multi-hop exploration
  enrich_company     research a company with the agent and save it (slow; writes)

Note: the module is named `mcp_server`, not `mcp`, to avoid shadowing the `mcp`
package.
"""

from mcp.server.fastmcp import FastMCP

from app.agents.enrichment.enrich import enrich
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
async def enrich_company(name: str, website: str, topic: str = "AI-native engineering") -> str:
    """Research a company from its name + website using the Nebula agent and SAVE
    it to the graph. Slow (web search + fetch + LLM, ~30-60s) and writes data.
    Returns the agent's summary of what was saved."""
    return await enrich(name, website, topic, verbose=False)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
