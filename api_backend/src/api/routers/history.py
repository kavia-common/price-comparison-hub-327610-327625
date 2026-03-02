from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.api.db.models import CompareQuery, Offer
from src.api.db.session import get_db_session
from src.api.schemas import (
    OfferOut,
    QueryDetailResponse,
    QueryHistoryItem,
    QueryHistoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queries", tags=["Queries"])


def _offer_to_out(offer: Offer) -> OfferOut:
    return OfferOut(
        id=offer.id,
        source_domain=offer.source_domain,
        source_url=offer.source_url,
        title=offer.title,
        currency=offer.currency,
        price_amount=offer.price_amount,
        availability=offer.availability,
    )


def _get_session_maker_dep() -> async_sessionmaker[AsyncSession]:
    from src.api.main import get_session_maker

    try:
        return get_session_maker()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Database is not configured.") from exc


@router.get(
    "",
    response_model=QueryHistoryResponse,
    summary="List compare query history",
    description="Returns recent compare queries, newest first. Use `before` cursor for pagination.",
    operation_id="queries_list",
)
async def list_queries(
    limit: int = Query(default=20, ge=1, le=100, description="Maximum number of items."),
    before: dt.datetime | None = Query(default=None, description="Cursor: return items created before this timestamp."),
    session_maker: async_sessionmaker[AsyncSession] = Depends(_get_session_maker_dep),
) -> QueryHistoryResponse:
    """List persisted compare queries (history)."""
    async for db in get_db_session(session_maker):
        stmt = select(CompareQuery).order_by(CompareQuery.created_at.desc()).limit(limit)
        if before is not None:
            stmt = stmt.where(CompareQuery.created_at < before)

        rows = (await db.execute(stmt)).scalars().all()
        items = [
            QueryHistoryItem(
                id=r.id,
                product_name=r.product_name,
                input_urls=r.input_urls or [],
                status=r.status,
                created_at=r.created_at,
            )
            for r in rows
        ]
        next_cursor = items[-1].created_at if len(items) == limit else None
        return QueryHistoryResponse(items=items, next_cursor=next_cursor)

    raise HTTPException(status_code=500, detail="Database session unavailable.")


@router.get(
    "/{query_id}",
    response_model=QueryDetailResponse,
    summary="Get compare query detail",
    description="Returns a specific persisted compare query, including its offers.",
    operation_id="queries_get_detail",
)
async def get_query_detail(
    query_id: uuid.UUID,
    session_maker: async_sessionmaker[AsyncSession] = Depends(_get_session_maker_dep),
) -> QueryDetailResponse:
    """Get query detail by id."""
    async for db in get_db_session(session_maker):
        stmt = (
            select(CompareQuery)
            .where(CompareQuery.id == query_id)
            .options(selectinload(CompareQuery.offers))
            .limit(1)
        )
        query = (await db.execute(stmt)).scalars().first()
        if query is None:
            raise HTTPException(status_code=404, detail="Query not found")

        return QueryDetailResponse(
            id=query.id,
            cache_key=query.cache_key,
            product_name=query.product_name,
            input_urls=query.input_urls or [],
            normalized_terms=query.normalized_terms,
            status=query.status,
            error_message=query.error_message,
            created_at=query.created_at,
            offers=[_offer_to_out(o) for o in (query.offers or [])],
        )

    raise HTTPException(status_code=500, detail="Database session unavailable.")
