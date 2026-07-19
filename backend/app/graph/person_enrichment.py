"""Graph read/write for person enrichment (story #40).

Kept OUT of ``repository.py`` (the company write path) on purpose: the company
functions there are shared, hot, and owned by other work — this module adds
person-specific functions without touching them. It reuses the person-identity
primitives from #39: identity keys on the canonical LinkedIn URL, and attaching a
newly discovered URL to an existing name-only person goes through
:func:`attach_linkedin` (company-scoped, so a namesake leading an unrelated
company is never rewritten — the #87 lesson).

:func:`attach_linkedin` (and its ``_merge_group_tx`` helper) live HERE, in the
graph layer, not in ``app.agents.people`` (#183): they are pure Cypher graph
mutations, so keeping them below the domain lets the person write path call them
directly instead of reaching UP into ``people`` (a pinned import now deleted).
``person_identity`` re-exports them for the discovery domain + the migration CLI.

Writes are gated: nothing here runs except from the explicit commit step of a
reviewed ``person_proposal`` job. The write is idempotent (all MERGE), so
committing the same reviewed record twice is safe.

Provenance mirrors the company CITES pattern exactly:
``(Person)-[:CITES {field, value, sourceDate, capturedAt}]->(Source {url})``.
Prior roles are ``(Person)-[:HELD_ROLE {title, from, to}]->(Company)`` with the
company MERGE'd as a stub when unknown.
"""

from neo4j import AsyncDriver, AsyncManagedTransaction

from app.graph.linkedin import canonical_linkedin
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


async def _merge_group_tx(tx: AsyncManagedTransaction, merge: dict) -> None:
    """Fold every absorbed node into the survivor, then set the canonical URL.

    Re-points each absorbed node's LEADS edges (carrying the title through), fills
    the survivor's name if it was empty, DETACH DELETEs the absorbed node, and only
    THEN writes the canonical linkedin onto the survivor — deleting the duplicates
    first is what keeps the write clear of the uniqueness constraint.
    """
    absorbed_eids = [a["eid"] for a in merge["absorbed"]]
    # Re-point LEADS from the absorbed nodes onto the survivor (keep any title).
    await tx.run(
        """
        MATCH (survivor:Person) WHERE elementId(survivor) = $survivor
        MATCH (dup:Person)-[r:LEADS]->(c:Company)
        WHERE elementId(dup) IN $absorbed
        MERGE (survivor)-[nr:LEADS]->(c)
        SET nr.title = coalesce(nr.title, r.title)
        DELETE r
        """,
        survivor=merge["survivor_eid"],
        absorbed=absorbed_eids,
    )
    # Fill the survivor's display name from an absorbed node if it lacks one.
    await tx.run(
        """
        MATCH (survivor:Person) WHERE elementId(survivor) = $survivor
        OPTIONAL MATCH (dup:Person)
          WHERE elementId(dup) IN $absorbed AND dup.name IS NOT NULL AND dup.name <> ''
        WITH survivor, collect(dup.name)[0] AS dupName
        SET survivor.name = coalesce(survivor.name, dupName)
        """,
        survivor=merge["survivor_eid"],
        absorbed=absorbed_eids,
    )
    # Remove the now-stripped duplicates.
    await tx.run(
        "MATCH (dup:Person) WHERE elementId(dup) IN $absorbed DETACH DELETE dup",
        absorbed=absorbed_eids,
    )
    # Finally write the canonical URL onto the sole surviving node.
    await tx.run(
        "MATCH (survivor:Person) WHERE elementId(survivor) = $survivor "
        "SET survivor.linkedin = $canonical",
        survivor=merge["survivor_eid"],
        canonical=merge["canonical"],
    )


async def attach_linkedin(
    driver: AsyncDriver, name: str, url: str, *, company: str, dry_run: bool = True
) -> dict:
    """Attach a discovered canonical LinkedIn URL to the name-only Person(s) called
    ``name`` who lead ``company``. The reviewable commit for enrichment-discovered
    URLs on EXISTING people (story #39) — never called silently from a write path.

    The evidence behind a discovered URL is specific to ONE company (its team page,
    its slug-gated search), so candidates are scoped to that company's leaders —
    a genuine namesake leading an unrelated company is never touched (#87 review).
    Only nodes that currently have no ``linkedin`` are considered, so a person
    already keyed on a profile is never overwritten. If a node already holds the
    canonical URL, the scoped name-only node(s) merge into it (dedup); otherwise
    the URL is set on one node and same-company name-siblings (true duplicates)
    fold into it. Returns the action taken.
    """
    canon = canonical_linkedin(url)
    if canon is None:
        return {"name": name, "action": "skipped", "reason": "not a personal-profile URL"}

    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {name: $name})-[:LEADS]->(:Company {name: $company})
            WHERE p.linkedin IS NULL
            WITH DISTINCT p
            OPTIONAL MATCH (p)-[r:LEADS]->()
            RETURN elementId(p) AS eid, count(r) AS leads
            """,
            name=name,
            company=company,
        )
        candidates = [dict(rec) async for rec in result]
        if not candidates:
            return {
                "name": name,
                "action": "skipped",
                "reason": f"no name-only Person leading {company!r} to attach",
            }

        # An existing node may already own this canonical URL (e.g. a prior run).
        result = await session.run(
            "MATCH (p:Person {linkedin: $canon}) RETURN elementId(p) AS eid LIMIT 1", canon=canon
        )
        holder = await result.single()

        if holder is not None:
            survivor_eid, survivor_name = holder["eid"], None
            absorbed = [{"eid": c["eid"], "name": name} for c in candidates]
        else:
            keep = sorted(candidates, key=lambda c: (-c["leads"], c["eid"]))[0]
            survivor_eid, survivor_name = keep["eid"], name
            absorbed = [
                {"eid": c["eid"], "name": name} for c in candidates if c["eid"] != keep["eid"]
            ]

        action = "merged" if (holder is not None or absorbed) else "set"
        if not dry_run:
            merge = {
                "canonical": canon,
                "survivor_eid": survivor_eid,
                "survivor_name": survivor_name,
                "absorbed": absorbed,
            }
            await session.execute_write(_merge_group_tx, merge)

    return {"name": name, "canonical": canon, "action": action, "dry_run": dry_run}


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
    # ``PersonRecord.linkedin`` is already canonical — its pydantic validator is the
    # domain choke point (#183), so the write path trusts its input instead of
    # re-canonicalising (or reaching up into ``people`` for the canonicaliser).
    canon = record.linkedin
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
