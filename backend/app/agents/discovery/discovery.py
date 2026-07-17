"""Durable web-discovery job: cohort profile → web search → deduped candidates.

Stored in the graph (via `app.graph.jobs`) so it survives Cloud Run scale-to-zero
and shows on the activity page like every other job. `enqueue_discovery` seeds the
job from the in-graph similar cohort; `execute_discovery_job` builds the profile,
searches, extracts + dedups candidates, all under a per-run budget; the user
reviews and selects candidates, which flow into the EXISTING research trigger
(`propose_enrichment`, ≤10 cap) — never an auto-write.
"""

import asyncio
import uuid

from app.agents.discovery.dedup import filter_new, known_index
from app.agents.discovery.extract import extract_candidates
from app.agents.discovery.profile import build_profile
from app.agents.discovery.search import build_queries
from app.budget import BudgetExhausted, budget_for, use_budget
from app.config import settings
from app.genai_retry import QuotaExhausted
from app.graph import jobs, queries
from app.graph.driver import get_driver
from app.tools.web import web_search

# Sanity cap on one "research selected" request — mirrors the backlog trigger
# (app.api.routes.MAX_BACKLOG_RESEARCH). Discovery candidates are untrusted web
# finds, so the guard against a costly mis-click is a hard ceiling per request.
MAX_DISCOVERY_RESEARCH = 10


async def enqueue_discovery(seed_name: str) -> dict:
    """Kick off discovery for a researched company. Seeds the job from its in-graph
    similar cohort (the shipped `similar_companies`), which becomes the search
    template. Returns immediately; the work runs in the background.

    404-shaped ({"error": ...}) if the seed isn't a company; a friendly note if it
    has no similar cohort to base a search on (nothing to template from)."""
    driver = get_driver()
    cohort = await queries.similar_companies(driver, seed_name, limit=queries.SIMILAR_DEFAULT)
    if cohort is None:
        return {"error": f"no company named {seed_name!r}"}
    if not cohort:
        return {
            "seed": seed_name,
            "candidates": 0,
            "note": "no similar in-graph cohort to search from",
        }

    job_id = uuid.uuid4().hex[:8]
    await jobs.create_job(
        job_id,
        "discovery",
        {
            "job_id": job_id,
            "status": "pending",
            "name": seed_name,  # the activity page's generic label
            "seed": seed_name,
            "cohort": [c["name"] for c in cohort],
            "queries": [],
            "candidates": [],
        },
    )
    await jobs.enqueue(job_id)
    return {"job_id": job_id, "seed": seed_name, "cohort": len(cohort)}


async def execute_discovery_job(job_id: str) -> None:
    """Job runner: build the profile, search the web, extract + dedup candidates."""
    job = await jobs.get_job(job_id)
    if job is None:
        return
    driver = get_driver()
    # Per-run budget: "discovery" defaults from settings, overridable by the job's
    # payload. The tool helpers (web_search / the profile's LLM summary) charge it
    # as they spend; None = unlimited (interactive/unconfigured).
    budget = budget_for("discovery", job.get("budget"))
    exhausted: BudgetExhausted | None = None
    try:
        with use_budget(budget):
            profile = await build_profile(driver, job["seed"], job["cohort"])
            search_queries = build_queries(profile)

            results: list[dict] = []
            try:
                for query in search_queries:
                    hits = (await asyncio.to_thread(web_search, query)).get("results", [])
                    results.extend(hits)
            except BudgetExhausted as exc:
                # A search cap tripped mid-sweep: keep the results gathered so far;
                # a budget cap is a graceful stop, not an error.
                exhausted = exc

            # The seed + cohort are already in the graph, so the name/domain dedup
            # drops them automatically — no special-casing needed.
            candidates = extract_candidates(results, profile.terms)
            name_keys, domains = await known_index(driver)
            new = filter_new(candidates, name_keys, domains)
    except QuotaExhausted as exc:
        await jobs.update_job(
            job_id,
            {**job, "error": exc.message, "error_detail": exc.detail},
            status="error",
        )
        return
    except Exception as exc:  # noqa: BLE001 — surface discovery failures to the client
        await jobs.update_job(job_id, {**job, "error": str(exc)}, status="error")
        return

    final = {
        **job,
        "seed": profile.seed,
        "profile": profile.to_dict(),
        "queries": search_queries,
        "candidates": new,
        "total_found": len(candidates),
        "outcome": f"found {len(new)} new candidate companies like {profile.seed}",
    }
    if exhausted is not None:
        final["budget_exhausted"] = {
            "limit": exhausted.limit,
            "cap": exhausted.cap,
            "reached": exhausted.count,
        }
    if budget is not None:
        final["budget_usage"] = budget.usage()
    await jobs.update_job(job_id, final, status="ready")


async def get_discovery(job_id: str) -> dict | None:
    return await jobs.get_job(job_id)


async def research_candidates(job_id: str, names: list[str]) -> dict:
    """Feed user-selected candidates into the EXISTING research pipeline.

    Only candidates that belong to this job's reviewed list are accepted (a client
    can't smuggle in arbitrary names); each is enriched via the same
    `propose_enrichment` trigger the backlog uses, seeded with the candidate's
    discovered website so enrichment doesn't re-discover it. Capped at
    MAX_DISCOVERY_RESEARCH and staggered so a batch doesn't exhaust Gemini quota.
    Nothing is written — each returns a proposal the user reviews and commits.
    """
    # Late import: proposals pulls in the ADK enrichment chain, and this module is
    # itself late-imported by the job dispatcher.
    from app.agents.assistant.proposals import propose_enrichment

    job = await jobs.get_job(job_id)
    if job is None:
        return {"error": "unknown discovery job"}

    by_name = {c["name"].lower(): c for c in job.get("candidates", [])}
    selected, seen = [], set()
    for raw in names:
        key = raw.strip().lower()
        if key and key in by_name and key not in seen:
            seen.add(key)
            selected.append(by_name[key])
    if not selected:
        return {"error": "no matching candidates in this job"}
    selected = selected[:MAX_DISCOVERY_RESEARCH]

    proposals = []
    for i, cand in enumerate(selected):
        started = await propose_enrichment(
            cand["name"],
            website=cand.get("website", ""),
            enqueue_delay=i * settings.research_stagger_seconds,
        )
        proposals.append({"name": cand["name"], "proposal_id": started["proposal_id"]})
    return {"proposals": proposals, "cap": MAX_DISCOVERY_RESEARCH}
