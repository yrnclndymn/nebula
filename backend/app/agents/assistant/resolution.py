"""Entity-resolution review: detect variant clusters, let a human dispose.

Follows the back-fill pattern (durable graph-backed job → poll → commit) so it
survives Cloud Run scale-to-zero. `start_resolution` creates a job and enqueues
it; the runner scans the stub companies, runs the heuristics, and stores the
proposed clusters + junk suggestions; the user reviews and commits a set of
*decisions* (merge / alias / junk), which are the only things that touch the
graph. No cluster is ever merged silently — detection proposes, the user
disposes, and merges are irreversible.
"""

import uuid

from app.graph import entity_resolution as er
from app.graph import jobs
from app.graph.driver import get_driver


async def start_resolution() -> dict:
    """Kick off a background scan for duplicate/junk company stubs and prepare a
    reviewable batch. Returns immediately; the scan runs as a durable job."""
    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "resolution",
        {"job_id": job_id, "status": "pending", "clusters": [], "junk": [], "stub_count": 0},
    )
    await jobs.enqueue(job_id)
    return {"job_id": job_id, "status": "scanning in the background"}


async def run_resolution_job(job_id: str) -> None:
    """Job runner: scan stubs, propose clusters + junk candidates, mark ready."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    try:
        stubs = await er.list_stub_companies(driver)
        by_name = {s["name"]: s for s in stubs}
        names = [s["name"] for s in stubs]

        clusters = er.detect_variant_clusters(names)
        # Attach edge counts so the reviewer sees which member is best-connected,
        # and default the canonical to the most-connected member when that beats
        # the heuristic's descriptive pick (a bare alias shouldn't win).
        for cluster in clusters:
            cluster["members"] = [
                {"name": m, "edges": by_name.get(m, {}).get("edges", 0)} for m in cluster["members"]
            ]
            best = max(cluster["members"], key=lambda m: m["edges"])
            if best["edges"] > 0:
                cluster["canonical"] = best["name"]

        junk = [
            {"name": s["name"], "edges": s["edges"]} for s in stubs if er.looks_like_junk(s["name"])
        ]

        await jobs.update_job(
            job_id,
            {**job, "clusters": clusters, "junk": junk, "stub_count": len(stubs)},
            status="ready",
        )
    except Exception as exc:  # noqa: BLE001 — surface scan failures to the client
        await jobs.update_job(job_id, {**job, "error": str(exc)}, status="error")


async def get_resolution(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_resolution(job_id: str, decisions: list[dict]) -> dict:
    """Apply reviewed decisions to the graph. Called by the UI, not the agent.

    Each decision is one of:
      {"action": "merge", "canonical": str, "variants": [str, ...]}
      {"action": "alias", "canonical": str, "aliases":  [str, ...]}
      {"action": "junk",  "names":     [str, ...]}

    Unknown actions/names are skipped rather than erroring, so a partially stale
    batch (a node already merged by an earlier decision) still commits cleanly.
    """
    job = await jobs.get_job(job_id)
    if job is None or job.get("status") != "ready":
        return {"error": "resolution job not found or not ready"}
    driver = get_driver()

    # A scoped, user-named merge job (see merge.py) relaxes the promoted-variant
    # guard: the user explicitly named these records as the same organisation, so a
    # researched variant is intended. Scan jobs keep the guard (allow_researched
    # stays False), so a stale scan can never delete a node that became researched.
    allow_researched = bool(job.get("scoped_merge"))

    # Decisions must be drawn from what THIS job proposed. Without this check any
    # authenticated caller could mint a scoped_merge job and then commit arbitrary
    # canonical/variants under the relaxed guard — the relaxation would bind to the
    # job while the names came from the request. A decision naming anything outside
    # the job's clusters (or junk list) is rejected, not applied.
    cluster_sets = [
        {m.get("name") for m in c.get("members", [])} | {c.get("canonical")}
        for c in job.get("clusters", [])
    ]
    proposed_junk = {m.get("name") for m in job.get("junk", [])}

    def _within_one_cluster(names: set) -> bool:
        return any(names <= cluster for cluster in cluster_sets)

    merged = 0
    aliased = 0
    flagged = 0
    rejected = 0
    for decision in decisions:
        action = decision.get("action")
        if action == "merge":
            names = {decision.get("canonical", "")} | set(decision.get("variants", []))
            if not _within_one_cluster(names):
                rejected += 1
                continue
            result = await er.merge_companies(
                driver,
                decision.get("canonical", ""),
                decision.get("variants", []),
                allow_researched=allow_researched,
            )
            merged += len(result.get("merged", []))
        elif action == "alias":
            names = {decision.get("canonical", "")} | set(decision.get("aliases", []))
            if not _within_one_cluster(names):
                rejected += 1
                continue
            aliases = await er.add_aliases(
                driver, decision.get("canonical", ""), decision.get("aliases", [])
            )
            aliased += len(aliases)
        elif action == "junk":
            names = set(decision.get("names", []))
            all_proposed = proposed_junk.union(*cluster_sets) if cluster_sets else proposed_junk
            allowed = names & all_proposed
            if names - allowed:
                rejected += 1
            if allowed:
                flagged += await er.flag_junk(driver, sorted(allowed))

    # Flip status so a stale double-POST is rejected by the ready-guard above
    # (the ops are idempotent-safe, but there is no reason to redo them).
    await jobs.update_job(job_id, {**job, "committed": True}, status="committed")
    return {"merged": merged, "aliased": aliased, "flagged": flagged, "rejected": rejected}
