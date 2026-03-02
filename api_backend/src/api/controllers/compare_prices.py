from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from src.api.adapters.scrapers import ScrapeResult, scrape_product_title_and_price


def _is_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return bool(parsed.scheme and parsed.netloc)


def _domain_of(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower()


@dataclass(frozen=True)
class SiteScrapeOutcome:
    """Internal outcome type for a single site scrape attempt."""
    site: str
    ok: bool
    offer: dict[str, Any] | None
    error: dict[str, Any] | None
    elapsed_ms: int


async def _run_site_scraper(
    *,
    site: str,
    scraper: Callable[[str], Awaitable[ScrapeResult]],
    url: str,
) -> SiteScrapeOutcome:
    started = time.perf_counter()
    try:
        result = await scraper(url)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return SiteScrapeOutcome(
            site=site,
            ok=True,
            offer={
                "site": site,
                "source_domain": _domain_of(url),
                "source_url": url,
                "title": result.title,
                "currency": result.currency,
                "price_amount": result.price_amount,
                # Marked later after aggregating all successful offers.
                "is_cheapest": False,
                "raw": result.raw,
            },
            error=None,
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:  # noqa: BLE001 (boundary: controller aggregates per-site errors)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return SiteScrapeOutcome(
            site=site,
            ok=False,
            offer=None,
            error={
                "site": site,
                "type": exc.__class__.__name__,
                "message": str(exc),
            },
            elapsed_ms=elapsed_ms,
        )


async def scrapeSite1(product_url: str) -> ScrapeResult:
    """Site 1 scraper wrapper (currently uses shared dispatcher)."""
    return await scrape_product_title_and_price(product_url)


async def scrapeSite2(product_url: str) -> ScrapeResult:
    """Site 2 scraper wrapper (currently uses shared dispatcher)."""
    return await scrape_product_title_and_price(product_url)


async def scrapeSite3(product_url: str) -> ScrapeResult:
    """Site 3 scraper wrapper (currently uses shared dispatcher)."""
    return await scrape_product_title_and_price(product_url)


# PUBLIC_INTERFACE
async def comparePrices(product: str) -> dict[str, Any]:
    """Compare prices by running multiple scrapers in parallel.

    Accepts:
      - product: a product URL (preferred) or a product name/keywords.

    Behavior:
      - If `product` is a URL, runs scrapeSite1/2/3 concurrently against that URL.
      - If `product` is NOT a URL, returns a structured response with per-site errors
        indicating that URL-based scraping is required (search-by-name is not implemented yet).

    Returns:
      Structured JSON with:
        - input: echoed input data
        - results: per-site entries with ok/offer/error/elapsed_ms
        - offers: list of successful offers
        - errors: list of per-site errors
        - meta: summary counts and timings
    """
    product = (product or "").strip()
    started = time.perf_counter()

    if not product:
        return {
            "input": {"product": product},
            "results": [],
            "offers": [],
            "errors": [{"type": "ValidationError", "message": "product is required"}],
            "meta": {
                "ok_count": 0,
                "error_count": 1,
                "elapsed_ms": 0,
                "min_price_amount": None,
                "max_price_amount": None,
                "price_spread_amount": None,
            },
        }

    if not _is_url(product):
        # Name/keyword-based searching across sites is a future extension point.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        per_site_error = {
            "type": "NotImplementedError",
            "message": "Product-name search is not implemented; provide a product URL.",
        }
        results = [
            {"site": "site1", "ok": False, "offer": None, "error": {**per_site_error, "site": "site1"}, "elapsed_ms": 0},
            {"site": "site2", "ok": False, "offer": None, "error": {**per_site_error, "site": "site2"}, "elapsed_ms": 0},
            {"site": "site3", "ok": False, "offer": None, "error": {**per_site_error, "site": "site3"}, "elapsed_ms": 0},
        ]
        return {
            "input": {"product": product, "kind": "name"},
            "results": results,
            "offers": [],
            "errors": [r["error"] for r in results if r["error"]],
            "meta": {
                "ok_count": 0,
                "error_count": 3,
                "elapsed_ms": elapsed_ms,
                "min_price_amount": None,
                "max_price_amount": None,
                "price_spread_amount": None,
            },
        }

    # URL case: run all site scrapers concurrently; collect per-site errors without failing whole request.
    tasks = [
        _run_site_scraper(site="site1", scraper=scrapeSite1, url=product),
        _run_site_scraper(site="site2", scraper=scrapeSite2, url=product),
        _run_site_scraper(site="site3", scraper=scrapeSite3, url=product),
    ]
    outcomes = await asyncio.gather(*tasks)

    offers: list[dict[str, Any]] = [o.offer for o in outcomes if o.ok and o.offer is not None]  # type: ignore[misc]
    errors: list[dict[str, Any]] = [o.error for o in outcomes if (not o.ok and o.error is not None)]  # type: ignore[misc]

    def _is_valid_price_amount(v: Any) -> bool:
        # price_amount is in minor units; treat <=0 and non-int as invalid/unknown.
        return isinstance(v, int) and v > 0

    # Sort successful offers by price ascending (valid numeric prices first; unknown/invalid last).
    def _offer_sort_key(offer: dict[str, Any]) -> tuple[int, int, str]:
        pa = offer.get("price_amount")
        if _is_valid_price_amount(pa):
            return (0, int(pa), str(offer.get("site") or ""))
        return (1, 0, str(offer.get("site") or ""))

    offers.sort(key=_offer_sort_key)

    priced_amounts: list[int] = [
        int(o["price_amount"]) for o in offers if _is_valid_price_amount(o.get("price_amount"))
    ]

    min_price_amount: int | None = None
    max_price_amount: int | None = None
    price_spread_amount: int | None = None

    if priced_amounts:
        min_price_amount = min(priced_amounts)
        max_price_amount = max(priced_amounts)
        # If only one valid price exists, spread is 0 (max-min).
        price_spread_amount = max_price_amount - min_price_amount

        # Mark cheapest option (ties allowed: mark all offers matching the min).
        for offer in offers:
            offer["is_cheapest"] = bool(offer.get("price_amount") == min_price_amount)

    results: list[dict[str, Any]] = [
        {"site": o.site, "ok": o.ok, "offer": o.offer, "error": o.error, "elapsed_ms": o.elapsed_ms} for o in outcomes
    ]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "input": {"product": product, "kind": "url"},
        "results": results,
        "offers": offers,
        "errors": errors,
        "meta": {
            "ok_count": len(offers),
            "error_count": len(errors),
            "elapsed_ms": elapsed_ms,
            "min_price_amount": min_price_amount,
            "max_price_amount": max_price_amount,
            "price_spread_amount": price_spread_amount,
        },
    }
