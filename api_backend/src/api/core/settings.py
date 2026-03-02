import os
from dataclasses import dataclass


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
      - Required env:
          * POSTGRES_URL
      - Optional env:
          * CACHE_TTL_SECONDS (default: 900)
      - Outputs: Settings object
      - Errors: ValueError if required env vars are missing or invalid.
    """
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        raise ValueError(
            "Missing required environment variable POSTGRES_URL (provided by postgres_db container)."
        )

    cache_ttl_raw = os.getenv("CACHE_TTL_SECONDS", "900")
    try:
        cache_ttl_seconds = int(cache_ttl_raw)
    except ValueError as exc:
        raise ValueError("CACHE_TTL_SECONDS must be an integer.") from exc

    return Settings(postgres_url=postgres_url, cache_ttl_seconds=cache_ttl_seconds)
