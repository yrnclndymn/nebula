"""HTTP routes: health checks, graph read endpoints, and the assistant chat."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.assistant.proposals import commit_proposal, get_proposal
from app.agents.assistant.service import respond
from app.graph import cache, queries
from app.graph.driver import check_connectivity, get_driver

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the API process is up. Does not touch the database."""
    return {"status": "ok"}


@router.get("/health/graph")
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
    headcount_min: int | None = Query(default=None, ge=0),
    headcount_max: int | None = Query(default=None, ge=0),
) -> list[dict]:
    """Researched companies (with aggregates), optionally filtered."""
    return await queries.list_companies(
        get_driver(),
        topic=topic,
        q=q,
        company_type=company_type,
        headcount_min=headcount_min,
        headcount_max=headcount_max,
    )


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
    return {"reply": turn.reply, "proposals": turn.proposals}


@router.get("/proposals/{proposal_id}")
async def proposal_status(proposal_id: str) -> dict:
    """Poll a background enrichment proposal until status is 'ready' (or 'error')."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="unknown proposal")
    return proposal


class CommitRequest(BaseModel):
    proposal_id: str


@router.post("/proposals/commit")
async def commit(req: CommitRequest) -> dict:
    """Write a reviewed proposal to the graph (the user's approval of an
    agent-prepared enrichment)."""
    result = await commit_proposal(req.proposal_id)
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


@router.get("/topics")
async def topics() -> list[str]:
    return await queries.list_topics(get_driver())


@router.get("/company-types")
async def company_types() -> list[str]:
    return await queries.list_company_types(get_driver())
