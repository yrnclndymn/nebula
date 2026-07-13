"""HTTP routes: health checks, graph read endpoints, and the assistant chat."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.assistant.backfill import commit_backfill, get_backfill
from app.agents.assistant.classification import (
    commit_classification,
    get_classification,
    start_classification,
)
from app.agents.assistant.proposals import commit_proposal, get_proposal, propose_enrichment
from app.agents.assistant.resolution import (
    commit_resolution,
    get_resolution,
    start_resolution,
)
from app.agents.assistant.service import respond
from app.agents.discovery.discovery import (
    get_discovery,
    research_candidates,
    start_discovery,
)
from app.capture.job import get_signal_capture, start_signal_capture
from app.capture.news import get_news_capture, start_news_capture
from app.config import settings
from app.graph import cache, digest, jobs, queries, retention, schedules, signals
from app.graph.driver import check_connectivity, get_driver
from app.graph.models import APPLIES_TO, KINDS, field_key

router = APIRouter()  # user-facing routes (Firebase-authed in prod)
public_router = APIRouter()  # health checks (always open)
tasks_router = APIRouter()  # Cloud Tasks callbacks (OIDC-authed in prod)


@public_router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the API process is up. Does not touch the database."""
    return {"status": "ok"}


@public_router.get("/health/graph")
async def health_graph() -> JSONResponse:
    """Readiness: can we reach Neo4j? Returns 503 if not."""
    try:
        await check_connectivity()
    except Exception as exc:  # noqa: BLE001 — surface any driver/connection error
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": str(exc)},
        )
    return JSONResponse(content={"status": "ok"})


@router.get("/companies")
async def companies(
    topic: str | None = None,
    q: str | None = None,
    company_type: str | None = None,
    kind: str | None = None,
    country: str | None = None,
    headcount_min: int | None = Query(default=None, ge=0),
    headcount_max: int | None = Query(default=None, ge=0),
) -> list[dict]:
    """Researched companies (with aggregates), optionally filtered."""
    return await queries.list_companies(
        get_driver(),
        topic=topic,
        q=q,
        company_type=company_type,
        kind=kind,
        country=country,
        headcount_min=headcount_min,
        headcount_max=headcount_max,
    )


@router.get("/backlog")
async def backlog(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Ranked research backlog: un-researched stub companies (no topic tag, no
    website) referenced by researched companies, ranked by mention count with a
    boost for cloud_provider/isv partnerships. Excludes junk and end-customer
    (kind='client') stubs. Paginated via limit/offset."""
    return await queries.research_backlog(get_driver(), limit=limit, offset=offset)


# Sanity cap on one "research selected" request. Proposal jobs are unbudgeted LLM
# work (the per-run budget guards scheduled fan-out, not interactive proposals),
# so the guard against a costly mis-click is a hard cap on how many a single
# request may enqueue.
MAX_BACKLOG_RESEARCH = 10


class BacklogResearchRequest(BaseModel):
    names: list[str]


@router.post("/backlog/research")
async def backlog_research(req: BacklogResearchRequest) -> dict:
    """Enqueue enrichment research for selected backlog stubs (issue #31).

    Each stub has no website, so its proposal job discovers the official site first
    and then runs the normal enrichment. Returns one proposal id per name for the
    client to poll and review — nothing is written until the user commits each
    proposal (HITL). Capped at MAX_BACKLOG_RESEARCH companies per request."""
    names, seen = [], set()
    for raw in req.names:
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    if not names:
        raise HTTPException(status_code=422, detail="no company names given")
    if len(names) > MAX_BACKLOG_RESEARCH:
        raise HTTPException(
            status_code=422,
            detail=f"at most {MAX_BACKLOG_RESEARCH} companies per request (got {len(names)})",
        )
    # Stagger the batch so the jobs don't all fire at once and exhaust the
    # free-tier Gemini quota (issue #65): the i-th proposal starts i*gap later.
    proposals = []
    for i, name in enumerate(names):
        started = await propose_enrichment(
            name, website="", enqueue_delay=i * settings.research_stagger_seconds
        )
        proposals.append({"name": name, "proposal_id": started["proposal_id"]})
    return {"proposals": proposals, "cap": MAX_BACKLOG_RESEARCH}


class KindRequest(BaseModel):
    kind: str | None


@router.patch("/companies/{name}/kind")
async def set_kind(name: str, req: KindRequest) -> dict:
    """Set a company's kind (service_provider / isv / cloud_provider / client), or
    null. Validated against KINDS, so 'client' is accepted automatically."""
    if req.kind is not None and req.kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {KINDS}")
    if not await queries.set_company_kind(get_driver(), name, req.kind):
        raise HTTPException(status_code=404, detail=f"No company named {name!r}")
    return {"name": name, "kind": req.kind}


@router.get("/companies/{name}")
async def company_detail(name: str) -> dict:
    """One company with its relationships (partners, clients, leadership)."""
    company = await queries.get_company(get_driver(), name)
    if company is None:
        raise HTTPException(status_code=404, detail=f"No company named {name!r}")
    return company


@router.get("/companies/{name}/graph")
async def company_graph(name: str) -> dict:
    """A company node + its 1-hop typed edges (partners, clients, leaders, topics,
    types) for the interactive graph view. Fetched lazily per node so the client
    never renders the whole ~700-node graph at once."""
    result = await queries.company_neighbourhood(get_driver(), name)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No company named {name!r}")
    return result


@router.get("/companies/{name}/similar")
async def company_similar(
    name: str,
    limit: int = Query(default=queries.SIMILAR_DEFAULT, ge=1, le=queries.SIMILAR_MAX),
) -> list[dict]:
    """Most similar OTHER researched companies, with an explainable weighted-overlap
    score (shared clients/partners/topics, same kind, same country — each component
    returned). Excludes junk and end-customer (kind='client') stubs. 404 if unknown."""
    result = await queries.similar_companies(get_driver(), name, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No company named {name!r}")
    return result


class ChatRequest(BaseModel):
    session_id: str
    message: str


@router.post("/chat")
async def chat(req: ChatRequest) -> dict:
    """One conversational turn with the research assistant. Pass a stable
    session_id per client to keep multi-turn context. May return `proposals` —
    enrichment the assistant prepared for the user to review and commit."""
    turn = await respond(req.session_id, req.message)
    return {
        "reply": turn.reply,
        "proposals": turn.proposals,
        "backfills": turn.backfills,
        "merges": turn.merges,
    }


@router.get("/jobs")
async def list_jobs(
    type: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """Recent durable jobs (newest first), with optional type/status filters.
    Returns id/type/status/createdAt + a compact type-aware summary (for proposals:
    name, discovered_website, error; plus outcome/done/total/error_detail on any job
    that carries them) — the full dataJson stays on the per-id detail endpoints.
    Rehydrates in-progress research after a refresh (#66) and backs the
    agent-activity page (#48)."""
    return await jobs.list_jobs(get_driver(), type=type, status=status, limit=limit)


@router.delete("/jobs/{job_id}")
async def dismiss_job(job_id: str) -> dict:
    """Dismiss (delete) a finished/errored job from the activity views (#73).
    Pending jobs are refused — they're still queued to run. The UI confirms
    before dismissing a ready (un-reviewed) job."""
    job = await jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job.get("status") == "pending":
        raise HTTPException(status_code=409, detail="job is still running — not dismissable")
    await jobs.delete_job(job_id)
    return {"dismissed": job_id}


@router.get("/proposals/{proposal_id}")
async def proposal_status(proposal_id: str) -> dict:
    """Poll a background enrichment proposal until status is 'ready' (or 'error')."""
    proposal = await get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="unknown proposal")
    return proposal


class CommitRequest(BaseModel):
    proposal_id: str
    scope: str = "all"  # "focus" = just the asked-about field; "all" = full record


@router.post("/proposals/commit")
async def commit(req: CommitRequest) -> dict:
    """Write a reviewed proposal to the graph (the user's approval of an
    agent-prepared enrichment)."""
    result = await commit_proposal(req.proposal_id, req.scope)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/backfill/{job_id}")
async def backfill_status(job_id: str) -> dict:
    """Poll a back-fill job; rows fill in as companies are researched."""
    job = await get_backfill(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown back-fill job")
    return job


@tasks_router.post("/jobs/run/{job_id}")
async def run_job_endpoint(job_id: str) -> dict:
    """Runner invoked by Cloud Tasks (prod) to execute a job with CPU allocated.
    Not used in local mode (jobs run inline). Guarded by verify_task (OIDC)."""
    await jobs.run_job(job_id)
    return {"ran": job_id}


@tasks_router.post("/jobs/schedule-tick")
async def schedule_tick() -> dict:
    """Periodic trigger invoked by Cloud Scheduler (OIDC, verify_task) — selects
    due work from the schedule registry and enqueues it as durable jobs. Cheap and
    idempotent: Cloud Scheduler retries, and a double-tick within a cadence window
    enqueues nothing extra. Locally: `make schedule-tick`."""
    return await schedules.run_tick()


class BackfillCommitRequest(BaseModel):
    companies: list[str] | None = None


@router.post("/backfill/{job_id}/commit")
async def backfill_commit(job_id: str, req: BackfillCommitRequest) -> dict:
    """Write selected back-fill rows (companies=null commits all) with provenance."""
    result = await commit_backfill(job_id, req.companies)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/resolution/scan")
async def resolution_scan() -> dict:
    """Start a background scan for duplicate/junk company stubs. Returns a job id
    to poll; nothing is merged until the user commits reviewed decisions."""
    return await start_resolution()


@router.get("/resolution/{job_id}")
async def resolution_status(job_id: str) -> dict:
    """Poll an entity-resolution scan; proposed clusters + junk fill in when ready."""
    job = await get_resolution(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown resolution job")
    return job


class ResolutionCommitRequest(BaseModel):
    # Each decision: {"action": "merge"|"alias"|"junk", ...}. Merges are
    # irreversible — this endpoint is the human-in-the-loop commit step.
    decisions: list[dict]


@router.post("/resolution/{job_id}/commit")
async def resolution_commit(job_id: str, req: ResolutionCommitRequest) -> dict:
    """Apply reviewed entity-resolution decisions (merge / alias / junk)."""
    result = await commit_resolution(job_id, req.decisions)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/classification/scan")
async def classification_scan() -> dict:
    """Start a background scan proposing kind='client' for end-customer stubs
    (only-inbound-HAS_CLIENT, no other signal). Returns a job id to poll; nothing
    is written until the user commits an approved subset."""
    return await start_classification()


@router.get("/classification/{job_id}")
async def classification_status(job_id: str) -> dict:
    """Poll a client-classification scan; proposed candidates fill in when ready."""
    job = await get_classification(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown classification job")
    return job


class ClassificationCommitRequest(BaseModel):
    # Names the reviewer approved for kind='client'. Human-in-the-loop commit step;
    # the mutation re-checks each is still an unclassified stub before writing.
    names: list[str]


@router.post("/classification/{job_id}/commit")
async def classification_commit(job_id: str, req: ClassificationCommitRequest) -> dict:
    """Apply kind='client' to the user-approved candidate names."""
    result = await commit_classification(job_id, req.names)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


class RefreshRequest(BaseModel):
    domain: str


@router.post("/cache/refresh")
async def refresh_cache(req: RefreshRequest) -> dict:
    """Drop the cached page snapshots + client list for a domain so the next
    research re-crawls it (e.g. "example.com")."""
    return await cache.clear_domain(get_driver(), cache.domain_of(req.domain))


@router.get("/fields")
async def fields() -> list[dict]:
    """Custom field definitions (registry)."""
    return await queries.list_field_defs(get_driver())


class FieldRequest(BaseModel):
    label: str
    description: str
    applies_to_kind: str = "all"
    type: str = "list"


@router.post("/fields")
async def add_field(req: FieldRequest) -> dict:
    """Register a custom field (e.g. 'Service Lines' for service_provider)."""
    if req.applies_to_kind not in APPLIES_TO:
        raise HTTPException(status_code=422, detail=f"applies_to_kind must be one of {APPLIES_TO}")
    if req.type not in ("list", "text"):
        raise HTTPException(status_code=422, detail="type must be 'list' or 'text'")
    return await queries.add_field_def(
        get_driver(),
        field_key(req.label),
        req.label,
        req.description,
        req.applies_to_kind,
        req.type,
    )


@router.get("/topics")
async def topics() -> list[str]:
    return await queries.list_topics(get_driver())


@router.get("/company-types")
async def company_types() -> list[str]:
    return await queries.list_company_types(get_driver())


# --- Web discovery (#75): find similar companies NOT yet in the graph -----------


@router.post("/companies/{name}/discover")
async def discover(name: str) -> dict:
    """Start web discovery for a researched company: use its in-graph similar cohort
    as a template to search the web for MORE companies like it that aren't captured
    yet. Returns a durable job id to poll (nothing is written); 404 if unknown."""
    result = await start_discovery(name)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/discovery/{job_id}")
async def discovery_status(job_id: str) -> dict:
    """Poll a discovery job; the reviewed candidate list fills in when ready."""
    job = await get_discovery(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown discovery job")
    return job


class DiscoveryResearchRequest(BaseModel):
    names: list[str]


@router.post("/discovery/{job_id}/research")
async def discovery_research(job_id: str, req: DiscoveryResearchRequest) -> dict:
    """Feed selected discovery candidates into the existing research pipeline
    (propose→review→commit, ≤10 cap). Only names from this job's reviewed list are
    accepted; each returns a proposal id to poll. Nothing is written until commit."""
    result = await research_candidates(job_id, req.names)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/countries")
async def countries() -> list[str]:
    return await queries.list_countries(get_driver())


@public_router.get("/health/graph/size")
async def graph_size() -> JSONResponse:
    """Graph size metrics (#37): node/relationship totals against the Aura Free
    200K node cap, plus the signal breakdown that retention bounds. Public, like
    the other /health probes; 503 if the graph is unreachable."""
    try:
        return JSONResponse(content=await retention.graph_size(get_driver()))
    except Exception as exc:  # noqa: BLE001 — surface any driver/connection error
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": str(exc)})


@router.post("/companies/{name}/signals/capture")
async def capture_signals(name: str) -> dict:
    """Capture recent news/blog/events from a company's OWN site (#34): autodiscover
    RSS/Atom feeds (index-page LLM crawl as fallback) and store items as Signals with
    provenance. Returns a job id to poll; re-runs only add items not already captured
    (canonical-URL dedup). 404 if the company is unknown or has no website."""
    result = await start_signal_capture(name)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/signals/capture/{job_id}")
async def capture_signals_status(job_id: str) -> dict:
    """Poll a signal-capture job; `captured`/`new`/`outcome` fill in when done."""
    job = await get_signal_capture(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown signal-capture job")
    return job


@router.post("/companies/{name}/news/capture")
async def capture_news(name: str) -> dict:
    """Search third-party outlets for recent coverage of a company (#35) and store
    matches as Signals with the outlet as Source. A pure entity-match filter guards
    against name collisions before anything is written. Returns a job id to poll;
    re-runs only add items not already captured (canonical-URL dedup, incl. against
    site-sourced signals). 404 if the company is unknown."""
    result = await start_news_capture(name)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/news/capture/{job_id}")
async def capture_news_status(job_id: str) -> dict:
    """Poll a news-capture job; `captured`/`new`/`outcome` fill in when done."""
    job = await get_news_capture(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown news-capture job")
    return job


@router.get("/companies/{name}/signals")
async def company_signals(
    name: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """A company's activity timeline (#38): signals mentioning it, newest-first,
    each kind-badged and carrying its source links. Empty list if none captured yet."""
    return await signals.signals_for_company(get_driver(), name, limit=limit)


@router.get("/signals")
async def recent_signals(
    kind: str | None = None,
    topic: str | None = None,
    limit: int = Query(default=40, ge=1, le=200),
) -> list[dict]:
    """The What's-new feed (#38): recent signals across all companies, newest-first,
    optionally filtered by kind (news/blog/event) and/or topic."""
    return await signals.recent_signals_filtered(get_driver(), limit=limit, kind=kind, topic=topic)


# --- Person enrichment (#40, People Intelligence) --------------------------------
# A person is enriched via propose→review→commit, exactly like a company: a durable
# job researches profile/history/links (each fact cited), the status endpoint is the
# review surface (proposed facts + citations + diff), and commit applies it. The
# review UI is a follow-up story; here the API IS the review surface.


class PersonEnrichRequest(BaseModel):
    name: str
    company: str  # the company this person leads (scopes which person; #87)


@router.post("/people/enrich")
async def enrich_person(req: PersonEnrichRequest) -> dict:
    """Start a background person-enrichment proposal (#40). Gathers current
    title/company, prior roles, a bio line, and public links — every fact cited —
    for the person named `name` who leads `company`. Returns a job id to poll;
    nothing is written until commit. 404 if that person isn't a known leader of the
    company."""
    from app.agents.people.proposals import propose_person

    result = await propose_person(req.name, req.company)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/people/enrich/{job_id}")
async def enrich_person_status(job_id: str) -> dict:
    """Poll a person-enrichment proposal until status is 'ready' (or 'error'). When
    ready, `record` holds the provenance-filtered proposed facts + citations and
    `diff` the changes against what's already stored — the review surface."""
    from app.agents.people.proposals import get_person_proposal

    proposal = await get_person_proposal(job_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="unknown person proposal")
    return proposal


@router.post("/people/enrich/{job_id}/commit")
async def enrich_person_commit(job_id: str) -> dict:
    """Write a reviewed person proposal to the graph (the user's approval). Applies
    only the cited facts prepared by the proposal; idempotent."""
    from app.agents.people.proposals import commit_person_proposal

    result = await commit_person_proposal(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# --- Acquisition research (#43, M&A Intelligence) --------------------------------
# A company's M&A history is researched via propose→review→commit, exactly like a
# person: a durable job gathers deals (made + received) with EVERY deal fact cited
# and uncited amounts dropped, the status endpoint is the review surface (proposed
# deals + citations + diff), and commit writes the ACQUIRED edges. The SPA M&A view
# is a follow-up story (#45); here the API IS the review surface.


class AcquisitionResearchRequest(BaseModel):
    company: str  # the tracked company whose acquisition history to research


@router.post("/companies/acquisitions/research")
async def research_company_acquisitions(req: AcquisitionResearchRequest) -> dict:
    """Start a background acquisition-research proposal (#43). Gathers deals the
    company made and deals where it was acquired — every deal cited, amounts dropped
    unless separately cited. Returns a job id to poll; nothing is written until
    commit. 404 if the company isn't tracked in the graph."""
    from app.agents.deals.proposals import propose_acquisitions

    result = await propose_acquisitions(req.company)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/companies/acquisitions/{job_id}")
async def research_company_acquisitions_status(job_id: str) -> dict:
    """Poll an acquisition-research proposal until status is 'ready' (or 'error').
    When ready, `record` holds the provenance-filtered proposed deals + citations
    and `diff` the new/changed deals against what's already stored."""
    from app.agents.deals.proposals import get_acquisition_proposal

    proposal = await get_acquisition_proposal(job_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="unknown acquisition proposal")
    return proposal


@router.post("/companies/acquisitions/{job_id}/commit")
async def research_company_acquisitions_commit(job_id: str) -> dict:
    """Write a reviewed acquisition proposal to the graph (the user's approval).
    Applies only the cited deals prepared by the proposal; idempotent."""
    from app.agents.deals.proposals import commit_acquisition_proposal

    result = await commit_acquisition_proposal(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/digests")
async def digests_list(limit: int = Query(default=52, ge=1, le=200)) -> list[dict]:
    """Weekly digests (#51), newest-first — a browsable history of what changed.
    Compact rows (totals + prose summary); the per-id endpoint carries the payload."""
    return await digest.list_digests(get_driver(), limit=limit)


@router.get("/digests/{digest_id}")
async def digest_detail(digest_id: str) -> dict:
    """One weekly digest's full detail (#51): the rendered summary plus the grouped
    deltas payload (new signals by company, newly-researched, notable changes)."""
    result = await digest.get_digest(get_driver(), digest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no digest {digest_id!r}")
    return result
