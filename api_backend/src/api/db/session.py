from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.api.core.settings import Settings


def _make_async_engine(settings: Settings) -> AsyncEngine:
    """Create an async SQLAlchemy engine from settings.

    We expect POSTGRES_URL to be a SQLAlchemy/asyncpg-compatible URL, e.g.
    postgresql+asyncpg://user:pass@host:port/db
    """
    return create_async_engine(
        settings.postgres_url,
        pool_pre_ping=True,
        future=True,
    )


# PUBLIC_INTERFACE
def build_session_maker(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Build an AsyncSession factory.

    Contract:
      - Inputs: Settings with postgres_url
      - Outputs: async_sessionmaker[AsyncSession]
      - Errors: propagates SQLAlchemy engine errors at runtime if URL invalid/unreachable.
    """
    engine = _make_async_engine(settings)
    return async_sessionmaker(engine, expire_on_commit=False)


# PUBLIC_INTERFACE
async def get_db_session(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an AsyncSession.

    Contract:
      - Inputs: an async session maker (injected from app state).
      - Outputs: yields an AsyncSession; closes it after request.
    """
    async with session_maker() as session:
        yield session
