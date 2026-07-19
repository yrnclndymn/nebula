"""Company classification review: propose end-customer stubs, let a human decide.

Enrichment pulls in bare `:Company` stubs — organisations that only ever appear
as the object of a HAS_CLIENT edge (banks, retailers, public bodies), plus the
occasional extraction-noise name. This surfaces them for review behind the same
propose→review→commit durable-job pattern as entity resolution and back-fill
(the enqueue/execute/commit ceremony lives in `app.graph.jobs`), so it survives
Cloud Run scale-to-zero and never writes silently.

`enqueue_classification` creates a job; the runner scans stubs with the client
heuristic (`entity_resolution.list_client_stub_candidates`) and pre-suggests a
per-candidate action; the user reviews and commits a batch of per-name
**decisions** — the only thing that touches the graph.

Each decision is `{name, action}` where `action` is a company KIND (client, ISV,
cloud/service provider — the reviewer relabels the stub) or `'remove'` (a HARD
delete of a true stub, e.g. extraction junk). Both paths are TOCTOU-guarded at
commit: kind writes go through `entity_resolution.classify_stub_kinds` (the full
scan predicate re-checked, so a promoted stub is refused, not clobbered) and
removals through `entity_resolution.remove_stub_companies` (researched companies
refused). The heuristic only proposes.
"""

from app.graph import entity_resolution as er
from app.graph import jobs
from app.graph.driver import get_driver
from app.graph.models import KINDS

# A per-candidate decision either relabels the stub to a company KIND or removes
# it outright. 'remove' is a hard delete, guarded to true stubs in the mutation.
REMOVE = "remove"
VALID_ACTIONS = frozenset(KINDS) | {REMOVE}


def suggested_action(name: str) -> str:
    """The action the UI pre-selects for a scanned candidate.

    Extraction noise (`looks_like_junk`) defaults to 'remove'; everything else
    defaults to 'client' (the common case — an end-customer stub). Only a
    suggestion: the reviewer overrides it per row before committing.
    """
    return REMOVE if er.looks_like_junk(name) else "client"


def partition_decisions(
    decisions: list[dict],
) -> tuple[list[tuple[str, str]], list[str], list[dict]]:
    """Split raw `{name, action}` commit decisions into kind-writes, removals and
    invalid entries. PURE (no DB) so the endpoint rejects a malformed batch before
    touching the graph.

    Returns `(kind_writes, remove_names, invalid)` where `kind_writes` is a list of
    `(name, kind)` pairs, `remove_names` the names to hard-delete, and `invalid`
    the decisions with an unknown action or a missing name.
    """
    kind_writes: list[tuple[str, str]] = []
    remove_names: list[str] = []
    invalid: list[dict] = []
    for d in decisions or []:
        # A non-string name (e.g. a number in a hand-crafted payload) must land in
        # `invalid`, not crash the endpoint on .strip() (PR #188 review r2).
        raw_name = d.get("name") if isinstance(d, dict) else None
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        action = d.get("action") if isinstance(d, dict) else None
        if not name or action not in VALID_ACTIONS:
            invalid.append(d)
        elif action == REMOVE:
            remove_names.append(name)
        else:
            kind_writes.append((name, action))
    return kind_writes, remove_names, invalid


async def enqueue_classification() -> dict:
    """Kick off a background scan for classification candidates and prepare a
    reviewable batch. Returns immediately; the scan runs as a durable job."""
    return await jobs.enqueue_scan_job("classification", {"candidates": [], "stub_count": 0})


async def execute_classification_job(job_id: str) -> None:
    """Job runner: scan stubs, pre-suggest a per-candidate action, mark ready."""

    async def scan(_job: dict) -> dict:
        candidates = await er.list_client_stub_candidates(get_driver())
        for c in candidates:
            c["suggested"] = suggested_action(c["name"])
        return {"candidates": candidates, "stub_count": len(candidates)}

    await jobs.execute_scan_job(job_id, scan)


async def get_classification(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def commit_classification(job_id: str, decisions: list[dict]) -> dict:
    """Apply the reviewer's per-name decisions. Called by the UI, not the agent.

    BOTH decision paths are TOCTOU-guarded at commit (PR #188 review): kind
    writes go through `classify_stub_kinds`, which re-runs the FULL scan
    predicate so a stub promoted while review sat open is refused rather than
    having its researched kind clobbered; 'remove' decisions hard-delete via
    `remove_stub_companies`, which refuses researched companies. A malformed
    batch (unknown action / missing name) is rejected wholesale before any
    write. Returns counts plus every refused name (both paths combined).
    """
    job = await jobs.get_ready_job(job_id)
    if job is None:
        return {"error": "classification job not found or not ready"}

    kind_writes, remove_names, invalid = partition_decisions(decisions)
    if invalid:
        return {"error": "invalid classification decisions"}

    driver = get_driver()
    classified, refused_kinds = await er.classify_stub_kinds(driver, kind_writes)
    removed, refused_removals = await er.remove_stub_companies(driver, remove_names)

    await jobs.mark_committed(job_id, job)
    return {
        "classified": len(classified),
        "removed": len(removed),
        "refused": refused_kinds + refused_removals,
    }
