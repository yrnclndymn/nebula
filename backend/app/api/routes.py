"""HTTP routes: health checks, graph read endpoints, and the assistant chat."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.assistant.backfill import commit_backfill, get_backfill
from app.agents.assistant.proposals import commit_proposal, get_proposal
from app.agents.assistant.service import respond
from app.graph import cache, jobs, queries
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


class KindRequest(BaseModel):
    kind: str | None


@router.patch("/companies/{name}/kind")
async def set_kind(name: str, req: KindRequest) -> dict:
    """Set a company's kind (service_provider / isv / cloud_provider), or null."""
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


class BackfillCommitRequest(BaseModel):
    companies: list[str] | None = None


@router.post("/backfill/{job_id}/commit")
async def backfill_commit(job_id: str, req: BackfillCommitRequest) -> dict:
    """Write selected back-fill rows (companies=null commits all) with provenance."""
    result = await commit_backfill(job_id, req.companies)
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
