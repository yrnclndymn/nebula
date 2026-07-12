"""Periodic signal-refresh selection (pure) + candidate loading (#36).

Signals for a company go stale: its own-site feed (#34) and third-party coverage
(#35) both move on, but capture only runs when someone triggers it. This module
decides *which* companies are due for an unattended re-capture, so the scheduled
``signal_refresh`` job (``app/graph/schedules.py``) can fan out capture jobs on a
cadence without any manual trigger.

The policy has two knobs, both in ``app/config.py``:

  * **staleness** — a company is due when its newest signal was captured more than
    ``signal_refresh_staleness_days`` ago, OR it has no signals at all yet (a
    researched company that has never been captured). This gives "roughly weekly
    per company" refresh when the schedule ticks daily with a 7-day staleness.
  * **batch** — at most ``signal_refresh_batch`` companies are refreshed per run.
    This is the budget rail: it hard-caps the fan-out (and thus downstream Gemini
    spend) one tick can trigger. The neediest (stalest) companies go first, so
    over successive ticks the whole tracked set is covered and each company lands
    back on roughly its staleness cadence.

Selection is pure and deterministic — it takes plain ``RefreshCandidate`` fixtures
and returns the companies to refresh — so it is unit-tested without a database
(fictional names only; the repo is public). Only *refreshable* companies reach the
selector: a company with a website that is not junk and not an end-customer stub
(``kind = 'client'``), mirroring how ``research_backlog`` / ``similar_companies``
exclude junk and client stubs. ``load_refresh_candidates`` applies that filter in
Cypher and attaches each company's newest ``capturedAt``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from neo4j import AsyncDriver

# Sort sentinel: a never-captured company (no signals yet) is the *stalest* of all,
# so it must sort before any real capture date. An aware min-datetime does that.
_NEVER = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RefreshCandidate:
    """A refreshable company and the age of its freshest signal.

    ``last_captured_at`` is the newest ``capturedAt`` among the company's signals
    — when we last pulled its stream — or ``None`` when it has no signals yet.
    ``None`` counts as maximally stale (never refreshed).
    """

    name: str
    website: str
    last_captured_at: datetime | None = None


def select_companies_to_refresh(
    candidates: list[RefreshCandidate],
    *,
    staleness_days: float,
    now: datetime,
    batch_size: int,
) -> list[RefreshCandidate]:
    """The companies due for a signal refresh this run, stalest first, capped.

    A candidate is *due* when it has never been captured (``last_captured_at is
    None``) or its newest signal is older than ``staleness_days``. Due candidates
    are ranked stalest-first (never-captured before any dated capture), ties broken
    by name for determinism, and the neediest ``batch_size`` are returned. A
    non-positive ``batch_size`` selects nothing (the budget rail can close the tap).
    """
    if batch_size <= 0:
        return []
    cutoff = now - timedelta(days=staleness_days)
    due = [c for c in candidates if c.last_captured_at is None or c.last_captured_at < cutoff]
    due.sort(key=lambda c: (c.last_captured_at or _NEVER, c.name))
    return due[:batch_size]


def _to_native(value):
    """neo4j.time.DateTime → aware datetime; pass anything else (incl. None) through."""
    if hasattr(value, "to_native"):
        return value.to_native()
    return value


# A company is refreshable when it has a website (capture needs a URL, and a null
# website also excludes un-researched backlog stubs), is not junk (entity
# resolution), and is not an end-customer stub (kind='client') — the same
# exclusions research_backlog / similar_companies apply. Coalesce-safe so the
# checks hold whether or not the property is set.
_REFRESHABLE = (
    "c.website IS NOT NULL AND NOT coalesce(c.junk, false) AND coalesce(c.kind, '') <> 'client'"
)


async def load_refresh_candidates(driver: AsyncDriver) -> list[RefreshCandidate]:
    """Read every refreshable company with the newest ``capturedAt`` of its signals.

    The tracked set is small (retention keeps it well under the Aura cap), so a
    single aggregating read is fine; the selector then ranks and caps. Companies
    with no signals come back with ``last_captured_at = None`` (maximally stale).
    """
    cypher = f"""
        MATCH (c:Company)
        WHERE {_REFRESHABLE}
        OPTIONAL MATCH (c)-[:MENTIONED_IN]->(s:Signal)
        WITH c, max(s.capturedAt) AS lastCaptured
        RETURN c.name AS name, c.website AS website, lastCaptured
    """
    candidates: list[RefreshCandidate] = []
    async with driver.session() as session:
        result = await session.run(cypher)
        async for r in result:
            candidates.append(
                RefreshCandidate(
                    name=r["name"],
                    website=r["website"],
                    last_captured_at=_to_native(r["lastCaptured"]),
                )
            )
    return candidates


async def companies_due_exist(driver: AsyncDriver, *, staleness_days: float) -> bool:
    """Cheap due-check for the schedule tick: does any refreshable company have a
    newest signal older than ``staleness_days`` (or none at all)? Keeps the tick
    from enqueuing an empty refresh job. The runner (with its batch cap and
    ranking) is the precise arbiter — a rare over-trigger just fans out nothing.
    """
    async with driver.session() as session:
        result = await session.run(
            f"MATCH (c:Company) WHERE {_REFRESHABLE} "
            "OPTIONAL MATCH (c)-[:MENTIONED_IN]->(s:Signal) "
            "WITH c, max(s.capturedAt) AS lastCaptured "
            "WHERE lastCaptured IS NULL "
            "OR lastCaptured < datetime() - duration({days: $days}) "
            "RETURN count(c) AS n",
            days=staleness_days,
        )
        record = await result.single()
    return record["n"] > 0
