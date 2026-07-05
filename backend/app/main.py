"""Nebula backend entrypoint.

FastAPI app that owns the Neo4j driver for its lifetime and exposes health
checks. Research agents and graph query routes get added under app/api as the
project grows.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
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

app.include_router(router)
