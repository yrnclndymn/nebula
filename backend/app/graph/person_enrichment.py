"""Graph read/write for person enrichment (story #40).

Kept OUT of ``repository.py`` (the company write path) on purpose: the company
functions there are shared, hot, and owned by other work — this module adds
person-specific functions without touching them. It reuses the person-identity
primitives from #39: identity keys on the canonical LinkedIn URL, and attaching a
newly discovered URL to an existing name-only person goes through
:func:`app.agents.people.person_identity.attach_linkedin` (company-scoped, so a namesake
leading an unrelated company is never rewritten — the #87 lesson).

Writes are gated: nothing here runs except from the explicit commit step of a
reviewed ``person_proposal`` job. The write is idempotent (all MERGE), so
committing the same reviewed record twice is safe.

Provenance mirrors the company CITES pattern exactly:
``(Person)-[:CITES {field, value, sourceDate, capturedAt}]->(Source {url})``.
Prior roles are ``(Person)-[:HELD_ROLE {title, from, to}]->(Company)`` with the
company MERGE'd as a stub when unknown.
"""

from neo4j import AsyncDriver, AsyncManagedTransaction

from app.graph.person_models import PersonRecord


async def get_person_scoped(driver: AsyncDriver, name: str, company: str) -> dict | None:
    """The enrichment snapshot of the person named ``name`` who leads ``company``.

    Scoped by the LEADS edge to ``company`` so we target one specific human, never
    a global name-key (#87). Returns ``None`` when no such leader exists (the
    trigger refuses to research a person who isn't in the graph). The snapshot
    carries the node's current enrichment fields + prior roles, so the review
    surface can diff proposed facts against what's already stored.
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {name: $name})-[l:LEADS]->(c:Company {name: $company})
            WITH p, l
            OPTIONAL MATCH (p)-[hr:HELD_ROLE]->(pc:Company)
            RETURN p.name AS name, p.linkedin AS linkedin, p.bio AS bio,
                   p.personalSite AS personalSite, p.talks AS talks,
                   l.title AS title,
                   collect(DISTINCT {company: pc.name, title: hr.title,
                                     from_year: hr.from, to_year: hr.to}) AS prior_roles
            LIMIT 1
            """,
            name=name,
            company=company,
        )
        record = await result.single()
    if record is None:
        return None
    data = dict(record)
    data["prior_roles"] = [r for r in data.get("prior_roles") or [] if r.get("company")]
    return data


async def _resolve_person_eid(
    tx: AsyncManagedTransaction, name: str, company: str, canon: str | None
) -> str | None:
    """The elementId of the target :Person: the node keyed on ``canon`` if we have
    one, else the name-only leader of ``company``. None if neither is found."""
    if canon:
        result = await tx.run(
            "MATCH (p:Person {linkedin: $canon}) RETURN elementId(p) AS eid LIMIT 1", canon=canon
        )
        row = await result.single()
        if row is not None:
            return row["eid"]
    result = await tx.run(
        "MATCH (p:Person {name: $name})-[:LEADS]->(:Company {name: $company}) "
        "RETURN elementId(p) AS eid LIMIT 1",
        name=name,
        company=company,
    )
    row = await result.single()
    return row["eid"] if row is not None else None


async def _write_person_tx(tx: AsyncManagedTransaction, eid: str, record: PersonRecord) -> None:
    """Apply the reviewed facts onto the located :Person node (by elementId)."""
    # 1. Flat profile properties (only the non-null ones survived provenance).
    props: dict = {}
    if record.bio:
        props["bio"] = record.bio
    if record.personal_site:
        props["personalSite"] = record.personal_site
    if record.talks:
        props["talks"] = record.talks
    if props:
        await tx.run(
            "MATCH (p:Person) WHERE elementId(p) = $eid SET p += $props, p.updatedAt = datetime()",
            eid=eid,
            props=props,
        )

    # 2. Current title lives on the LEADS edge to the scoping company.
    if record.title:
        await tx.run(
            """
            MATCH (p:Person) WHERE elementId(p) = $eid
            MATCH (p)-[l:LEADS]->(:Company {name: $company})
            SET l.title = $title
            """,
            eid=eid,
            company=record.company,
            title=record.title,
        )

    # 3. Prior roles -> HELD_ROLE edges; unknown employers MERGE as :Company stubs.
    #    Keyed on (company, title) so a re-commit updates the span in place.
    if record.prior_roles:
        await tx.run(
            """
            MATCH (p:Person) WHERE elementId(p) = $eid
            UNWIND $roles AS role
              MERGE (co:Company {name: role.company})
              MERGE (p)-[hr:HELD_ROLE {title: coalesce(role.title, '')}]->(co)
              SET hr.from = role.from_year, hr.to = role.to_year, hr.updatedAt = datetime()
            """,
            eid=eid,
            roles=[r.model_dump() for r in record.prior_roles],
        )

    # 4. Provenance — one CITES edge per fact, mirroring the company write path.
    if record.citations:
        await tx.run(
            """
            MATCH (p:Person) WHERE elementId(p) = $eid
            UNWIND $citations AS cit
              MERGE (s:Source {url: cit.source})
              MERGE (p)-[r:CITES {field: cit.field}]->(s)
              SET r.value = cit.value,
                  r.sourceDate = cit.source_date,
                  r.capturedAt = datetime()
            """,
            eid=eid,
            citations=[c.model_dump() for c in record.citations],
        )


async def upsert_person(driver: AsyncDriver, record: PersonRecord) -> dict:
    """Write a reviewed :class:`PersonRecord` to the graph. Called ONLY by the
    commit step of a ``person_proposal`` job — never from a research/write path
    directly (human-in-the-loop preserved).

    When the record carries a canonical LinkedIn URL and the target is still a
    name-only person, identity is established/deduped first via
    :func:`attach_linkedin` (company-scoped). The node is then located (by the
    canonical URL if set, else the name-only leader of the scoping company) and the
    reviewed facts + provenance are applied. Idempotent. Returns the action taken.
    """
    # Lazy import: identity canonicalisation + attach live in the people
    # entity-domain (above graph); this write path reaches UP for them inside the
    # function — the same pinned-exception pattern as the graph/jobs.py dispatch.
    from app.agents.people.person_identity import attach_linkedin, canonical_linkedin

    canon = canonical_linkedin(record.linkedin)
    # Establish/dedup identity on the canonical URL first (reuses #39's reviewable
    # attach; only ever touches a name-only leader of THIS company).
    if canon:
        await attach_linkedin(driver, record.name, canon, company=record.company, dry_run=False)

    async with driver.session() as session:

        async def _tx(tx: AsyncManagedTransaction) -> dict:
            eid = await _resolve_person_eid(tx, record.name, record.company, canon)
            if eid is None:
                return {"name": record.name, "action": "skipped", "reason": "person not found"}
            await _write_person_tx(tx, eid, record)
            return {"name": record.name, "action": "written"}

        return await session.execute_write(_tx)
