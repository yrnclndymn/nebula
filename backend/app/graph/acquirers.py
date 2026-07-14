"""Potential-acquirer analysis (story #44, epic #26 M&A Intelligence).

READ-ONLY over the graph — this module never writes, proposes, or commits. It
answers two questions off the ``ACQUIRED`` edges that #43 introduced:

  * *Who might buy this company?* — :func:`potential_acquirers` ranks candidate
    acquirers for one target company by signal.
  * *Who is most active in the space?* — :func:`most_active_acquirers` lists the
    busiest acquirers (optionally within a topic) with their recent deals.

Kept OUT of ``acquisitions.py`` (which owns the commit write path + #45's read
additions) so parallel story branches merge cleanly.

The design deliberately SPLITS the pure ranking from the Cypher: the graph query
gathers raw, structured facts per candidate; the pure, test-first
:func:`rank_acquirer_candidates` combines them into an ordered list where every
candidate carries a machine-shaped ``why`` (a list of ``{signal, detail}``
reasons) — never a bare score. That keeps the heuristic weights testable without
a database and the ranking explainable in the UI.
"""

from neo4j import AsyncDriver

# --- Scoring weights (documented; the ranking is fully explainable) -------------
# A candidate acquirer earns points per signal, strongest first:
#   * acquired a company in the TARGET's topic — the clearest "buys in this exact
#     space" evidence, so it weighs most (3 per distinct target);
#   * already PARTNERS_WITH the target — a partner buying a partner is a classic
#     acquisition path, so a single direct partnership is worth as much as a
#     topic deal (3);
#   * acquired a company of the target's KIND (but not already topic-matched) —
#     they buy this *type* of company (2 per distinct target);
#   * shares a partner / client with the target — overlapping ecosystem ties
#     (2 each);
#   * general acquisition activity — a bounded breadth bonus so a serial acquirer
#     edges ahead on ties without volume alone dominating real overlap.
W_TOPIC_DEAL = 3
W_DIRECT_PARTNER = 3
W_KIND_DEAL = 2
W_SHARED_PARTNER = 2
W_SHARED_CLIENT = 2
W_ACTIVITY = 1
# Activity bonus counts acquisitions BEYOND the first, capped so it can add at most
# W_ACTIVITY * ACTIVITY_CAP — a tiebreaker, not a driver of the ranking.
ACTIVITY_CAP = 3

# Default / maximum candidates returned for a target company.
ACQUIRERS_DEFAULT = 5
ACQUIRERS_MAX = 20

# Space-level "most active acquirers" defaults and how many recent deals each carries.
ACTIVE_DEFAULT = 10
ACTIVE_MAX = 50
RECENT_DEALS = 5


def _dedup_deals(deals: list | None) -> list[dict]:
    """Deals deduped by target (first source wins), robust to any Cypher quirk."""
    seen: set[str] = set()
    out: list[dict] = []
    for deal in deals or []:
        target = deal.get("target")
        if not target or target in seen:
            continue
        seen.add(target)
        out.append({"target": target, "source": deal.get("source")})
    return out


def _dedup_names(names: list | None) -> list[str]:
    """Names deduped, order preserved (blank/None dropped)."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names or []:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _score_candidate(cand: dict, target_kind: str | None) -> dict | None:
    """Score one candidate's raw facts, returning ``{acquirer, score, why, …}`` or
    None when nothing relevant to the target connects them (pure activity, with no
    topic/kind/partner/client tie, is not a candidate)."""
    topic_deals = _dedup_deals(cand.get("topic_deals"))
    kind_deals = _dedup_deals(cand.get("kind_deals"))
    shared_partners = _dedup_names(cand.get("shared_partners"))
    shared_clients = _dedup_names(cand.get("shared_clients"))
    is_partner = bool(cand.get("is_direct_partner"))
    total = int(cand.get("total_acquisitions") or 0)

    relevance = (
        len(topic_deals)
        + len(kind_deals)
        + len(shared_partners)
        + len(shared_clients)
        + (1 if is_partner else 0)
    )
    if relevance == 0:
        return None

    activity_bonus = W_ACTIVITY * min(max(total - 1, 0), ACTIVITY_CAP)
    score = (
        W_TOPIC_DEAL * len(topic_deals)
        + W_KIND_DEAL * len(kind_deals)
        + (W_DIRECT_PARTNER if is_partner else 0)
        + W_SHARED_PARTNER * len(shared_partners)
        + W_SHARED_CLIENT * len(shared_clients)
        + activity_bonus
    )

    why: list[dict] = []
    if topic_deals:
        why.append(
            {
                "signal": "acquired-in-topic",
                "detail": {"count": len(topic_deals), "deals": topic_deals},
            }
        )
    if kind_deals:
        why.append(
            {
                "signal": "acquired-same-kind",
                "detail": {"count": len(kind_deals), "kind": target_kind, "deals": kind_deals},
            }
        )
    if is_partner:
        why.append({"signal": "direct-partner", "detail": {}})
    if shared_partners:
        why.append(
            {
                "signal": "shared-partners",
                "detail": {"count": len(shared_partners), "partners": shared_partners},
            }
        )
    if shared_clients:
        why.append(
            {
                "signal": "shared-clients",
                "detail": {"count": len(shared_clients), "clients": shared_clients},
            }
        )
    if total > 1:
        why.append({"signal": "active-acquirer", "detail": {"total_acquisitions": total}})

    return {
        "acquirer": cand["acquirer"],
        "score": score,
        "total_acquisitions": total,
        "why": why,
    }


def rank_acquirer_candidates(
    candidates: list[dict],
    *,
    target_kind: str | None = None,
    limit: int = ACQUIRERS_DEFAULT,
) -> list[dict]:
    """Pure ranking: turn raw per-candidate facts into an ordered candidate list.

    Each input dict carries the raw signals gathered by the Cypher
    (``topic_deals``/``kind_deals`` as ``[{target, source}]`` lists,
    ``shared_partners``/``shared_clients`` as name lists, ``is_direct_partner``,
    ``total_acquisitions``). Candidates with no relevant tie to the target are
    dropped; the rest are ordered by score desc then acquirer name (deterministic
    for stable rendering) and capped at ``limit``.
    """
    ranked = [scored for cand in candidates if (scored := _score_candidate(cand, target_kind))]
    ranked.sort(key=lambda r: (-r["score"], r["acquirer"].lower()))
    return ranked[:limit]


# Raw-fact gatherer for one target. Pattern comprehensions collect each signal as a
# list so the pure ranker (above) does the scoring. Only acquirers with >=1 tie to
# the target survive the final WHERE — the same relevance gate the ranker applies.
_CANDIDATES_CYPHER = """
    MATCH (t:Company {name: $name})
    OPTIONAL MATCH (t)-[:TAGGED_AS]->(tt:Topic)
    WITH t, collect(DISTINCT tt.name) AS targetTopics, t.kind AS targetKind
    MATCH (a:Company)
    WHERE a <> t
      AND NOT coalesce(a.junk, false)
      AND EXISTS { (a)-[:ACQUIRED]->(:Company) }
    WITH t, targetTopics, targetKind, a,
      [ (a)-[r:ACQUIRED]->(x:Company)
          WHERE EXISTS { (x)-[:TAGGED_AS]->(xt:Topic) WHERE xt.name IN targetTopics }
        | {target: x.name, source: r.source} ] AS topic_deals,
      [ (a)-[r:ACQUIRED]->(x:Company)
          WHERE targetKind IS NOT NULL AND x.kind = targetKind
            AND NOT EXISTS { (x)-[:TAGGED_AS]->(xt:Topic) WHERE xt.name IN targetTopics }
        | {target: x.name, source: r.source} ] AS kind_deals,
      [ (a)-[:PARTNERS_WITH]-(p:Company)
          WHERE p <> a AND p <> t AND EXISTS { (t)-[:PARTNERS_WITH]-(p) }
        | p.name ] AS shared_partners,
      [ (a)-[:HAS_CLIENT]->(c:Company)
          WHERE EXISTS { (t)-[:HAS_CLIENT]->(c) }
        | c.name ] AS shared_clients,
      EXISTS { (a)-[:PARTNERS_WITH]-(t) } AS is_direct_partner,
      COUNT { (a)-[:ACQUIRED]->(:Company) } AS total_acquisitions
    WITH a.name AS acquirer, topic_deals, kind_deals, shared_partners, shared_clients,
         is_direct_partner, total_acquisitions
    WHERE size(topic_deals) > 0 OR size(kind_deals) > 0 OR size(shared_partners) > 0
       OR size(shared_clients) > 0 OR is_direct_partner
    RETURN acquirer, topic_deals, kind_deals, shared_partners, shared_clients,
           is_direct_partner, total_acquisitions
"""


async def potential_acquirers(
    driver: AsyncDriver, name: str, *, limit: int = ACQUIRERS_DEFAULT
) -> list[dict] | None:
    """Ranked candidate acquirers for the tracked company ``name`` (story #44).

    Gathers each candidate's raw signals — acquisitions of companies in the
    target's topic / of the target's kind, shared partners/clients, an existing
    partnership, and overall acquisition activity — then ranks them with the pure
    :func:`rank_acquirer_candidates`. Returns None if ``name`` is not a company
    (so the route can 404); an empty list means no acquirer has any tie to it.
    """
    async with driver.session() as session:
        exists = await session.run(
            "MATCH (c:Company {name: $name}) RETURN c.kind AS kind", name=name
        )
        record = await exists.single()
        if record is None:
            return None
        target_kind = record["kind"]
        result = await session.run(_CANDIDATES_CYPHER, name=name)
        rows = [rec.data() async for rec in result]
    return rank_acquirer_candidates(rows, target_kind=target_kind, limit=limit)


async def most_active_acquirers(
    driver: AsyncDriver, *, topic: str | None = None, limit: int = ACTIVE_DEFAULT
) -> list[dict]:
    """Space-level view (story #44): the busiest acquirers, most deals first.

    Counts distinct acquired companies per acquirer and carries each acquirer's
    most-recent deals (announced-date desc). When ``topic`` is given, only deals
    whose TARGET is tagged with that topic count — "who is most active in this
    space". Read-only, deterministic ordering (deal count desc, then name).
    """
    topic_filter = "AND EXISTS { (x)-[:TAGGED_AS]->(:Topic {name: $topic}) }" if topic else ""
    cypher = f"""
        MATCH (a:Company)-[r:ACQUIRED]->(x:Company)
        WHERE NOT coalesce(a.junk, false)
          {topic_filter}
        WITH a, r, x
        ORDER BY coalesce(r.announcedAt, '') DESC
        WITH a,
             count(DISTINCT x) AS deal_count,
             collect({{target: x.name, announced_at: r.announcedAt, closed_at: r.closedAt,
                       amount: r.amount, currency: r.currency, source: r.source}}) AS deals
        RETURN a.name AS acquirer, deal_count, deals[0..$recent] AS recent_deals
        ORDER BY deal_count DESC, acquirer ASC
        LIMIT $limit
    """
    params: dict = {"recent": RECENT_DEALS, "limit": limit}
    if topic:
        params["topic"] = topic
    async with driver.session() as session:
        result = await session.run(cypher, **params)
        return [rec.data() async for rec in result]
