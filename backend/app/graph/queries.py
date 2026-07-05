"""Read queries for the API. Returns plain dicts/lists ready for JSON.

The table UI works off "researched" companies — those with a TAGGED_AS edge to a
topic — which distinguishes them from the partner/client stub nodes pulled in by
relationships. Aggregates (partner/client/leader counts) come back per row so the
table can show connection density without a second round-trip.
"""

import re

from neo4j import AsyncDriver

_COMPANY_PROPS = (
    "c{.name,.priority,.about,.website,.linkedin,.hqLocation,.headcount,"
    ".estimatedRevenue,.yearFounded,.funding,.notes,.origin,.kind}"
)


async def list_companies(
    driver: AsyncDriver,
    *,
    topic: str | None = None,
    q: str | None = None,
    company_type: str | None = None,
    kind: str | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
) -> list[dict]:
    conditions: list[str] = []
    params: dict = {}
    if topic:
        conditions.append("t0.name = $topic")
        params["topic"] = topic
    if kind:
        conditions.append("c.kind = $kind")
        params["kind"] = kind
    if q:
        conditions.append(
            "(toLower(c.name) CONTAINS toLower($q) OR toLower(coalesce(c.about,'')) CONTAINS toLower($q))"
        )
        params["q"] = q
    if company_type:
        conditions.append("EXISTS { (c)-[:CLASSIFIED_AS]->(:CompanyType {name: $companyType}) }")
        params["companyType"] = company_type
    if headcount_min is not None:
        conditions.append("c.headcount >= $hmin")
        params["hmin"] = headcount_min
    if headcount_max is not None:
        conditions.append("c.headcount <= $hmax")
        params["hmax"] = headcount_max

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cypher = f"""
        MATCH (c:Company)-[:TAGGED_AS]->(t0:Topic)
        {where}
        WITH DISTINCT c
        OPTIONAL MATCH (c)-[:TAGGED_AS]->(t:Topic)
        OPTIONAL MATCH (c)-[:CLASSIFIED_AS]->(ct:CompanyType)
        OPTIONAL MATCH (c)-[:PARTNERS_WITH]-(p:Company)
        OPTIONAL MATCH (c)-[:HAS_CLIENT]->(cl:Company)
        OPTIONAL MATCH (pe:Person)-[:LEADS]->(c)
        RETURN {_COMPANY_PROPS} AS company,
               collect(DISTINCT t.name) AS topics,
               collect(DISTINCT ct.name) AS companyTypes,
               count(DISTINCT p) AS partnerCount,
               count(DISTINCT cl) AS clientCount,
               count(DISTINCT pe) AS leaderCount
        ORDER BY toLower(company.name)
    """
    async with driver.session() as session:
        result = await session.run(cypher, **params)
        rows = [record.data() async for record in result]
    # Flatten {company:{...}, topics, ...} into one dict per row.
    return [
        {
            **r["company"],
            "topics": r["topics"],
            "companyTypes": r["companyTypes"],
            "partnerCount": r["partnerCount"],
            "clientCount": r["clientCount"],
            "leaderCount": r["leaderCount"],
        }
        for r in rows
    ]


async def get_company(driver: AsyncDriver, name: str) -> dict | None:
    cypher = f"""
        MATCH (c:Company {{name: $name}})
        OPTIONAL MATCH (c)-[:TAGGED_AS]->(t:Topic)
        OPTIONAL MATCH (c)-[:CLASSIFIED_AS]->(ct:CompanyType)
        OPTIONAL MATCH (c)-[:PARTNERS_WITH]-(p:Company)
        OPTIONAL MATCH (c)-[:HAS_CLIENT]->(cl:Company)
        OPTIONAL MATCH (pe:Person)-[lr:LEADS]->(c)
        OPTIONAL MATCH (c)-[cit:CITES]->(src:Source)
        RETURN {_COMPANY_PROPS} AS company,
               collect(DISTINCT t.name) AS topics,
               collect(DISTINCT ct.name) AS companyTypes,
               collect(DISTINCT p.name) AS partners,
               collect(DISTINCT cl.name) AS clients,
               collect(DISTINCT {{name: pe.name, title: lr.title}}) AS leadership,
               collect(DISTINCT {{field: cit.field, value: cit.value,
                                  source: src.url, sourceDate: cit.sourceDate}}) AS citations
    """
    async with driver.session() as session:
        result = await session.run(cypher, name=name)
        record = await result.single()
    if record is None or record["company"] is None:
        return None
    data = record.data()
    leadership = [leader for leader in data["leadership"] if leader.get("name")]
    citations = [c for c in data["citations"] if c.get("source")]
    return {
        **data["company"],
        "topics": data["topics"],
        "companyTypes": data["companyTypes"],
        "partners": sorted(data["partners"]),
        "clients": sorted(data["clients"]),
        "leadership": leadership,
        "citations": citations,
    }


async def set_company_kind(driver: AsyncDriver, name: str, kind: str | None) -> bool:
    """Set (or clear, with None) a company's kind. Returns False if not found."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company {name: $name}) SET c.kind = $kind RETURN c.name AS name",
            name=name,
            kind=kind,
        )
        return await result.single() is not None


async def list_topics(driver: AsyncDriver) -> list[str]:
    async with driver.session() as session:
        result = await session.run("MATCH (t:Topic) RETURN t.name AS name ORDER BY name")
        return [record["name"] async for record in result]


async def list_company_types(driver: AsyncDriver) -> list[str]:
    async with driver.session() as session:
        result = await session.run("MATCH (ct:CompanyType) RETURN ct.name AS name ORDER BY name")
        return [record["name"] async for record in result]


async def graph_overview(driver: AsyncDriver) -> dict:
    """Node/edge counts — a quick orientation for a caller (or the MCP client)."""
    async with driver.session() as session:
        nodes = {}
        for label in ["Company", "Person", "Topic", "CompanyType", "Tool"]:
            result = await session.run(f"MATCH (n:{label}) RETURN count(n) AS n")
            nodes[label] = (await result.single())["n"]
        result = await session.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n ORDER BY n DESC"
        )
        edges = {record["t"]: record["n"] async for record in result}
    return {"nodes": nodes, "edges": edges}


# Reject anything that could mutate the graph; run_read_cypher is read-only.
_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|CALL\s*\{)\b",
    re.IGNORECASE,
)


async def run_read_cypher(driver: AsyncDriver, query: str, limit: int = 200) -> list[dict]:
    """Run a READ-ONLY Cypher query and return up to `limit` rows as dicts.

    Rejects any query containing a write clause. Runs in a read transaction as a
    second line of defence. Intended for ad-hoc multi-hop exploration.
    """
    if _WRITE_KEYWORDS.search(query):
        raise ValueError("Only read-only queries are allowed (no CREATE/MERGE/DELETE/SET/…).")

    async def _work(tx):
        result = await tx.run(query)
        return [record.data() async for record in result][:limit]

    async with driver.session() as session:
        return await session.execute_read(_work)
