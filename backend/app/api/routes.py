"""HTTP routes: health checks + graph read endpoints for the table UI."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.graph import queries
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


@router.get("/topics")
async def topics() -> list[str]:
    return await queries.list_topics(get_driver())


@router.get("/company-types")
async def company_types() -> list[str]:
    return await queries.list_company_types(get_driver())
