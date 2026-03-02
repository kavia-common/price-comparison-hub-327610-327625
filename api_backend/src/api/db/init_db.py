from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from src.api.db.models import Base

logger = logging.getLogger(__name__)


# PUBLIC_INTERFACE
async def init_db(engine: AsyncEngine) -> None:
    """Initialize database schema (create tables if missing).

    Contract:
      - Inputs: AsyncEngine
      - Side effects: Executes CREATE TABLE statements in the configured database.
      - Errors: Propagates SQLAlchemy DB errors (connectivity/permissions).
    """
    logger.info("init_db: creating tables (if not exist)")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
