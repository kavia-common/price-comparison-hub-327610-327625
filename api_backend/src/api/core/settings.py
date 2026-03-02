import os
from dataclasses import dataclass


def _normalize_async_postgres_url(url: str) -> str:
    """Normalize postgres connection URL for SQLAlchemy async engine.

    Accepts:
      - postgresql+asyncpg://... (preferred)
      - postgresql://... (will be converted to postgresql+asyncpg://...)

    This is required because SQLAlchemy async engine needs an async driver.
    """
    cleaned = url.strip()
    if cleaned.startswith("postgresql+asyncpg://"):
        return cleaned
    if cleaned.startswith("postgresql://"):
        return cleaned.replace("postgresql://", "postgresql+asyncpg://", 1)
    return cleaned


@dataclass(frozen=True)
class Settings:
    """Application configuration loaded from environment variables."""

    postgres_url: str
    cache_ttl_seconds: int


# PUBLIC_INTERFACE
def get_settings() -> Settings:
    """Load application settings from environment variables.

    Contract:
      - Inputs: environment variables.
      - Required env (one of):
          * POSTGRES_URL  (preferred; async SQLAlchemy URL)
          * DATABASE_URL  (compatibility alias)
      - Optional env:
          * CACHE_TTL_SECONDS (default: 900)
      - Outputs: Settings object
      - Errors: ValueError if required env vars are missing or invalid.
    """
    postgres_url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not postgres_url:
        raise ValueError(
            "Missing required environment variable POSTGRES_URL (or DATABASE_URL). "
            "It should be a PostgreSQL SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host:port/db"
        )

    cache_ttl_raw = os.getenv("CACHE_TTL_SECONDS", "900")
    try:
        cache_ttl_seconds = int(cache_ttl_raw)
    except ValueError as exc:
        raise ValueError("CACHE_TTL_SECONDS must be an integer.") from exc

    return Settings(
        postgres_url=_normalize_async_postgres_url(postgres_url),
        cache_ttl_seconds=cache_ttl_seconds,
    )


# PUBLIC_INTERFACE
def get_settings_optional() -> Settings | None:
    """Load application settings if configured, otherwise return None.

    This is intended for environments (like preview/dev) where the service should still
    start and provide non-DB functionality (e.g., health endpoint, lightweight controllers)
    even when the database is not configured.

    Contract:
      - Inputs: environment variables
      - Output: Settings if POSTGRES_URL/DATABASE_URL is present; otherwise None.
      - Errors: raises ValueError only for invalid optional envs (e.g., bad CACHE_TTL_SECONDS).
    """
    postgres_url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not postgres_url:
        return None

    cache_ttl_raw = os.getenv("CACHE_TTL_SECONDS", "900")
    try:
        cache_ttl_seconds = int(cache_ttl_raw)
    except ValueError as exc:
        raise ValueError("CACHE_TTL_SECONDS must be an integer.") from exc

    return Settings(
        postgres_url=_normalize_async_postgres_url(postgres_url),
        cache_ttl_seconds=cache_ttl_seconds,
    )
