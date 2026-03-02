from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.adapters.robots import check_robots_allowed
from src.api.core.settings import Settings
from src.api.db.models import CompareQuery, Offer, OfferHistory, SiteConfig
from src.api.domain.compare_key import NormalizedCompareInput, normalize_compare_input

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompareFlowRequest:
    """Request contract for CompareFlow."""

    product_name: str | None
    urls: list[str]
    force_refresh: bool


@dataclass(frozen=True)
class CompareFlowResult:
    """Result contract for CompareFlow."""

    query: CompareQuery
    cached: bool


def _domain_from_url(url: str) -> str:
    return urlparse(url).netloc.lower()


async def _load_enabled_site_configs(db: AsyncSession) -> dict[str, SiteConfig]:
    rows = (await db.execute(select(SiteConfig).where(SiteConfig.enabled.is_(True)))).scalars().all()
    return {r.domain.lower(): r for r in rows}


async def _find_cached_query(
    db: AsyncSession, cache_key: str, ttl_seconds: int
) -> CompareQuery | None:
    ttl_cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=ttl_seconds)
    stmt = (
        select(CompareQuery)
        .where(CompareQuery.cache_key == cache_key)
        .where(CompareQuery.created_at >= ttl_cutoff)
        .order_by(CompareQuery.created_at.desc())
        .limit(1)
    )
    cached = (await db.execute(stmt)).scalars().first()
    return cached


async def _persist_query_and_offers(
    db: AsyncSession,
    normalized: NormalizedCompareInput,
    offers_payload: list[dict],
) -> CompareQuery:
    query = CompareQuery(
        cache_key=normalized.cache_key,
        product_name=normalized.product_name,
        input_urls=list(normalized.urls),
        normalized_terms=normalized.normalized_terms,
        status="completed",
        error_message=None,
    )
    db.add(query)
    await db.flush()  # assign query.id

    for offer_dict in offers_payload:
        offer = Offer(
            query_id=query.id,
            source_domain=offer_dict["source_domain"],
            source_url=offer_dict["source_url"],
            title=offer_dict.get("title", ""),
            currency=offer_dict.get("currency", "USD"),
            price_amount=offer_dict.get("price_amount"),
            availability=offer_dict.get("availability", "unknown"),
            raw_payload=offer_dict.get("raw_payload", {}),
        )
        db.add(offer)
        await db.flush()  # assign offer.id
        db.add(
            OfferHistory(
                offer_id=offer.id,
                currency=offer.currency,
                price_amount=offer.price_amount,
                availability=offer.availability,
            )
        )

    await db.commit()
    await db.refresh(query)
    return query


async def _orchestrate_compare(
    db: AsyncSession,
    settings: Settings,
    normalized: NormalizedCompareInput,
) -> list[dict]:
    """Core orchestration (placeholder).

    Current behavior:
      - For each URL, optionally checks robots.txt (if site config says 'respect').
      - Returns a minimal "offer" record for each allowed URL with unknown price.

    Extension points (future):
      - per-site parsers, searching by product_name, headless fetch mode, etc.
    """
    site_configs = await _load_enabled_site_configs(db)

    offers: list[dict] = []
    for url in normalized.urls:
        domain = _domain_from_url(url)
        site_cfg = site_configs.get(domain)

        robots_policy = site_cfg.robots_policy if site_cfg else "respect"
        if robots_policy == "respect":
            decision = await check_robots_allowed(url)
            if not decision.allowed:
                logger.info(
                    "compare_flow: url blocked by robots",
                    extra={"cache_key": normalized.cache_key, "domain": domain, "url": url},
                )
                continue

        offers.append(
            {
                "source_domain": domain,
                "source_url": url,
                "title": normalized.product_name or "",
                "currency": "USD",
                "price_amount": None,
                "availability": "unknown",
                "raw_payload": {
                    "note": "Scraping/parsing not yet implemented; this is a persistence + flow skeleton.",
                    "robots_policy": robots_policy,
                },
            }
        )

    # If user only provided product_name (no urls), we still return empty offers for now.
    # Later we can search enabled sites by product keyword.
    _ = settings
    return offers


# PUBLIC_INTERFACE
async def run_compare_flow(
    *,
    db: AsyncSession,
    settings: Settings,
    request: CompareFlowRequest,
) -> CompareFlowResult:
    """Run the end-to-end compare flow with caching, robots hook, and persistence.

    Flow name: CompareFlow

    Contract:
      - Inputs:
          * db: AsyncSession (open transaction boundary at request level)
          * settings: Settings
          * request: CompareFlowRequest with product_name and/or urls
      - Outputs:
          * CompareFlowResult with persisted CompareQuery and cached flag
      - Errors:
          * ValueError: if both product_name and urls are missing
          * Propagates DB errors (connectivity/constraints)
      - Side effects:
          * Reads site_configs
          * Reads cached queries
          * Persists new compare_queries/offers/offer_history when not cached
    """
    normalized = normalize_compare_input(request.product_name, request.urls)

    if not normalized.product_name and not normalized.urls:
        raise ValueError("Provide at least one of product_name or urls.")

    logger.info(
        "compare_flow: start",
        extra={
            "cache_key": normalized.cache_key,
            "product_name": normalized.product_name,
            "url_count": len(normalized.urls),
            "force_refresh": request.force_refresh,
        },
    )

    if not request.force_refresh:
        cached = await _find_cached_query(db, normalized.cache_key, settings.cache_ttl_seconds)
        if cached is not None:
            # Ensure offers relationship is loaded when converting later
            await db.refresh(cached, attribute_names=["offers"])
            logger.info("compare_flow: cache hit", extra={"cache_key": normalized.cache_key, "query_id": str(cached.id)})
            return CompareFlowResult(query=cached, cached=True)

    offers_payload = await _orchestrate_compare(db, settings, normalized)
    query = await _persist_query_and_offers(db, normalized, offers_payload)

    await db.refresh(query, attribute_names=["offers"])
    logger.info("compare_flow: completed", extra={"cache_key": normalized.cache_key, "query_id": str(query.id)})
    return CompareFlowResult(query=query, cached=False)
