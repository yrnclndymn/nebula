"""Graph read/write for acquisitions (story #43, epic #26 M&A Intelligence).

Kept OUT of ``repository.py`` (the shared, hot company write path) on purpose —
this module adds M&A-specific functions without touching it, so parallel story
branches merge cleanly. The write mirrors the person-enrichment discipline:
nothing here runs except from the explicit commit step of a reviewed
``acquisition_proposal`` job (human-in-the-loop), and it is idempotent (all MERGE),
so committing the same reviewed record twice is safe.

Shape: ``(acquirer)-[:ACQUIRED {announcedAt, closedAt, amount, currency, thesis,
source, amountSource}]->(target)``. Both endpoints MERGE as :Company nodes —
unknown ones become stubs (``origin: 'agent'`` on create) that feed the research
backlog. Acquirer/target names resolve through the entity-resolution alias map
first (same coalesce the partner/client stubs use) so a variant name hits the
canonical node instead of spawning a duplicate.
"""

from neo4j import AsyncDriver, AsyncManagedTransaction

from app.graph.deal_models import AcquisitionRecord, Deal


async def get_acquisitions(driver: AsyncDriver, company: str) -> list[dict]:
    """Every ACQUIRED edge that touches ``company`` (as acquirer OR target).

    Feeds the review/diff surface: the proposal diffs its proposed deals against
    this so the reviewer sees only genuinely new/changed deals. Direction is
    carried explicitly (``acquirer``/``target`` names), never inferred.
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (acq:Company)-[r:ACQUIRED]->(tgt:Company)
            WHERE acq.name = $company OR tgt.name = $company
            RETURN acq.name AS acquirer, tgt.name AS target,
                   r.announcedAt AS announced_at, r.closedAt AS closed_at,
                   r.amount AS amount, r.currency AS currency,
                   r.thesis AS thesis, r.source AS source,
                   r.amountSource AS amount_source
            ORDER BY r.announcedAt DESC
            """,
            company=company,
        )
        return [dict(rec) async for rec in result]


async def canonical_names(driver: AsyncDriver, names: list[str]) -> dict[str, str]:
    """Map each name to the canonical Company name whose alias list contains it.

    Names with no aliased node map to themselves. Used to canonicalise a proposed
    record BEFORE diffing it against stored edges (which are already
    alias-resolved at write time) — so the review diff compares like with like.
    Same alias coalesce the write path applies, hoisted to a batch read.
    """
    if not names:
        return {}
    async with driver.session() as session:
        result = await session.run(
            """
            UNWIND $names AS n
            OPTIONAL MATCH (c:Company) WHERE n IN c.aliases
            RETURN n AS name, collect(c.name)[0] AS canonical
            """,
            names=sorted(set(names)),
        )
        return {rec["name"]: rec["canonical"] or rec["name"] async for rec in result}


async def _write_deal_tx(tx: AsyncManagedTransaction, deal: Deal) -> None:
    """MERGE both companies (alias-resolved; unknown ones stubbed) and the ACQUIRED
    edge, then set the reviewed deal facts. Provenance rides on the edge as ``source``
    (deal) and ``amountSource`` (amount) — the figure is always checkable."""
    await tx.run(
        """
        OPTIONAL MATCH (ca:Company) WHERE $acquirer IN ca.aliases
        WITH collect(ca.name)[0] AS acqName
        OPTIONAL MATCH (ct:Company) WHERE $target IN ct.aliases
        WITH acqName, collect(ct.name)[0] AS tgtName
        MERGE (acq:Company {name: coalesce(acqName, $acquirer)})
          ON CREATE SET acq.origin = 'agent', acq.createdAt = datetime()
        MERGE (tgt:Company {name: coalesce(tgtName, $target)})
          ON CREATE SET tgt.origin = 'agent', tgt.createdAt = datetime()
        MERGE (acq)-[r:ACQUIRED]->(tgt)
        SET r.announcedAt = $announced_at,
            r.closedAt = $closed_at,
            r.amount = $amount,
            r.currency = $currency,
            r.thesis = $thesis,
            r.source = $source,
            r.amountSource = $amount_source,
            r.updatedAt = datetime()
        """,
        acquirer=deal.acquirer,
        target=deal.target,
        announced_at=deal.announced_at,
        closed_at=deal.closed_at,
        amount=deal.amount,
        currency=deal.currency,
        thesis=deal.thesis,
        source=deal.source,
        amount_source=deal.amount_source,
    )


async def upsert_acquisitions(driver: AsyncDriver, record: AcquisitionRecord) -> dict:
    """Write a reviewed :class:`AcquisitionRecord` to the graph. Called ONLY by the
    commit step of an ``acquisition_proposal`` job — never from a research/write
    path directly (human-in-the-loop preserved).

    Each deal already carries only cited facts (provenance filtered at build time),
    so the write is safe. Idempotent — the ACQUIRED edge MERGEs on (acquirer,
    target). Returns the count written.
    """
    async with driver.session() as session:

        async def _tx(tx: AsyncManagedTransaction) -> dict:
            for deal in record.deals:
                await _write_deal_tx(tx, deal)
            return {"company": record.company, "action": "written", "deals": len(record.deals)}

        return await session.execute_write(_tx)


async def recent_acquisitions(
    driver: AsyncDriver,
    limit: int = 25,
    topic: str | None = None,
    acquirer: str | None = None,
) -> list[dict]:
    """Recent ACQUIRED edges across the whole graph, newest announced first (#45).

    Powers the space-level M&A view (not scoped to one company like
    :func:`get_acquisitions`). Optional filters: ``topic`` keeps only deals where
    *either* endpoint is TAGGED_AS that topic (an acquisition is "in the space" if
    the acquirer or the target is), and ``acquirer`` narrows to deals made by one
    company. Returns the deal facts plus both provenance URLs (``source`` for the
    deal, ``amount_source`` for the figure) so the UI can render every amount next
    to its citation — an uncited amount is never surfaced.

    Read-only. Ordered by announced date descending; edges with no ``announcedAt``
    sort LAST via the coalesce-to-empty guard (Cypher treats null as the largest
    value, so a bare DESC would float undated deals to the top).
    """
    conditions: list[str] = []
    params: dict = {"limit": limit}
    if topic:
        conditions.append(
            "(EXISTS { (acq)-[:TAGGED_AS]->(:Topic {name: $topic}) } "
            "OR EXISTS { (tgt)-[:TAGGED_AS]->(:Topic {name: $topic}) })"
        )
        params["topic"] = topic
    if acquirer:
        # Case-insensitive substring, matching the live-filter precedent in
        # graph/queries.py — exact equality made partial typing return empty
        # (PR #118 review).
        conditions.append("toLower(acq.name) CONTAINS toLower($acquirer)")
        params["acquirer"] = acquirer
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    async with driver.session() as session:
        result = await session.run(
            f"""
            MATCH (acq:Company)-[r:ACQUIRED]->(tgt:Company)
            {where}
            RETURN acq.name AS acquirer, tgt.name AS target,
                   r.announcedAt AS announced_at, r.closedAt AS closed_at,
                   r.amount AS amount, r.currency AS currency,
                   r.thesis AS thesis, r.source AS source,
                   r.amountSource AS amount_source
            ORDER BY coalesce(r.announcedAt, '') DESC
            LIMIT $limit
            """,
            **params,
        )
        return [dict(rec) async for rec in result]
