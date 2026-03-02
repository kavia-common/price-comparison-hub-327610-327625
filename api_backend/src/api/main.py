from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.core.settings import get_settings_optional
from src.api.db.init_db import init_db
from src.api.routers import admin, compare, compare_prices, history

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


def _split_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


# CORS: use environment-provided allowlist in preview/prod.
# NOTE: Using "*" with allow_credentials=True is invalid per CORS spec; browsers will block it.
allowed_origins = _split_csv_env("ALLOWED_ORIGINS", "http://localhost:3000")
allowed_headers = _split_csv_env("ALLOWED_HEADERS", "Content-Type,Authorization")
allowed_methods = _split_csv_env("ALLOWED_METHODS", "GET,POST,PUT,DELETE,PATCH,OPTIONS")
cors_max_age = int(os.getenv("CORS_MAX_AGE", "3600"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    max_age=cors_max_age,
)


def _build_engine_and_sessionmaker() -> async_sessionmaker[AsyncSession] | None:
    """Build DB engine/sessionmaker if DB is configured, otherwise return None.

    Returning None allows the service to start in environments where the DB is not
    wired yet (preview/dev). DB-backed endpoints will be unavailable in that case.
    """
    settings = get_settings_optional()
    if settings is None:
        return None
    engine = create_async_engine(settings.postgres_url, pool_pre_ping=True, future=True)
    return async_sessionmaker(engine, expire_on_commit=False)


@app.on_event("startup")
async def _on_startup() -> None:
    """Initialize resources on service startup.

    If POSTGRES_URL/DATABASE_URL is not set, DB initialization is skipped so the
    service can still become ready and serve non-DB endpoints (e.g. health check).
    """
    session_maker = _build_engine_and_sessionmaker()
    app.state.session_maker = session_maker

    if session_maker is None:
        logger.warning(
            "DB is not configured (POSTGRES_URL/DATABASE_URL missing). "
            "DB-backed endpoints will return 503 until configured."
        )
        return

    # Create tables if needed (safe for dev; production should use migrations later).
    engine = session_maker.kw["bind"]
    await init_db(engine)


# PUBLIC_INTERFACE
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Get the app-global AsyncSession maker.

    Contract:
      - Must be called after startup event has run.
      - Raises RuntimeError if DB is not configured OR if startup did not initialize it.
    """
    session_maker = getattr(app.state, "session_maker", None)
    if session_maker is None:
        raise RuntimeError(
            "DB session maker not available. "
            "Database is not configured (POSTGRES_URL/DATABASE_URL missing) or startup did not run."
        )
    return session_maker


@app.get("/", tags=["Compare"])
def health_check() -> dict:
    """Health check endpoint."""
    return {"message": "Healthy"}


@app.get(
    "/healthz",
    tags=["Compare"],
    summary="Readiness health check",
    description="Readiness endpoint used by PreviewManager/Kavia health checks.",
    operation_id="health_check_healthz",
)
# PUBLIC_INTERFACE
def healthz() -> dict:
    """Readiness health check endpoint.

    This endpoint exists to satisfy the platform health-check path configured via
    HEALTHCHECK_PATH (commonly `/healthz`), ensuring the container is marked ready
    once Uvicorn is bound and the app is responding.
    """
    return {"status": "ok"}


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
app.include_router(compare_prices.router)
app.include_router(history.router)
app.include_router(admin.router)
