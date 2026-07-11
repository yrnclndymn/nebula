"""Read queries for the API. Returns plain dicts/lists ready for JSON.

The table UI works off "researched" companies — those with a TAGGED_AS edge to a
topic — which distinguishes them from the partner/client stub nodes pulled in by
relationships. Aggregates (partner/client/leader counts) come back per row so the
table can show connection density without a second round-trip.
"""

import re

from neo4j import AsyncDriver

_COMPANY_PROPS = (
    "c{.name,.priority,.about,.website,.linkedin,.hqLocation,.hqCountry,.hqCity,.hqState,"
    ".headcount,.estimatedRevenue,.yearFounded,.funding,.notes,.origin,.kind}"
)


async def list_companies(
    driver: AsyncDriver,
    *,
    topic: str | None = None,
    q: str | None = None,
    company_type: str | None = None,
    kind: str | None = None,
    country: str | None = None,
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
    if country:
        conditions.append("c.hqCountry = $country")
        params["country"] = country
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

    params["customKeys"] = await _custom_keys(driver)
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
               count(DISTINCT pe) AS leaderCount,
               [k IN $customKeys | {{key: k, value: c[k]}}] AS customFields
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
            "custom": {cf["key"]: cf["value"] for cf in r["customFields"]},
        }
        for r in rows
    ]


async def get_company(driver: AsyncDriver, name: str) -> dict | None:
    custom_keys = await _custom_keys(driver)
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
                                  source: src.url, sourceDate: cit.sourceDate}}) AS citations,
               [k IN $customKeys | {{key: k, value: c[k]}}] AS customFields
    """
    async with driver.session() as session:
        result = await session.run(cypher, name=name, customKeys=custom_keys)
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
        "custom": {cf["key"]: cf["value"] for cf in data["customFields"]},
    }


async def list_field_defs(driver: AsyncDriver) -> list[dict]:
    """Custom field definitions (registry)."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (fd:FieldDef) RETURN fd{.name, .label, .description, .appliesToKind, .type} AS fd "
            "ORDER BY fd.label"
        )
        return [record["fd"] async for record in result]


async def add_field_def(
    driver: AsyncDriver,
    name: str,
    label: str,
    description: str,
    applies_to_kind: str,
    field_type: str,
) -> dict:
    async with driver.session() as session:
        await session.run(
            "MERGE (fd:FieldDef {name: $name}) "
            "SET fd.label = $label, fd.description = $description, "
            "    fd.appliesToKind = $applies, fd.type = $type",
            name=name,
            label=label,
            description=description,
            applies=applies_to_kind,
            type=field_type,
        )
    return {"name": name, "label": label, "appliesToKind": applies_to_kind, "type": field_type}


async def _custom_keys(driver: AsyncDriver) -> list[str]:
    async with driver.session() as session:
        result = await session.run("MATCH (fd:FieldDef) RETURN fd.name AS name")
        return [record["name"] async for record in result]


async def set_custom_field(driver: AsyncDriver, company_name: str, key: str, value) -> bool:
    """Set a custom field value on a company (dynamic property via SET c += map)."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company {name: $name}) SET c += $props, c.updatedAt = datetime() "
            "RETURN c.name AS name",
            name=company_name,
            props={key: value},
        )
        return await result.single() is not None


async def cite(driver: AsyncDriver, company_name: str, field: str, value: str, source: str) -> None:
    """Attach a provenance citation for a field value on a company."""
    async with driver.session() as session:
        await session.run(
            "MATCH (c:Company {name: $name}) MERGE (src:Source {url: $source}) "
            "MERGE (c)-[r:CITES {field: $field}]->(src) "
            "SET r.value = $value, r.capturedAt = datetime()",
            name=company_name,
            field=field,
            value=value,
            source=source,
        )


async def companies_with_hq(driver: AsyncDriver) -> list[dict]:
    """Researched companies that have a free-text HQ (for the tidy-up)."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company)-[:TAGGED_AS]->(:Topic) WHERE c.hqLocation IS NOT NULL "
            "RETURN c.name AS name, c.hqLocation AS hq ORDER BY name"
        )
        return [dict(record) async for record in result]


async def set_hq(
    driver: AsyncDriver, name: str, country: str | None, city: str | None, state: str | None
) -> None:
    async with driver.session() as session:
        await session.run(
            "MATCH (c:Company {name: $name}) "
            "SET c.hqCountry = $country, c.hqCity = $city, c.hqState = $state",
            name=name,
            country=country,
            city=city,
            state=state,
        )


async def list_countries(driver: AsyncDriver) -> list[str]:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company) WHERE c.hqCountry IS NOT NULL "
            "RETURN DISTINCT c.hqCountry AS country ORDER BY country"
        )
        return [record["country"] async for record in result]


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


# Edge types surfaced in the interactive graph view (issue #50). Matched
# undirected so a node's whole 1-hop neighbourhood comes back regardless of the
# stored direction; the direction is preserved per edge in the response.
_GRAPH_EDGE_TYPES = ["PARTNERS_WITH", "HAS_CLIENT", "TAGGED_AS", "CLASSIFIED_AS", "LEADS"]


def _node_kind(labels: list[str]) -> str:
    """Pick the styling label for a node (Company / Person / Topic / CompanyType)."""
    for label in ("Company", "Person", "Topic", "CompanyType"):
        if label in labels:
            return label
    return labels[0] if labels else "Unknown"


async def company_neighbourhood(driver: AsyncDriver, name: str) -> dict | None:
    """A company node plus its 1-hop typed edges, for the interactive graph view.

    Lazy expansion: the SPA fetches one node's neighbourhood at a time rather than
    rendering the whole ~700-node graph. Returns ``{center, nodes, edges}`` with
    stable ids (``"<Label>:<name>"``) so the client can dedupe/merge across
    expansions, and preserves each edge's direction (source→target). Returns None
    if the company is absent.
    """
    cypher = """
        MATCH (c:Company {name: $name})
        OPTIONAL MATCH (c)-[r]-(m)
        WHERE type(r) IN $edgeTypes
        WITH c, r, m, startNode(r) = c AS outgoing
        RETURN labels(c) AS centerLabels,
               c.name AS centerName,
               c.kind AS centerKind,
               c.website AS centerWebsite,
               EXISTS { (c)-[:TAGGED_AS]->(:Topic) } AS centerResearched,
               collect(
                 CASE WHEN r IS NULL THEN NULL ELSE {
                   type: type(r),
                   outgoing: outgoing,
                   name: m.name,
                   labels: labels(m),
                   kind: m.kind,
                   website: m.website,
                   researched: EXISTS { (m)-[:TAGGED_AS]->(:Topic) }
                 } END
               ) AS rels
    """
    async with driver.session() as session:
        result = await session.run(cypher, name=name, edgeTypes=_GRAPH_EDGE_TYPES)
        record = await result.single()
    if record is None or record["centerName"] is None:
        return None
    data = record.data()

    center_kind = _node_kind(data["centerLabels"])
    center_id = f"{center_kind}:{data['centerName']}"
    nodes: dict[str, dict] = {
        center_id: {
            "id": center_id,
            "kind": center_kind,
            "name": data["centerName"],
            "companyKind": data["centerKind"],
            "website": data["centerWebsite"],
            "researched": data["centerResearched"],
        }
    }
    edges: list[dict] = []
    seen: set[tuple] = set()
    for rel in data["rels"]:
        if rel is None or rel.get("name") is None:
            continue
        m_kind = _node_kind(rel["labels"])
        m_id = f"{m_kind}:{rel['name']}"
        if m_id not in nodes:
            nodes[m_id] = {
                "id": m_id,
                "kind": m_kind,
                "name": rel["name"],
                "companyKind": rel.get("kind"),
                "website": rel.get("website"),
                "researched": bool(rel.get("researched")),
            }
        source, target = (center_id, m_id) if rel["outgoing"] else (m_id, center_id)
        key = (source, target, rel["type"])
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": source, "target": target, "type": rel["type"]})

    return {"center": center_id, "nodes": list(nodes.values()), "edges": edges}


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
