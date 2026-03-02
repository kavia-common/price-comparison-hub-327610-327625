from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class CompareRequest(BaseModel):
    product_name: str | None = Field(
        default=None,
        description="Optional product name/keywords to compare. Provide either product_name and/or urls.",
        min_length=1,
        max_length=512,
    )
    urls: list[HttpUrl] = Field(
        default_factory=list,
        description="Optional list of product URLs from different sites to compare.",
        max_length=20,
    )
    force_refresh: bool = Field(
        default=False,
        description="If true, bypass cache and run a fresh orchestration.",
    )


class OfferOut(BaseModel):
    id: uuid.UUID = Field(description="Offer identifier.")
    source_domain: str = Field(description="Domain of the source site (e.g., amazon.com).")
    source_url: str = Field(description="Canonical source URL.")
    title: str = Field(description="Detected product title/name.")
    currency: str = Field(description="Currency code, e.g. USD.")
    price_amount: int | None = Field(description="Price in minor units (e.g., cents). Null if unknown.")
    availability: str = Field(description="Availability string (in_stock/out_of_stock/unknown).")


class CompareResponse(BaseModel):
    query_id: uuid.UUID = Field(description="Persisted compare query id.")
    cached: bool = Field(description="True if response came from cache.")
    created_at: dt.datetime = Field(description="Query timestamp.")
    offers: list[OfferOut] = Field(description="List of offers found.")


class QueryHistoryItem(BaseModel):
    id: uuid.UUID = Field(description="Query identifier.")
    product_name: str | None = Field(description="Original product name.")
    input_urls: list[str] = Field(description="Original input URLs.")
    status: str = Field(description="Query status.")
    created_at: dt.datetime = Field(description="Creation time.")


class QueryHistoryResponse(BaseModel):
    items: list[QueryHistoryItem] = Field(description="Paginated query history items.")
    next_cursor: dt.datetime | None = Field(
        default=None,
        description="Cursor to pass as `before` for the next page (created_at of last item).",
    )


class QueryDetailResponse(BaseModel):
    id: uuid.UUID = Field(description="Query identifier.")
    cache_key: str = Field(description="Deterministic cache key for the query inputs.")
    product_name: str | None = Field(description="Original product name.")
    input_urls: list[str] = Field(description="Original input URLs.")
    normalized_terms: str = Field(description="Normalized terms used for orchestration/caching.")
    status: str = Field(description="Query status.")
    error_message: str | None = Field(description="Error message if failed.")
    created_at: dt.datetime = Field(description="Creation time.")
    offers: list[OfferOut] = Field(description="Offers captured for this query.")


class SiteConfigIn(BaseModel):
    domain: str = Field(description="Site domain (unique).", min_length=3, max_length=255)
    enabled: bool = Field(default=True, description="Enable or disable this site for orchestration.")
    parser_key: str = Field(default="generic", description="Parser strategy key.", max_length=128)
    robots_policy: str = Field(default="respect", description="robots policy: respect|ignore")
    fetch_mode: str = Field(default="http", description="fetch mode: http|headless")


class SiteConfigOut(SiteConfigIn):
    id: uuid.UUID = Field(description="Site config identifier.")
    created_at: dt.datetime = Field(description="Creation time.")
    updated_at: dt.datetime = Field(description="Last update time.")


class AdminSiteConfigListResponse(BaseModel):
    items: list[SiteConfigOut] = Field(description="List of site configurations.")


class DebugRobotsCheckResponse(BaseModel):
    allowed: bool = Field(description="Whether fetching this URL is allowed under current policy.")
    reason: str = Field(description="Human-readable reasoning for the decision.")
    details: dict[str, Any] = Field(description="Additional decision metadata.")


class ComparePricesRequest(BaseModel):
    product: str = Field(
        description="Product URL (preferred) or product name/keywords.",
        min_length=1,
        max_length=2083,
    )


class ComparePricesSiteError(BaseModel):
    site: str = Field(description="Site identifier (site1/site2/site3).")
    type: str = Field(description="Error class/category.")
    message: str = Field(description="Human-readable error message.")


class ComparePricesOffer(BaseModel):
    site: str = Field(description="Site identifier (site1/site2/site3).")
    source_domain: str = Field(description="Domain extracted from the scraped URL.")
    source_url: str = Field(description="Input URL used for scraping.")
    title: str = Field(description="Detected product title/name.")
    currency: str = Field(description="Currency code, e.g. INR or USD.")
    price_amount: int | None = Field(description="Price in minor units (e.g., cents/paise), or null if unknown.")
    raw: dict[str, Any] = Field(description="Raw scraper debug payload.")


class ComparePricesSiteResult(BaseModel):
    site: str = Field(description="Site identifier (site1/site2/site3).")
    ok: bool = Field(description="True if scraping succeeded for this site.")
    offer: ComparePricesOffer | None = Field(default=None, description="Offer data when ok=true.")
    error: ComparePricesSiteError | None = Field(default=None, description="Error data when ok=false.")
    elapsed_ms: int = Field(description="Time spent for this site's scrape attempt in milliseconds.")


class ComparePricesMeta(BaseModel):
    ok_count: int = Field(description="Number of successful site scrapes.")
    error_count: int = Field(description="Number of failed site scrapes.")
    elapsed_ms: int = Field(description="Total controller time in milliseconds.")


class ComparePricesResponse(BaseModel):
    input: dict[str, Any] = Field(description="Echoed and classified input payload.")
    results: list[ComparePricesSiteResult] = Field(description="Per-site outcomes.")
    offers: list[ComparePricesOffer] = Field(description="Flattened list of successful offers.")
    errors: list[dict[str, Any]] = Field(description="Flattened list of per-site error payloads.")
    meta: ComparePricesMeta = Field(description="Summary counts and timings.")
