"""Read tools for the chat assistant — the same graph, exposed for conversation.

Thin ADK function tools over `app.graph.queries`. run_cypher is the flexible one
(the assistant writes read-only Cypher for specific/multi-hop questions);
search_companies and get_company cover the common cases.
"""

from app.graph import queries
from app.graph.driver import get_driver


async def search_companies(
    topic: str = "", text: str = "", company_type: str = "", headcount_max: int = 0
) -> list[dict]:
    """List researched companies. Empty strings and 0 mean "no filter". Returns a
    compact row per company (name, hq, headcount, topics, types, partner/client
    counts) — use get_company for full detail."""
    rows = await queries.list_companies(
        get_driver(),
        topic=topic or None,
        q=text or None,
        company_type=company_type or None,
        headcount_max=headcount_max or None,
    )
    return [
        {
            "name": r["name"],
            "hq": r.get("hqLocation"),
            "headcount": r.get("headcount"),
            "topics": r.get("topics"),
            "types": r.get("companyTypes"),
            "partners": r.get("partnerCount"),
            "clients": r.get("clientCount"),
        }
        for r in rows
    ]


async def run_cypher(query: str) -> list[dict]:
    """Run a READ-ONLY Cypher query (up to 200 rows) for specific or multi-hop
    questions. Write clauses are rejected.

    Schema: (:Company {name,website,hqLocation,headcount,yearFounded,funding,origin}),
    (:Person), (:Topic), (:CompanyType). Edges:
    (Company)-[:PARTNERS_WITH]-(Company), (Company)-[:HAS_CLIENT]->(Company),
    (Person)-[:LEADS {title}]->(Company), (Company)-[:TAGGED_AS]->(Topic),
    (Company)-[:CLASSIFIED_AS]->(CompanyType)."""
    return await queries.run_read_cypher(get_driver(), query)


async def get_company(name: str) -> dict:
    """Full detail for one company: properties, partners, clients, leadership, and
    cited sources. Returns an error dict if not found."""
    company = await queries.get_company(get_driver(), name)
    return company or {"error": f"No company named {name!r}"}
