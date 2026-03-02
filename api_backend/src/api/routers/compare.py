from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.adapters.robots import check_robots_allowed
from src.api.core.settings import Settings
from src.api.db.models import CompareQuery, Offer
from src.api.db.session import get_db_session
from src.api.flows.compare_flow import CompareFlowRequest, run_compare_flow
from src.api.schemas import CompareRequest, CompareResponse, DebugRobotsCheckResponse, OfferOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/compare", tags=["Compare"])


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


def _query_to_response(query: CompareQuery, cached: bool) -> CompareResponse:
    return CompareResponse(
        query_id=query.id,
        cached=cached,
        created_at=query.created_at,
        offers=[_offer_to_out(o) for o in (query.offers or [])],
    )


def _get_settings_dep() -> Settings:
    # Imported lazily to avoid cyclic imports in app initialization
    from src.api.core.settings import get_settings

    return get_settings()


def _get_session_maker_dep() -> async_sessionmaker[AsyncSession]:
    from src.api.main import get_session_maker

    return get_session_maker()


@router.post(
    "",
    response_model=CompareResponse,
    summary="Compare product offers by name and/or URLs",
    description="Accepts a product name and/or a list of URLs, runs orchestration, caches recent results, and persists query/offers/history.",
    operation_id="compare_run",
)
async def run_compare(
    payload: CompareRequest,
    settings: Settings = Depends(_get_settings_dep),
    session_maker: async_sessionmaker[AsyncSession] = Depends(_get_session_maker_dep),
) -> CompareResponse:
    """Run compare flow.

    Returns:
      - CompareResponse with query_id, cached flag, and offers list.
    """
    async for db in get_db_session(session_maker):
        try:
            result = await run_compare_flow(
                db=db,
                settings=settings,
                request=CompareFlowRequest(
                    product_name=payload.product_name,
                    urls=[str(u) for u in payload.urls],
                    force_refresh=payload.force_refresh,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 boundary
            logger.exception("compare failed")
            raise HTTPException(status_code=500, detail="Compare orchestration failed.") from exc

        return _query_to_response(result.query, cached=result.cached)

    raise HTTPException(status_code=500, detail="Database session unavailable.")


@router.get(
    "/robots-check",
    response_model=DebugRobotsCheckResponse,
    summary="Debug robots.txt decision for a URL",
    description="Helper endpoint to inspect current robots decision logic (hook).",
    operation_id="compare_robots_check",
)
async def robots_check(url: str) -> DebugRobotsCheckResponse:
    """Check robots.txt decision (debug).

    Note: this is a helper endpoint and does not persist data.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid url")

    decision = await check_robots_allowed(url)
    return DebugRobotsCheckResponse(allowed=decision.allowed, reason=decision.reason, details=decision.details)
