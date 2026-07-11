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
from app.graph import cache, jobs, queries, schedules
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
    proposals = []
    for name in names:
        started = await propose_enrichment(name, website="")
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


class ChatRequest(BaseModel):
    session_id: str
    message: str


@router.post("/chat")
async def chat(req: ChatRequest) -> dict:
    """One conversational turn with the research assistant. Pass a stable
    session_id per client to keep multi-turn context. May return `proposals` —
    enrichment the assistant prepared for the user to review and commit."""
    turn = await respond(req.session_id, req.message)
    return {"reply": turn.reply, "proposals": turn.proposals, "backfills": turn.backfills}


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


@router.get("/countries")
async def countries() -> list[str]:
    return await queries.list_countries(get_driver())
