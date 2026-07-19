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

from app.graph.thesis import get_thesis_rules

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

# --- Size-awareness weights (#165) ---------------------------------------------
# Two size signals nudge — never gate — a candidate that already has a relationship
# tie. Each fires ONLY when BOTH sides of its comparison exist; absent size data is
# strictly neutral, so a candidate with no headcount ranks exactly as it did pre-#165.
#   * size-plausible — acquirer meaningfully larger than the target (>= SIZE_LARGER_RATIO)
#     earns a small bonus; an acquirer SMALLER than the target is dampened by a
#     penalty (reverse takeovers exist but are rare) — a penalty, never an exclusion.
#   * size-fit — the target's headcount sits within (or near) the acquirer's historical
#     target-size range, drawn from its past targets' headcounts.
W_SIZE_PLAUSIBLE = 1
W_SIZE_SMALLER = -1
W_SIZE_FIT = 1
# An acquirer at least this many times the target's headcount reads as "meaningfully
# larger" — enough of a gap that swallowing the target is plausible.
SIZE_LARGER_RATIO = 3.0
# Tolerance band around the historical [min, max] target-size range: a target just
# outside the observed band (or a single-deal history) still counts as a "fit".
SIZE_FIT_TOLERANCE = 1.5

# --- Thesis-match weight (#194, epic #192) --------------------------------------
# The stored acquisition thesis (:ThesisRule) is a top-level model of who acquires
# whom in the space. When a candidate's KIND acquires the target's KIND per an active
# rule, it earns points scaled by that rule's confidence and a `thesis-match` why-entry
# citing the rule's statement + evidence count — never a bare score. Like the #165 size
# signals, this REWEIGHTS a candidate that already cleared the relevance gate; it never
# gates one in, and missing kind data (either side) is strictly neutral.
W_THESIS_MATCH = 3
# Qualified rules only fire when their condition is confirmed. The maintainer's sole
# qualified seed rule is "domain-focused" (larger services companies acquiring ISVs);
# we operationalize the checkable half — the acquirer being meaningfully LARGER than
# the target — via #165's size-plausibility helper. A qualifier we don't model (or one
# whose size data is missing) does NOT fire, so a rule never contributes unearned points.
THESIS_LARGER_QUALIFIERS = frozenset({"domain-focused"})

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


def _pos_int(value) -> int | None:
    """A strictly-positive int, or None (missing/zero/garbage headcounts are neutral)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _norm_kind(value) -> str | None:
    """Canonical kind token (trimmed, lower, spaces→underscores), or None when absent.

    Mirrors ``app.graph.thesis._normalise_kind`` so a candidate's ``a.kind`` and a
    rule's stored ``acquirerKind``/``targetKind`` compare on the same footing; a
    missing/blank/non-string kind is None, which the thesis matcher treats as neutral.
    """
    if not isinstance(value, str):
        return None
    normalised = "_".join(value.strip().lower().split())
    return normalised or None


def _confidence(value) -> float:
    """A rule confidence clamped to ``[0, 1]``; garbage/absent falls back to the seed
    prior 0.5 so a malformed rule still scales sanely rather than zeroing out."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(max(c, 0.0), 1.0)


def _size_plausibility(acq_headcount, target_headcount) -> tuple[int, dict] | None:
    """Relative-size signal: bonus when the acquirer is >= SIZE_LARGER_RATIO x the
    target, penalty when it is smaller, neutral (None) in between or when either side
    is unknown. Both directions carry the actual numbers so the UI stays explainable."""
    acq, tgt = _pos_int(acq_headcount), _pos_int(target_headcount)
    if acq is None or tgt is None:
        return None
    ratio = acq / tgt
    if ratio >= SIZE_LARGER_RATIO:
        points, direction = W_SIZE_PLAUSIBLE, "larger"
    elif ratio < 1:
        points, direction = W_SIZE_SMALLER, "smaller"
    else:
        return None
    detail = {
        "acquirer_headcount": acq,
        "target_headcount": tgt,
        "ratio": round(ratio, 1),
        "direction": direction,
    }
    return points, {"signal": "size-plausible", "detail": detail}


def _size_fit(target_headcount, past_headcounts, past_amounts) -> tuple[int, dict] | None:
    """Historical target-size fit: bonus when the target's headcount sits within (or
    within SIZE_FIT_TOLERANCE of) the [min, max] range of the acquirer's past targets'
    headcounts. Neutral when the target has no headcount or no past target does. Cited
    deal amounts, where present, ride along in the detail as supporting context."""
    tgt = _pos_int(target_headcount)
    known = [n for n in (_pos_int(h) for h in past_headcounts or []) if n is not None]
    if tgt is None or not known:
        return None
    low, high = min(known), max(known)
    if not (low / SIZE_FIT_TOLERANCE <= tgt <= high * SIZE_FIT_TOLERANCE):
        return None
    detail: dict = {"low": low, "high": high, "n": len(known)}
    amounts = [a.strip() for a in past_amounts or [] if isinstance(a, str) and a.strip()]
    if amounts:
        detail["amounts"] = amounts
    return W_SIZE_FIT, {"signal": "size-fit", "detail": detail}


def _size_signals(cand: dict, target_headcount) -> tuple[int, list[dict]]:
    """All size scoring for one candidate, extracted from :func:`_score_candidate` so
    that its branch complexity stays under the repo's lint cap. Returns the size points
    to add and the size ``why`` entries; both are empty when no size comparison fires."""
    points = 0
    whys: list[dict] = []
    for result in (
        _size_plausibility(cand.get("acquirer_headcount"), target_headcount),
        _size_fit(
            target_headcount, cand.get("past_target_headcounts"), cand.get("past_target_amounts")
        ),
    ):
        if result is not None:
            pts, why = result
            points += pts
            whys.append(why)
    return points, whys


def _qualifier_holds(qualifier: str, cand: dict, target_headcount) -> bool:
    """Whether a rule's qualifier condition holds for this candidate.

    Unqualified rules always hold. A ``domain-focused`` rule composes with #165's
    size-plausibility — it holds only when the acquirer is meaningfully LARGER than the
    target (the 'larger services companies' half of the thesis we can actually check).
    A qualifier we don't model, or one whose size data is missing, does NOT hold, so the
    rule stays neutral rather than firing unearned."""
    if not qualifier:
        return True
    if qualifier in THESIS_LARGER_QUALIFIERS:
        result = _size_plausibility(cand.get("acquirer_headcount"), target_headcount)
        return result is not None and result[1]["detail"]["direction"] == "larger"
    return False


def _thesis_signals(
    cand: dict, target_kind, target_headcount, thesis_rules: list[dict] | None
) -> tuple[float, list[dict]]:
    """Thesis-match scoring for one candidate against the active :ThesisRules.

    The candidate's own kind → the target's kind, matched against each rule's
    ``(acquirerKind, targetKind)``. A matched rule adds points scaled by its confidence
    and a ``thesis-match`` why-entry citing its statement + evidence count; qualified
    rules fire only when :func:`_qualifier_holds`. Missing candidate/target kind — or no
    rules — is strictly neutral (returns integer ``0`` points and no reasons), so a
    candidate with no kind data ranks byte-identically to the pre-#194 ranking.
    Extracted from :func:`_score_candidate` so its branch complexity stays under the cap.
    """
    acq_kind = _norm_kind(cand.get("acquirer_kind"))
    tgt_kind = _norm_kind(target_kind)
    if acq_kind is None or tgt_kind is None or not thesis_rules:
        return 0, []
    points: float = 0
    whys: list[dict] = []
    for rule in thesis_rules:
        if _norm_kind(rule.get("acquirer_kind")) != acq_kind:
            continue
        if _norm_kind(rule.get("target_kind")) != tgt_kind:
            continue
        qualifier = (rule.get("qualifier") or "").strip()
        if not _qualifier_holds(qualifier, cand, target_headcount):
            continue
        confidence = _confidence(rule.get("confidence"))
        points += round(W_THESIS_MATCH * confidence, 2)
        detail: dict = {
            "statement": rule.get("statement"),
            "confidence": confidence,
            "evidence": int(rule.get("evidence_count") or 0),
            "acquirer_kind": acq_kind,
            "target_kind": tgt_kind,
        }
        if qualifier:
            detail["qualifier"] = qualifier
        whys.append({"signal": "thesis-match", "detail": detail})
    return points, whys


def _score_candidate(
    cand: dict,
    target_kind: str | None,
    target_headcount: int | None = None,
    thesis_rules: list[dict] | None = None,
) -> dict | None:
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

    # Size awareness only reweights a candidate that already cleared the relevance
    # gate above — it never gates one in, and absent size data contributes nothing.
    size_points, size_whys = _size_signals(cand, target_headcount)
    thesis_points, thesis_whys = _thesis_signals(cand, target_kind, target_headcount, thesis_rules)
    activity_bonus = W_ACTIVITY * min(max(total - 1, 0), ACTIVITY_CAP)
    score = (
        W_TOPIC_DEAL * len(topic_deals)
        + W_KIND_DEAL * len(kind_deals)
        + (W_DIRECT_PARTNER if is_partner else 0)
        + W_SHARED_PARTNER * len(shared_partners)
        + W_SHARED_CLIENT * len(shared_clients)
        + activity_bonus
        + size_points
        + thesis_points
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
    why.extend(size_whys)
    why.extend(thesis_whys)

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
    target_headcount: int | None = None,
    thesis_rules: list[dict] | None = None,
    limit: int = ACQUIRERS_DEFAULT,
) -> list[dict]:
    """Pure ranking: turn raw per-candidate facts into an ordered candidate list.

    Each input dict carries the raw signals gathered by the Cypher
    (``topic_deals``/``kind_deals`` as ``[{target, source}]`` lists,
    ``shared_partners``/``shared_clients`` as name lists, ``is_direct_partner``,
    ``total_acquisitions``, the #165 size facts ``acquirer_headcount`` /
    ``past_target_headcounts`` / ``past_target_amounts``, and the candidate's own
    ``acquirer_kind`` for the #194 thesis match). ``target_headcount`` is the open
    company's own headcount; size signals fire only when both sides exist, so a None
    here (or absent candidate size data) leaves the pre-#165 ranking untouched.
    ``thesis_rules`` is the active :class:`ThesisRule` set (fetched ONCE per ranking,
    not per candidate); a match on candidate-kind→target-kind adds a confidence-scaled
    ``thesis-match`` reason, and missing kind data (either side) or no rules is strictly
    neutral. Candidates with no relevant tie to the target are dropped; the rest are
    ordered by score desc then acquirer name (deterministic for stable rendering) and
    capped.
    """
    ranked = [
        scored
        for cand in candidates
        if (scored := _score_candidate(cand, target_kind, target_headcount, thesis_rules))
    ]
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
      COUNT { (a)-[:ACQUIRED]->(:Company) } AS total_acquisitions,
      a.kind AS acquirer_kind,
      a.headcount AS acquirer_headcount,
      [ (a)-[:ACQUIRED]->(x:Company) WHERE x.headcount IS NOT NULL
        | x.headcount ] AS past_target_headcounts,
      [ (a)-[r:ACQUIRED]->(:Company) WHERE r.amount IS NOT NULL
        | r.amount ] AS past_target_amounts
    WITH a.name AS acquirer, topic_deals, kind_deals, shared_partners, shared_clients,
         is_direct_partner, total_acquisitions, acquirer_kind, acquirer_headcount,
         past_target_headcounts, past_target_amounts
    WHERE size(topic_deals) > 0 OR size(kind_deals) > 0 OR size(shared_partners) > 0
       OR size(shared_clients) > 0 OR is_direct_partner
    RETURN acquirer, topic_deals, kind_deals, shared_partners, shared_clients,
           is_direct_partner, total_acquisitions, acquirer_kind, acquirer_headcount,
           past_target_headcounts, past_target_amounts
"""


async def potential_acquirers(
    driver: AsyncDriver, name: str, *, limit: int = ACQUIRERS_DEFAULT
) -> list[dict] | None:
    """Ranked candidate acquirers for the tracked company ``name`` (story #44).

    Gathers each candidate's raw signals — acquisitions of companies in the
    target's topic / of the target's kind, shared partners/clients, an existing
    partnership, overall acquisition activity, (for #165) the acquirer's own
    headcount plus its past targets' headcounts and cited deal amounts, and (for #194)
    the acquirer's own kind — then ranks them with the pure
    :func:`rank_acquirer_candidates`. The active thesis rules are fetched ONCE here (not
    per candidate) and threaded into the pure ranker. Returns None if ``name`` is not a
    company (so the route can 404); an empty list means no acquirer has any tie to it.
    """
    async with driver.session() as session:
        exists = await session.run(
            "MATCH (c:Company {name: $name}) RETURN c.kind AS kind, c.headcount AS headcount",
            name=name,
        )
        record = await exists.single()
        if record is None:
            return None
        target_kind = record["kind"]
        target_headcount = record["headcount"]
        result = await session.run(_CANDIDATES_CYPHER, name=name)
        rows = [rec.data() async for rec in result]
    # Fetch the active thesis rules ONCE per ranking (not per candidate), after the
    # target is confirmed so a 404 costs no extra query.
    thesis_rules = await get_thesis_rules(driver)
    return rank_acquirer_candidates(
        rows,
        target_kind=target_kind,
        target_headcount=target_headcount,
        thesis_rules=thesis_rules,
        limit=limit,
    )


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
