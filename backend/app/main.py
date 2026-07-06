"""Nebula backend entrypoint.

FastAPI app that owns the Neo4j driver for its lifetime and exposes health
checks. Research agents and graph query routes get added under app/api as the
project grows.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import public_router, router, tasks_router
from app.auth import verify_task, verify_user
from app.config import settings
from app.graph.driver import close_driver, get_driver


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_driver()  # create the driver eagerly at startup
    yield
    await close_driver()


app = FastAPI(title="Nebula API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(public_router)  # /health — open, at root
app.include_router(tasks_router, dependencies=[Depends(verify_task)])  # /jobs/run — root, OIDC
app.include_router(
    router, prefix=settings.api_prefix, dependencies=[Depends(verify_user)]
)  # user routes — "/api" in prod
