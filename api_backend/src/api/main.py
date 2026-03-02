from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.core.settings import get_settings
from src.api.db.init_db import init_db
from src.api.routers import admin, compare, history

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

openapi_tags = [
    {"name": "Compare", "description": "Compare orchestration endpoints (name/URLs) with caching and persistence."},
    {"name": "Queries", "description": "Query history and detail endpoints."},
    {"name": "Admin", "description": "Basic admin endpoints for site configuration (auth to be added)."},
]

app = FastAPI(
    title="Price Comparison Hub API",
    description=(
        "Backend for price comparison: accepts product name/URLs, runs orchestration, "
        "applies robots hooks, caches recent requests, and persists query/offer history."
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_engine_and_sessionmaker() -> async_sessionmaker[AsyncSession]:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_url, pool_pre_ping=True, future=True)
    return async_sessionmaker(engine, expire_on_commit=False)


@app.on_event("startup")
async def _on_startup() -> None:
    """Initialize resources on service startup."""
    session_maker = _build_engine_and_sessionmaker()
    app.state.session_maker = session_maker

    # Create tables if needed (safe for dev; production should use migrations later).
    engine = session_maker.kw["bind"]
    await init_db(engine)


# PUBLIC_INTERFACE
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Get the app-global AsyncSession maker.

    Contract:
      - Must be called after startup event has run.
      - Raises RuntimeError if session maker is not initialized.
    """
    session_maker = getattr(app.state, "session_maker", None)
    if session_maker is None:
        raise RuntimeError("DB session maker not initialized. Startup event may not have run.")
    return session_maker


@app.get("/", tags=["Compare"])
def health_check() -> dict:
    """Health check endpoint."""
    return {"message": "Healthy"}


@app.get(
    "/docs/help",
    tags=["Compare"],
    summary="API usage help",
    description="Quick usage notes for key endpoints.",
    operation_id="docs_help",
)
def docs_help() -> dict:
    """Return basic usage notes for the API."""
    return {
        "compare": {
            "endpoint": "POST /compare",
            "body_example": {"product_name": "iPhone 15", "urls": [], "force_refresh": False},
        },
        "history": {
            "list": "GET /queries?limit=20",
            "detail": "GET /queries/{query_id}",
        },
        "admin": {"sites_list": "GET /admin/sites", "sites_upsert": "POST /admin/sites"},
    }


app.include_router(compare.router)
app.include_router(history.router)
app.include_router(admin.router)
