"""Signal write path + read queries (news/blog/events per company).

Foundation for the Signals epic (#33): the capture agents (#34/#35) build a
`SignalRecord` and call `upsert_signal`; the API/UI (#38) read via
`signals_for_company` / `recent_signals`. This module only touches the graph —
no endpoints, no agents.

Graph shape:
    (:Signal {url, title, publishedAt, publishedAtRaw, kind, summary, capturedAt})
    (Company)-[:MENTIONED_IN]->(Signal)   # a signal can mention several companies
    (Signal)-[:FROM_SOURCE]->(Source)     # reuses the existing Source provenance node

Dedup: `url` is the *canonical* URL (models.canonicalise_url) and carries a
uniqueness constraint, so a second capture of the same story MERGEs onto the
existing node — updating props and unioning company mentions rather than
duplicating.

publishedAt: parsed to a timezone-aware datetime when the raw string is
parseable; otherwise `publishedAt` is left null and the raw string is kept in
`publishedAtRaw`, so nothing is lost and ordering stays purely temporal.
"""

from datetime import datetime, timezone

from neo4j import AsyncDriver, AsyncManagedTransaction

from app.graph.models import SignalRecord

# Human date formats to try after ISO parsing fails, before giving up.
_DATE_FORMATS = ("%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y", "%m/%d/%Y")


def parse_published_at(raw: str | None) -> datetime | None:
    """Parse a raw publish-date string to an aware UTC datetime, or None.

    Accepts ISO 8601 (incl. a trailing ``Z`` and date-only strings) plus a few
    common human formats. Returns None for anything unparseable — the caller keeps
    the raw string instead. Pure/deterministic (naive results are pinned to UTC).
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def upsert_signal(driver: AsyncDriver, record: SignalRecord, companies: list[str]) -> str:
    """MERGE a signal by its canonical URL; link company mentions + a source.

    Idempotent: re-capturing the same canonical URL updates title/summary/date and
    unions any new company mentions (existing ones stay). `capturedAt` is set once,
    on first discovery. Returns the canonical URL the signal is keyed on.
    """
    canonical = record.canonical_url()
    published_dt = parse_published_at(record.published_at)
    async with driver.session() as session:
        await session.execute_write(
            _upsert_signal_tx, record, canonical, published_dt, companies or []
        )
    return canonical


async def _upsert_signal_tx(
    tx: AsyncManagedTransaction,
    record: SignalRecord,
    canonical: str,
    published_dt: datetime | None,
    companies: list[str],
) -> None:
    props = {
        "title": record.title,
        "kind": record.kind,
        "summary": record.summary,
        # Temporal when parseable; otherwise null + keep the raw string.
        "publishedAt": published_dt,
        "publishedAtRaw": None if published_dt else record.published_at,
    }
    props = {k: v for k, v in props.items() if v is not None}
    if published_dt:
        # A successful parse must also CLEAR any stale raw string from an earlier
        # unparseable capture: `SET s += map` removes keys whose value is null,
        # so re-adding the explicit None deletes the property on the node.
        props["publishedAtRaw"] = None

    await tx.run(
        """
        MERGE (s:Signal {url: $url})
        ON CREATE SET s.capturedAt = datetime()
        SET s += $props
        """,
        url=canonical,
        props=props,
    )

    if companies:
        await tx.run(
            """
            MATCH (s:Signal {url: $url})
            UNWIND $companies AS cname
              MERGE (c:Company {name: cname})
              MERGE (c)-[:MENTIONED_IN]->(s)
            """,
            url=canonical,
            companies=companies,
        )

    if record.source:
        await tx.run(
            """
            MATCH (s:Signal {url: $url})
            MERGE (src:Source {url: $source})
            MERGE (s)-[:FROM_SOURCE]->(src)
            """,
            url=canonical,
            source=record.source,
        )


def _iso(value):
    """Neo4j/py temporal → ISO string; pass other values through unchanged."""
    if value is None:
        return None
    if hasattr(value, "to_native"):  # neo4j.time.DateTime
        value = value.to_native()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _shape(signal: dict, companies: list[str], sources: list[str]) -> dict:
    return {
        "url": signal.get("url"),
        "title": signal.get("title"),
        "kind": signal.get("kind"),
        "summary": signal.get("summary"),
        "publishedAt": _iso(signal.get("publishedAt")),
        "publishedAtRaw": signal.get("publishedAtRaw"),
        "capturedAt": _iso(signal.get("capturedAt")),
        "companies": sorted(c for c in companies if c),
        "sources": sorted(s for s in sources if s),
    }


# Newest-first: a parsed publish date when we have one, else fall back to when we
# captured it (both temporal, so the comparison stays single-typed).
_SIGNAL_PROPS = "s{.url,.title,.kind,.summary,.publishedAt,.publishedAtRaw,.capturedAt}"


async def signals_for_company(driver: AsyncDriver, name: str, limit: int = 20) -> list[dict]:
    """Signals mentioning a company, newest-first."""
    cypher = f"""
        MATCH (:Company {{name: $name}})-[:MENTIONED_IN]->(s:Signal)
        OPTIONAL MATCH (c:Company)-[:MENTIONED_IN]->(s)
        OPTIONAL MATCH (s)-[:FROM_SOURCE]->(src:Source)
        WITH s, collect(DISTINCT c.name) AS companies, collect(DISTINCT src.url) AS sources
        ORDER BY coalesce(s.publishedAt, s.capturedAt) DESC
        LIMIT $limit
        RETURN {_SIGNAL_PROPS} AS signal, companies, sources
    """
    async with driver.session() as session:
        result = await session.run(cypher, name=name, limit=limit)
        rows = [record.data() async for record in result]
    return [_shape(r["signal"], r["companies"], r["sources"]) for r in rows]


async def recent_signals(
    driver: AsyncDriver, limit: int = 20, kind: str | None = None
) -> list[dict]:
    """Signals across all companies, newest-first; optionally filtered by kind."""
    where = "WHERE s.kind = $kind" if kind else ""
    cypher = f"""
        MATCH (s:Signal)
        {where}
        OPTIONAL MATCH (c:Company)-[:MENTIONED_IN]->(s)
        OPTIONAL MATCH (s)-[:FROM_SOURCE]->(src:Source)
        WITH s, collect(DISTINCT c.name) AS companies, collect(DISTINCT src.url) AS sources
        ORDER BY coalesce(s.publishedAt, s.capturedAt) DESC
        LIMIT $limit
        RETURN {_SIGNAL_PROPS} AS signal, companies, sources
    """
    params: dict = {"limit": limit}
    if kind:
        params["kind"] = kind
    async with driver.session() as session:
        result = await session.run(cypher, **params)
        rows = [record.data() async for record in result]
    return [_shape(r["signal"], r["companies"], r["sources"]) for r in rows]


# --- People links (AUTHORED / QUOTED_IN / SPOKE_AT) — story #41 --------------
# Append-only additions: the pure extraction + matching-precedence logic lives in
# `app.capture.people`; this module holds only the graph read (candidate lookup)
# and the append-only edge writes, so the graph layer never imports back into
# `capture` (no cycle). Kept in lock-step with capture.people.SIGNAL_PERSON_RELATIONS.
_PERSON_SIGNAL_RELATIONS = frozenset({"AUTHORED", "QUOTED_IN", "SPOKE_AT"})


async def person_signal_candidates(
    driver: AsyncDriver, *, name: str, company: str, linkedin_canon: str | None = None
) -> dict:
    """Existing :Person candidates for a mention, for the precedence decision.

    Returns ``{"linkedin_eids": [...], "name_company_eids": [...]}``: nodes keyed
    on the mention's canonical LinkedIn URL (≤1 under the uniqueness constraint),
    and nodes with the exact ``name`` that lead ``company``. The pure
    ``resolve_mention`` turns these counts into a confident link or a flag.
    """
    async with driver.session() as session:
        linkedin_eids: list[str] = []
        if linkedin_canon:
            result = await session.run(
                "MATCH (p:Person {linkedin: $canon}) RETURN elementId(p) AS eid",
                canon=linkedin_canon,
            )
            linkedin_eids = [record["eid"] async for record in result]
        result = await session.run(
            "MATCH (p:Person {name: $name})-[:LEADS]->(:Company {name: $company}) "
            "RETURN DISTINCT elementId(p) AS eid",
            name=name,
            company=company,
        )
        name_company_eids = [record["eid"] async for record in result]
    return {"linkedin_eids": linkedin_eids, "name_company_eids": name_company_eids}


async def _link_existing_person_tx(tx, url: str, link, rel: str, source: str | None) -> None:
    """Link a confidently-matched existing :Person to the signal (unflagged)."""
    await tx.run(
        f"""
        MATCH (s:Signal {{url: $url}})
        MATCH (p:Person) WHERE elementId(p) = $eid
        MERGE (p)-[r:{rel}]->(s)
        ON CREATE SET r.capturedAt = datetime()
        SET r.flagged = false, r.source = $source
        """,
        url=url,
        eid=link.target_eid,
        source=source,
    )


async def _link_stub_person_tx(tx, url: str, link, rel: str, source: str | None) -> None:
    """Attach a mention we could NOT confidently match to a flagged review stub.

    The stub is keyed on ``(name, unresolvedForSignal)`` so it is idempotent per
    signal and can never collide with — or be mistaken for — a real person node:
    it carries ``origin='signal-capture'`` and ``flagged=true`` for a reviewer to
    reconcile (merge into the right person, or drop). Untrusted crawled text is
    thus never silently written onto a trusted identity.
    """
    await tx.run(
        f"""
        MATCH (s:Signal {{url: $url}})
        MERGE (p:Person {{name: $name, unresolvedForSignal: $url}})
          ON CREATE SET p.origin = 'signal-capture', p.flagged = true, p.createdAt = datetime()
        MERGE (p)-[r:{rel}]->(s)
        ON CREATE SET r.capturedAt = datetime()
        SET r.flagged = true, r.flagReason = $reason, r.source = $source
        """,
        url=url,
        name=link.name,
        reason=link.reason,
        source=source,
    )


async def write_person_signal_links(
    driver: AsyncDriver, canonical_url: str, links, source: str | None = None
) -> dict:
    """Write resolved people→signal links. Append-only, idempotent.

    ``links`` are ``ResolvedLink``-shaped (``.name``, ``.relation``, ``.target_eid``,
    ``.flagged``, ``.reason``). Confident links attach to the existing person;
    flagged links attach to a review stub. The relation type is validated against
    the fixed allowlist before it is interpolated into Cypher (relationship types
    can't be parameterised), so an unexpected value is skipped, never executed.
    Returns ``{"linked": n, "flagged": n}``.
    """
    linked = flagged = 0
    async with driver.session() as session:
        for link in links:
            rel = link.relation
            if rel not in _PERSON_SIGNAL_RELATIONS:
                continue  # never interpolate an unexpected relation type into Cypher
            if link.target_eid and not link.flagged:
                await session.execute_write(
                    _link_existing_person_tx, canonical_url, link, rel, source
                )
                linked += 1
            else:
                await session.execute_write(_link_stub_person_tx, canonical_url, link, rel, source)
                flagged += 1
    return {"linked": linked, "flagged": flagged}


async def recent_signals_filtered(
    driver: AsyncDriver,
    limit: int = 20,
    kind: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """Recent signals across all companies, newest-first, filtered by kind and/or topic.

    A `topic` matches a signal when any company that mentions it is tagged to that
    topic (Company-[:TAGGED_AS]->Topic). Appended rather than folded into
    `recent_signals` so the existing #33 read helper stays untouched for its callers;
    this adds the topic dimension the #38 What's-new feed needs.
    """
    conditions: list[str] = []
    params: dict = {"limit": limit}
    if kind:
        conditions.append("s.kind = $kind")
        params["kind"] = kind
    if topic:
        conditions.append(
            "EXISTS { MATCH (tc:Company)-[:MENTIONED_IN]->(s) "
            "WHERE (tc)-[:TAGGED_AS]->(:Topic {name: $topic}) }"
        )
        params["topic"] = topic
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cypher = f"""
        MATCH (s:Signal)
        {where}
        OPTIONAL MATCH (c:Company)-[:MENTIONED_IN]->(s)
        OPTIONAL MATCH (s)-[:FROM_SOURCE]->(src:Source)
        WITH s, collect(DISTINCT c.name) AS companies, collect(DISTINCT src.url) AS sources
        ORDER BY coalesce(s.publishedAt, s.capturedAt) DESC
        LIMIT $limit
        RETURN {_SIGNAL_PROPS} AS signal, companies, sources
    """
    async with driver.session() as session:
        result = await session.run(cypher, **params)
        rows = [record.data() async for record in result]
    return [_shape(r["signal"], r["companies"], r["sources"]) for r in rows]
