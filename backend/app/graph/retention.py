"""Signal retention policy: prune-selection (pure) + graph size metrics (#37).

Why this exists — day one, not cleanup. Neo4j Aura Free caps at **200K nodes**,
and periodic signal capture (#34/#35) grows the graph without bound, so a
retention policy is a launch requirement. The policy bounds growth two ways, both
configurable in ``app/config.py``:

  * **count cap** — keep only the newest ``signal_max_per_company`` signals per
    company per kind. This is a *hard* node bound: at most
    ``companies x kinds x N`` Signal nodes, independent of capture rate.
  * **age cap** — drop any signal older than ``signal_max_age_days``; stale news
    ages out even for a company below the count cap.

A signal is **kept** iff it clears BOTH caps for at least one company that
mentions it (a shared story that is still fresh-and-recent for *any* company
survives); it is pruned otherwise. Selection is pure and deterministic — it takes
plain fixtures and returns the URLs to delete — so it is unit-tested without a
database (fictional names only; the repo is public). The scheduled runner in
``app/graph/schedules.py`` reads candidate data, applies this function, spares
signals referenced by un-reviewed work, and batch-deletes the rest.

Page-cache expiry for signal crawls is NOT handled here: pages fetched during
capture are ordinary ``:Page`` nodes (``app/graph/cache.py``), already pruned by
the existing ``cache_prune`` schedule on ``cache_ttl_days``. See the RETENTION
section of ``app/graph/README.md``.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from neo4j import AsyncDriver

# Neo4j Aura Free node ceiling — the number the retention policy exists to defend.
# Surfaced by `graph_size` so the cap is observable, not just assumed.
AURA_FREE_NODE_CAP = 200_000


@dataclass(frozen=True)
class SignalRef:
    """The minimal shape of a signal needed to decide its fate.

    ``effective_at`` is ``coalesce(publishedAt, capturedAt)`` — a parsed publish
    date when we have one, else when we captured it — matching how the read
    queries order signals. ``companies`` is every company that mentions the
    signal (a signal can mention several); empty for an orphan.
    """

    url: str
    kind: str
    effective_at: datetime
    companies: tuple[str, ...] = ()


def select_signals_to_prune(
    signals: list[SignalRef],
    *,
    max_per_company: int,
    max_age_days: float,
    now: datetime,
    protected_urls: frozenset[str] = frozenset(),
) -> list[str]:
    """Return the canonical URLs of signals to delete, given the policy.

    A signal is pruned when it is older than ``max_age_days`` OR it is beyond the
    newest ``max_per_company`` for its ``(company, kind)`` group — i.e. it is kept
    only if it clears BOTH caps for at least one mentioning company. A signal
    whose URL is in ``protected_urls`` is never returned (the caller protects
    signals cited by un-reviewed work). Deterministic: ties on ``effective_at``
    break by URL so ranking at the cap boundary is stable.
    """
    age_cutoff = now - timedelta(days=max_age_days)

    # For each (company, kind), the newest-N URLs clear the count cap. A URL that
    # clears it for ANY of its companies is "within cap" — a shared story kept by
    # one company is kept for all (deleting the node would remove it everywhere).
    within_cap: set[str] = set()
    groups: dict[tuple[str, str], list[SignalRef]] = defaultdict(list)
    for s in signals:
        for company in s.companies:
            groups[(company, s.kind)].append(s)
    for items in groups.values():
        ranked = sorted(items, key=lambda s: (s.effective_at, s.url), reverse=True)
        for rank, s in enumerate(ranked, start=1):
            if rank <= max_per_company:
                within_cap.add(s.url)

    prune: set[str] = set()
    for s in signals:
        if s.url in protected_urls:
            continue
        too_old = s.effective_at < age_cutoff
        over_cap = s.url not in within_cap  # orphans (no company) never clear it
        if too_old or over_cap:
            prune.add(s.url)
    return sorted(prune)


def _to_native(value):
    """neo4j.time.DateTime → aware datetime; pass a datetime through unchanged."""
    if hasattr(value, "to_native"):
        return value.to_native()
    return value


async def load_signal_refs(driver: AsyncDriver) -> list[SignalRef]:
    """Read every signal as a `SignalRef` (url, kind, effective date, companies).

    Bounded by the retention policy itself (nodes stay well under the 200K cap),
    so a single read per prune run is fine; the runner batches the delete.
    """
    cypher = """
        MATCH (s:Signal)
        OPTIONAL MATCH (c:Company)-[:MENTIONED_IN]->(s)
        WITH s, collect(DISTINCT c.name) AS companies
        RETURN s.url AS url, s.kind AS kind,
               coalesce(s.publishedAt, s.capturedAt) AS effAt, companies
    """
    refs: list[SignalRef] = []
    async with driver.session() as session:
        result = await session.run(cypher)
        async for r in result:
            refs.append(
                SignalRef(
                    url=r["url"],
                    kind=r["kind"],
                    effective_at=_to_native(r["effAt"]),
                    companies=tuple(name for name in r["companies"] if name),
                )
            )
    return refs


async def graph_size(driver: AsyncDriver) -> dict:
    """Node/relationship totals + signal breakdown, for observing the 200K cap.

    Cheap counts only. Surfaced by the metrics endpoint and mirrored in the
    prune job's log so graph size is observable both ways.
    """
    async with driver.session() as session:
        nodes = (await (await session.run("MATCH (n) RETURN count(n) AS n")).single())["n"]
        rels = (await (await session.run("MATCH ()-[r]->() RETURN count(r) AS n")).single())["n"]
        result = await session.run("MATCH (s:Signal) RETURN s.kind AS kind, count(*) AS n")
        by_kind = {r["kind"]: r["n"] async for r in result}
        pages = (await (await session.run("MATCH (p:Page) RETURN count(p) AS n")).single())["n"]
    return {
        "nodes": nodes,
        "relationships": rels,
        "nodeCap": AURA_FREE_NODE_CAP,
        "signals": {"total": sum(by_kind.values()), "byKind": by_kind},
        "cachePages": pages,
    }
