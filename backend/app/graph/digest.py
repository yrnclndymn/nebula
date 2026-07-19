"""Weekly digest — what changed (#51).

Generates a browsable weekly summary of graph deltas and stores each run as a
`(:Digest)` node, so history is a scrollable list rather than a single live view.
The scheduled `digest` job (see app/graph/schedules.py) calls `execute_digest_job`.

Delta model — the trailing 7-day window ``[start, end)``:
  - **new signals**        `(:Signal)` with ``capturedAt`` in the window, grouped
                           by the company(ies) that mention them.
  - **newly-researched**   `(:Company)-[:TAGGED_AS]->(:Topic)` whose ``updatedAt``
                           falls in the window — a topic tag marks a *researched*
                           company (vs a partner/client stub), and ``updatedAt`` is
                           set by every enrichment write (repository.upsert_company).
  - **notable changes**    completed `(:Job)` nodes in the window carrying an
                           ``outcome`` line, minus internal housekeeping (prunes,
                           the digest itself).

The delta *queries* are parameterized Cypher; the grouping/shaping/rendering is
pure and unit-tested without a DB. One budget-capped Gemini call phrases the
summary from the STRUCTURED deltas (graph data — company names + counts, never
crawled signal text); it degrades gracefully to the deterministic rendering on
any error (the #84 lesson: an optional LLM garnish must never fail the job).
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.genai import types
from neo4j import AsyncDriver

from app import budget
from app.config import settings
from app import llm
from app.graph import jobs
from app.graph.driver import get_driver

logger = logging.getLogger("nebula.digest")

WINDOW_DAYS = 7
# Cap the signals listed per company (the group's `count` is still the true total),
# and the number of notable-change lines carried, so a busy week's payload stays small.
_MAX_SIGNALS_PER_COMPANY = 10
_MAX_NOTABLE = 25

# Housekeeping/self job types excluded from "notable changes" — they're internal
# retention chores, not activity a reader cares about.
# signal_refresh is the fan-out orchestrator (#36): its own "refreshed N companies"
# line is housekeeping — the per-company capture outcomes it enqueues are the news.
# thesis_revision (#211) is the scheduled scan: its own "N proposed changes" line is
# housekeeping — the notable change is a reviewer COMMITTING a rule, not the scan run.
_HOUSEKEEPING_TYPES = [
    "digest",
    "cache_prune",
    "job_prune",
    "signal_prune",
    "signal_refresh",
    "thesis_revision",
]


@dataclass(frozen=True)
class DigestWindow:
    """The reporting window. ``week_of`` is the window start's date (ISO) — the
    human label for the digest."""

    start: datetime
    end: datetime
    week_of: str


def week_window(now: datetime, days: int = WINDOW_DAYS) -> DigestWindow:
    """The trailing ``days``-day window ending at ``now``. Pure."""
    start = now - timedelta(days=days)
    return DigestWindow(start=start, end=now, week_of=start.date().isoformat())


def _iso(value):
    """Neo4j/py temporal → ISO string; pass other values through unchanged."""
    if value is None:
        return None
    if hasattr(value, "to_native"):  # neo4j.time.DateTime
        value = value.to_native()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# --- pure: shape the grouped payload -----------------------------------------


def build_payload(
    window: DigestWindow,
    signal_rows: list[dict],
    researched_rows: list[dict],
    job_rows: list[dict],
) -> dict:
    """Group the raw delta rows into the stored/rendered digest payload. Pure —
    inputs are plain dicts with primitive (already ISO-stringified) fields."""
    by_company: dict[str, list[dict]] = {}
    for row in signal_rows:
        item = {
            "title": row.get("title"),
            "url": row.get("url"),
            "kind": row.get("kind"),
            "when": row.get("publishedAt") or row.get("publishedAtRaw") or row.get("capturedAt"),
        }
        companies = [c for c in (row.get("companies") or []) if c] or ["(unattributed)"]
        for company in companies:
            by_company.setdefault(company, []).append(item)

    # Busiest companies first, then alphabetical for stable ordering.
    new_signals_by_company = [
        {
            "company": company,
            "count": len(items),
            "signals": items[:_MAX_SIGNALS_PER_COMPANY],
        }
        for company, items in sorted(
            by_company.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())
        )
    ]

    newly_researched = sorted(
        (
            {
                "name": r.get("name"),
                "topics": sorted(t for t in (r.get("topics") or []) if t),
                "updatedAt": r.get("updatedAt"),
            }
            for r in researched_rows
        ),
        key=lambda r: (r["name"] or "").lower(),
    )

    notable = [
        {"type": j.get("type"), "outcome": j.get("outcome"), "when": j.get("createdAt")}
        for j in job_rows
        if j.get("outcome")
    ][:_MAX_NOTABLE]

    return {
        "weekOf": window.week_of,
        "window": {"start": _iso(window.start), "end": _iso(window.end)},
        "newSignalsByCompany": new_signals_by_company,
        "newlyResearched": newly_researched,
        "notableChanges": notable,
        "totals": {
            "newSignals": len(signal_rows),
            "companiesWithNewSignals": len(new_signals_by_company),
            "newlyResearched": len(newly_researched),
            "notableChanges": len(notable),
        },
    }


def has_deltas(payload: dict) -> bool:
    """Did anything change this week? Pure."""
    totals = payload.get("totals", {})
    return bool(
        totals.get("newSignals") or totals.get("newlyResearched") or totals.get("notableChanges")
    )


def render_summary(payload: dict) -> str:
    """Deterministic one-paragraph summary — the fallback when the LLM is
    unavailable, and the grounding the LLM phrases from. Pure."""
    totals = payload["totals"]
    if not has_deltas(payload):
        return (
            f"Week of {payload['weekOf']}: a quiet week — no new signals, "
            "newly-researched companies, or notable changes."
        )
    clauses: list[str] = []
    if totals["newSignals"]:
        clauses.append(
            f"{totals['newSignals']} new signal(s) across "
            f"{totals['companiesWithNewSignals']} company(ies)"
        )
    if totals["newlyResearched"]:
        clauses.append(f"{totals['newlyResearched']} newly-researched company(ies)")
    if totals["notableChanges"]:
        clauses.append(f"{totals['notableChanges']} notable change(s)")
    return f"Week of {payload['weekOf']}: " + ", ".join(clauses) + "."


def summary_prompt(payload: dict) -> str:
    """Prompt for the optional LLM phrasing. Grounded ONLY in structured graph
    facts — company names and counts, never crawled signal titles/summaries — so
    untrusted content can't steer the prose. Pure."""
    facts = json.dumps(
        {
            "weekOf": payload["weekOf"],
            "totals": payload["totals"],
            "newSignalsByCompany": [
                {"company": g["company"], "count": g["count"]}
                for g in payload["newSignalsByCompany"]
            ],
            "newlyResearched": [r["name"] for r in payload["newlyResearched"]],
            "notableChanges": [n["outcome"] for n in payload["notableChanges"]],
        },
        indent=2,
    )
    return (
        "You are writing a short weekly digest for an internal tool that tracks "
        "companies in a knowledge graph. In 2-4 sentences of plain prose (no "
        "markdown), summarise WHAT CHANGED this week, grounded ONLY in the "
        "structured facts below. Cite specific company names and counts from the "
        "data; never invent anything not present.\n\nFACTS:\n" + facts
    )


async def summarise_deltas(payload: dict) -> str:
    """Phrase the digest with one budget-capped Gemini call. Fails safe: any error
    (incl. quota exhaustion, or a missing key when the client is constructed)
    returns the deterministic rendering — the LLM is an optional garnish (#84)."""
    fallback = render_summary(payload)
    if not has_deltas(payload):
        return fallback
    try:
        resp = await llm.generate(
            model=settings.gemini_model,
            contents=summary_prompt(payload),
            config=types.GenerateContentConfig(temperature=0.2),
        )
        text = (resp.text or "").strip()
    except Exception as exc:  # noqa: BLE001 — optional garnish, never fail the job
        logger.info("digest LLM summary unavailable (%s); using structured rendering", exc)
        return fallback
    return text or fallback


# --- delta queries (parameterized Cypher) ------------------------------------


async def _new_signals(driver: AsyncDriver, start: datetime, end: datetime) -> list[dict]:
    """Signals captured in the window, each with the companies that mention it."""
    cypher = """
        MATCH (s:Signal)
        WHERE s.capturedAt >= $start AND s.capturedAt < $end
        OPTIONAL MATCH (c:Company)-[:MENTIONED_IN]->(s)
        WITH s, collect(DISTINCT c.name) AS companies
        ORDER BY s.capturedAt DESC
        RETURN s{.url,.title,.kind,.summary,.publishedAt,.publishedAtRaw,.capturedAt} AS signal,
               companies
    """
    async with driver.session() as session:
        result = await session.run(cypher, start=start, end=end)
        rows = [rec.data() async for rec in result]
    out: list[dict] = []
    for row in rows:
        sig = row["signal"]
        out.append(
            {
                "url": sig.get("url"),
                "title": sig.get("title"),
                "kind": sig.get("kind"),
                "summary": sig.get("summary"),
                "publishedAt": _iso(sig.get("publishedAt")),
                "publishedAtRaw": sig.get("publishedAtRaw"),
                "capturedAt": _iso(sig.get("capturedAt")),
                "companies": [c for c in row["companies"] if c],
            }
        )
    return out


async def _newly_researched(driver: AsyncDriver, start: datetime, end: datetime) -> list[dict]:
    """Researched companies (topic-tagged) whose enrichment updated in the window."""
    cypher = """
        MATCH (c:Company)-[:TAGGED_AS]->(t:Topic)
        WHERE c.updatedAt >= $start AND c.updatedAt < $end
          AND NOT coalesce(c.junk, false)
        WITH c, collect(DISTINCT t.name) AS topics
        RETURN c.name AS name, toString(c.updatedAt) AS updatedAt, topics
    """
    async with driver.session() as session:
        result = await session.run(cypher, start=start, end=end)
        return [
            {"name": rec["name"], "updatedAt": rec["updatedAt"], "topics": rec["topics"]}
            async for rec in result
        ]


async def _job_outcomes(driver: AsyncDriver, start: datetime, end: datetime) -> list[dict]:
    """Completed jobs in the window carrying an ``outcome`` (excluding housekeeping)."""
    cypher = """
        MATCH (j:Job)
        WHERE j.createdAt >= $start AND j.createdAt < $end
          AND j.status = 'done'
          AND NOT j.type IN $exclude
        RETURN j.type AS type, j.dataJson AS data, toString(j.createdAt) AS createdAt
        ORDER BY j.createdAt DESC
    """
    async with driver.session() as session:
        result = await session.run(cypher, start=start, end=end, exclude=_HOUSEKEEPING_TYPES)
        records = [rec async for rec in result]
    out: list[dict] = []
    for rec in records:
        data = json.loads(rec["data"]) if rec["data"] else {}
        outcome = data.get("outcome")
        if not outcome:
            continue
        out.append({"type": rec["type"], "outcome": outcome, "createdAt": rec["createdAt"]})
    return out


async def collect_deltas(driver: AsyncDriver, now: datetime | None = None) -> dict:
    """Query the week's deltas and shape them into the grouped digest payload."""
    window = week_window(now or datetime.now(timezone.utc))
    signal_rows = await _new_signals(driver, window.start, window.end)
    researched_rows = await _newly_researched(driver, window.start, window.end)
    job_rows = await _job_outcomes(driver, window.start, window.end)
    return build_payload(window, signal_rows, researched_rows, job_rows)


async def digest_due(driver: AsyncDriver, now: datetime | None = None) -> bool:
    """Cheap due-check for the scheduler: is there any new signal OR newly-researched
    company in the trailing window? (Notable-change-only weeks always coincide with
    signals, so this stays a two-clause existence query.) Avoids storing empty
    digests on truly quiet weeks."""
    window = week_window(now or datetime.now(timezone.utc))
    async with driver.session() as session:
        result = await session.run(
            "OPTIONAL MATCH (s:Signal) "
            "WHERE s.capturedAt >= $start AND s.capturedAt < $end "
            "WITH count(s) AS sigs "
            "OPTIONAL MATCH (c:Company)-[:TAGGED_AS]->(:Topic) "
            "WHERE c.updatedAt >= $start AND c.updatedAt < $end "
            "AND NOT coalesce(c.junk, false) "
            "WITH sigs, count(DISTINCT c) AS researched "
            "RETURN sigs + researched AS n",
            start=window.start,
            end=window.end,
        )
        record = await result.single()
    return record["n"] > 0


# --- storage (browsable history) ---------------------------------------------


async def store_digest(driver: AsyncDriver, payload: dict, summary: str) -> dict:
    """Persist a digest: the rendered summary + a JSON payload of the grouped
    deltas, plus the top-line totals as node props for a cheap list view. Returns
    the stored detail."""
    digest_id = f"digest-{uuid.uuid4().hex[:8]}"
    totals = payload["totals"]
    async with driver.session() as session:
        await session.run(
            "CREATE (d:Digest {id: $id, weekOf: $weekOf, generatedAt: datetime(), "
            "summary: $summary, payloadJson: $payload, newSignals: $ns, "
            "companiesWithNewSignals: $cw, newlyResearched: $nr, notableChanges: $nc})",
            id=digest_id,
            weekOf=payload["weekOf"],
            summary=summary,
            payload=json.dumps(payload),
            ns=totals["newSignals"],
            cw=totals["companiesWithNewSignals"],
            nr=totals["newlyResearched"],
            nc=totals["notableChanges"],
        )
    stored = await get_digest(driver, digest_id)
    assert stored is not None  # just created
    return stored


async def list_digests(driver: AsyncDriver, limit: int = 52) -> list[dict]:
    """Stored digests, newest-first — compact rows (totals + prose, no payload)."""
    cypher = """
        MATCH (d:Digest)
        RETURN d.id AS id, d.weekOf AS weekOf, toString(d.generatedAt) AS generatedAt,
               d.summary AS summary, d.newSignals AS newSignals,
               d.companiesWithNewSignals AS companiesWithNewSignals,
               d.newlyResearched AS newlyResearched, d.notableChanges AS notableChanges
        ORDER BY d.generatedAt DESC
        LIMIT $limit
    """
    async with driver.session() as session:
        result = await session.run(cypher, limit=limit)
        records = [rec async for rec in result]
    return [
        {
            "id": rec["id"],
            "weekOf": rec["weekOf"],
            "generatedAt": rec["generatedAt"],
            "summary": rec["summary"],
            "totals": {
                "newSignals": rec["newSignals"],
                "companiesWithNewSignals": rec["companiesWithNewSignals"],
                "newlyResearched": rec["newlyResearched"],
                "notableChanges": rec["notableChanges"],
            },
        }
        for rec in records
    ]


async def get_digest(driver: AsyncDriver, digest_id: str) -> dict | None:
    """One digest's full detail: the summary + the grouped-deltas payload."""
    cypher = """
        MATCH (d:Digest {id: $id})
        RETURN d.id AS id, d.weekOf AS weekOf, toString(d.generatedAt) AS generatedAt,
               d.summary AS summary, d.payloadJson AS payload
    """
    async with driver.session() as session:
        result = await session.run(cypher, id=digest_id)
        record = await result.single()
    if record is None:
        return None
    return {
        "id": record["id"],
        "weekOf": record["weekOf"],
        "generatedAt": record["generatedAt"],
        "summary": record["summary"],
        "payload": json.loads(record["payload"]) if record["payload"] else {},
    }


# --- scheduled runner --------------------------------------------------------


async def execute_digest_job(job_id: str) -> None:
    """Generate and store this week's digest. Budgeted (one optional LLM call, no
    crawling/searching); the LLM step fails safe, so only a graph error fails the
    job. Records the stored digest id + a human-readable outcome for the activity
    page."""
    driver = get_driver()
    job = await jobs.get_job(job_id)
    run_budget = budget.budget_for("digest", (job or {}).get("budget"))
    with budget.use_budget(run_budget):
        payload = await collect_deltas(driver)
        summary = await summarise_deltas(payload)
    stored = await store_digest(driver, payload, summary)
    totals = payload["totals"]
    outcome = (
        f"digest for week of {payload['weekOf']}: {totals['newSignals']} new signal(s), "
        f"{totals['newlyResearched']} newly-researched, {totals['notableChanges']} notable change(s)"
    )
    await jobs.update_job(
        job_id, {**(job or {}), "digestId": stored["id"], "outcome": outcome}, status="done"
    )
    logger.info("digest %s stored as %s (%s)", job_id, stored["id"], outcome)
