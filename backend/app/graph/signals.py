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
