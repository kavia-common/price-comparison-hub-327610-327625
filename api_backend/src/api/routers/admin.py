from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.db.models import SiteConfig
from src.api.db.session import get_db_session
from src.api.schemas import AdminSiteConfigListResponse, SiteConfigIn, SiteConfigOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])


def _get_session_maker_dep() -> async_sessionmaker[AsyncSession]:
    from src.api.main import get_session_maker

    return get_session_maker()


def _to_out(row: SiteConfig) -> SiteConfigOut:
    return SiteConfigOut(
        id=row.id,
        domain=row.domain,
        enabled=row.enabled,
        parser_key=row.parser_key,
        robots_policy=row.robots_policy,
        fetch_mode=row.fetch_mode,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/sites",
    response_model=AdminSiteConfigListResponse,
    summary="List site configurations",
    description="Lists admin-configured target sites used by the orchestration flow.",
    operation_id="admin_sites_list",
)
async def list_sites(
    session_maker: async_sessionmaker[AsyncSession] = Depends(_get_session_maker_dep),
) -> AdminSiteConfigListResponse:
    """List site configurations."""
    async for db in get_db_session(session_maker):
        rows = (await db.execute(select(SiteConfig).order_by(SiteConfig.domain.asc()))).scalars().all()
        return AdminSiteConfigListResponse(items=[_to_out(r) for r in rows])

    raise HTTPException(status_code=500, detail="Database session unavailable.")


@router.post(
    "/sites",
    response_model=SiteConfigOut,
    summary="Upsert a site configuration",
    description="Creates a new site config if domain does not exist; otherwise updates it.",
    operation_id="admin_sites_upsert",
)
async def upsert_site(
    payload: SiteConfigIn,
    session_maker: async_sessionmaker[AsyncSession] = Depends(_get_session_maker_dep),
) -> SiteConfigOut:
    """Upsert site configuration by domain."""
    async for db in get_db_session(session_maker):
        existing = (await db.execute(select(SiteConfig).where(SiteConfig.domain == payload.domain))).scalars().first()
        if existing is None:
            row = SiteConfig(
                domain=payload.domain.lower(),
                enabled=payload.enabled,
                parser_key=payload.parser_key,
                robots_policy=payload.robots_policy,
                fetch_mode=payload.fetch_mode,
            )
            db.add(row)
        else:
            row = existing
            row.enabled = payload.enabled
            row.parser_key = payload.parser_key
            row.robots_policy = payload.robots_policy
            row.fetch_mode = payload.fetch_mode

        await db.commit()
        await db.refresh(row)
        return _to_out(row)

    raise HTTPException(status_code=500, detail="Database session unavailable.")


@router.delete(
    "/sites/{site_id}",
    summary="Delete a site configuration",
    description="Deletes a site config by id.",
    operation_id="admin_sites_delete",
)
async def delete_site(
    site_id: uuid.UUID,
    session_maker: async_sessionmaker[AsyncSession] = Depends(_get_session_maker_dep),
) -> dict:
    """Delete site configuration."""
    async for db in get_db_session(session_maker):
        row = (await db.execute(select(SiteConfig).where(SiteConfig.id == site_id))).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Site config not found")
        await db.delete(row)
        await db.commit()
        return {"deleted": True, "id": str(site_id)}

    raise HTTPException(status_code=500, detail="Database session unavailable.")
