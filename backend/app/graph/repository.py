"""Graph writes. `upsert_company` is the single entry point for getting a
`CompanyRecord` into Neo4j — used by both the Sheet importer and the agents.

The write runs as one transaction of a few MERGE statements rather than one
giant query, to avoid Cartesian-product row blow-ups when a company has, say,
several partners AND several leaders. Each MERGE is idempotent, so re-running an
enrichment updates in place instead of duplicating.
"""

from neo4j import AsyncDriver, AsyncManagedTransaction

from app.graph.models import CompanyRecord
from app.graph.person_identity import canonical_linkedin


async def upsert_company(driver: AsyncDriver, record: CompanyRecord) -> None:
    async with driver.session() as session:
        await session.execute_write(_upsert_tx, record)


async def _upsert_tx(tx: AsyncManagedTransaction, record: CompanyRecord) -> None:
    # 1. The company itself + flat properties.
    await tx.run(
        """
        MERGE (c:Company {name: $name})
        SET c += $props, c.updatedAt = datetime()
        """,
        name=record.name,
        props=record.scalar_props(),
    )

    # 2. Tags: research topics and company types.
    if record.topics:
        await tx.run(
            """
            MATCH (c:Company {name: $name})
            UNWIND $topics AS topic
              MERGE (t:Topic {name: topic})
              MERGE (c)-[:TAGGED_AS]->(t)
            """,
            name=record.name,
            topics=record.topics,
        )
    if record.company_types:
        await tx.run(
            """
            MATCH (c:Company {name: $name})
            UNWIND $types AS ctype
              MERGE (ct:CompanyType {name: ctype})
              MERGE (c)-[:CLASSIFIED_AS]->(ct)
            """,
            name=record.name,
            types=record.company_types,
        )

    # 3. Org-to-org edges. Partners/clients become :Company stubs if new. A name
    #    that entity resolution recorded as an alias of a canonical node resolves
    #    to that canonical, so re-enrichment hits the merged node instead of
    #    re-creating the variant stub (coalesce falls back to the raw name).
    if record.partnerships:
        await tx.run(
            """
            MATCH (c:Company {name: $name})
            UNWIND $partners AS partner
              OPTIONAL MATCH (canon:Company) WHERE partner IN canon.aliases
              WITH c, partner, collect(canon.name)[0] AS canonName
              MERGE (p:Company {name: coalesce(canonName, partner)})
              MERGE (c)-[:PARTNERS_WITH]->(p)
            """,
            name=record.name,
            partners=record.partnerships,
        )
    if record.clients:
        await tx.run(
            """
            MATCH (c:Company {name: $name})
            UNWIND $clients AS client
              OPTIONAL MATCH (canon:Company) WHERE client IN canon.aliases
              WITH c, client, collect(canon.name)[0] AS canonName
              MERGE (cl:Company {name: coalesce(canonName, client)})
              MERGE (c)-[:HAS_CLIENT]->(cl)
            """,
            name=record.name,
            clients=record.clients,
        )

    # 4. Leadership. Title lives on the relationship (a person may lead more than
    #    one company, in different roles). Identity keys on the canonical LinkedIn
    #    URL when the leader carries one (story #39) — the name is display-only —
    #    and falls back to name-keying (with the caller's variant reconciliation)
    #    otherwise. The two are split so each UNWIND has a single MERGE key.
    if record.leadership:
        keyed, by_name = [], []
        for leader in record.leadership:
            canon = canonical_linkedin(leader.linkedin)
            entry = {"name": leader.name, "title": leader.title}
            if canon:
                keyed.append({**entry, "linkedin": canon})
            else:
                by_name.append(entry)
        if keyed:
            await tx.run(
                """
                MATCH (c:Company {name: $name})
                UNWIND $leaders AS leader
                  MERGE (person:Person {linkedin: leader.linkedin})
                    ON CREATE SET person.name = leader.name
                  SET person.name = coalesce(person.name, leader.name)
                  MERGE (person)-[r:LEADS]->(c)
                  SET r.title = leader.title
                """,
                name=record.name,
                leaders=keyed,
            )
        if by_name:
            await tx.run(
                """
                MATCH (c:Company {name: $name})
                UNWIND $leaders AS leader
                  MERGE (person:Person {name: leader.name})
                  MERGE (person)-[r:LEADS]->(c)
                  SET r.title = leader.title
                """,
                name=record.name,
                leaders=by_name,
            )

    # 5. Provenance. Each citation ties a field's value to a Source (+ its date),
    #    so a figure can be checked back to where the agent found it.
    if record.citations:
        await tx.run(
            """
            MATCH (c:Company {name: $name})
            UNWIND $citations AS cit
              MERGE (s:Source {url: cit.source})
              MERGE (c)-[r:CITES {field: cit.field}]->(s)
              SET r.value = cit.value,
                  r.sourceDate = cit.source_date,
                  r.capturedAt = datetime()
            """,
            name=record.name,
            citations=[c.model_dump() for c in record.citations],
        )
