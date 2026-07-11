"""Chat-proposed company merges (propose → review → commit).

The scan flow (`resolution.py`) can only merge bare *stubs*: its scan lists
un-researched companies, and its TOCTOU guard skips any variant that turns out to
be researched. That is right for a scan — a "stub" that gained a website while
review sat open was never meant to be merged. But it means the assistant cannot
help when the user points at two *named* records ("merge Acme and Acme Inc — same
company"), especially when one or both sides are already researched.

This module fills that gap. `propose_merge` is the assistant tool: it builds a
scoped merge job over exactly the named companies (no scan), surfaced in chat as a
reviewable card via the `turn_merges` ContextVar. Nothing is written here — the
job reuses the existing resolution commit path (`commit_resolution`), so only the
user's explicit commit runs `merge_companies`. The assistant can never merge.

Researched-variant semantics (the deliberate choice for this flow):
  - The survivor (canonical) is forced to be the RESEARCHED side. If the user's
    named canonical is a bare stub but a variant is researched, we promote the
    best-connected researched node to canonical so the richer record survives and
    the stub merely contributes its edges + name-as-alias.
  - When BOTH sides are researched, the user's canonical is kept and the merge
    runs with the promoted-variant guard RELAXED (`scoped_merge` marker →
    `allow_researched=True` at commit). This is safe because `merge_companies`
    unions props into the canonical's GAPS only (never overwrites) and re-points
    edges + provenance, so the survivor keeps its researched data; the only value
    dropped is a conflicting field on the variant, which is inherent to merging
    two records the user asserted are the same organisation.
The card spells out what will happen so the user disposes with full context.
"""

import uuid
from contextvars import ContextVar

from app.graph import jobs
from app.graph.driver import get_driver

# Merges proposed during the current chat turn (read by the /chat endpoint).
turn_merges: ContextVar[list | None] = ContextVar("nebula_turn_merges", default=None)


async def _lookup_members(driver, names: list[str]) -> list[dict]:
    """For each named company that exists, return {name, edges, researched}.

    `researched` mirrors the promoted test in `merge_companies` (a topic tag or a
    website), so the canonical choice below lines up with what the merge treats as
    researched. Unknown names are dropped — the caller reports the shortfall.
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company) WHERE c.name IN $names
            OPTIONAL MATCH (c)-[r]-()
            RETURN c.name AS name, count(r) AS edges,
                   (EXISTS { (c)-[:TAGGED_AS]->(:Topic) } OR c.website IS NOT NULL) AS researched
            """,
            names=names,
        )
        found = {rec["name"]: dict(rec) async for rec in result}
    # Preserve caller order for stable output.
    return [found[n] for n in dict.fromkeys(names) if n in found]


async def start_merge(canonical: str, variants: list[str]) -> dict:
    """Create a scoped, user-named merge job (no scan) and return it immediately.

    Validates that the named companies exist and picks the survivor so researched
    data is preserved (see the module docstring). The job is durable and starts
    `ready` — there is no background work; the cluster IS the named companies — so
    the user can commit it straight from the review card via `commit_resolution`.
    """
    named = [canonical, *[v for v in variants if v]]
    members = await _lookup_members(get_driver(), named)
    if len(members) < 2:
        # Nothing to merge: a typo, a not-yet-added company, or only one match.
        found = [m["name"] for m in members]
        return {"merged": 0, "note": f"need two known companies to merge; found {found}"}

    by_name = {m["name"]: m for m in members}
    chosen = canonical if canonical in by_name else members[0]["name"]
    canonical_reason = ""
    # Protect researched data: the survivor must be a researched node when one is
    # present. If the user's canonical is a bare stub but a variant is researched,
    # promote the best-connected researched member instead of deleting it.
    if not by_name[chosen]["researched"]:
        researched = [m for m in members if m["researched"]]
        if researched:
            promoted = max(researched, key=lambda m: m["edges"])
            if promoted["name"] != chosen:
                canonical_reason = (
                    f"{promoted['name']} is already researched, so it is kept as the "
                    "survivor to preserve its data"
                )
                chosen = promoted["name"]

    ordered = [by_name[chosen], *[m for m in members if m["name"] != chosen]]
    job_id = uuid.uuid4().hex[:8]
    payload = {
        "job_id": job_id,
        "status": "ready",  # the cluster is the named companies — no scan needed
        "scoped_merge": True,  # marker: relax the promoted-variant guard on commit
        "stub_count": len(members),
        "junk": [],
        "clusters": [
            {
                "canonical": chosen,
                "members": [
                    {"name": m["name"], "edges": m["edges"], "researched": m["researched"]}
                    for m in ordered
                ],
                "reason": "user",
            }
        ],
        "canonical_reason": canonical_reason,
    }
    await jobs.create_job(job_id, "resolution", payload)

    ref = {
        "job_id": job_id,
        "canonical": chosen,
        "members": payload["clusters"][0]["members"],
        "canonical_reason": canonical_reason,
    }
    collected = turn_merges.get()
    if collected is not None:
        collected.append(ref)
    return {"job_id": job_id, **{k: ref[k] for k in ("canonical", "members", "canonical_reason")}}


async def propose_merge(canonical: str, variants: list[str]) -> dict:
    """Propose merging duplicate company records that are the SAME organisation, for
    the user to review and commit. Returns immediately and does NOT merge anything —
    only the user's commit on the review card runs the merge (human-in-the-loop).

    Call this when the user says two or more records are the same company and should
    be merged ("merge X and Y", "X and Y are the same company, combine them"). Pass
    canonical = the name to KEEP (the surviving record) and variants = the other
    name(s) that should fold into it and become aliases. Works whether or not the
    companies are researched. If a researched company must survive, the tool keeps
    it as the canonical automatically. After calling, tell the user a merge proposal
    will appear to review and commit — never claim you merged anything yourself.
    """
    return await start_merge(canonical, [v for v in variants if v and v != canonical])
