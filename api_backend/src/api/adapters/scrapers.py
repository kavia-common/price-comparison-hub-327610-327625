from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeResult:
    """Normalized scrape result for a product page."""

    title: str
    currency: str
    price_amount: int | None  # minor units (paise for INR)
    raw: dict[str, Any]


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _strip_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_inr_to_paise(price_text: str | None) -> int | None:
    """Parse an INR price string into paise (minor units).

    Accepts common formats such as:
      - "₹ 2,499"
      - "Rs. 2,499.00"
      - "INR 2499"
    """
    if not price_text:
        return None

    cleaned = price_text
    cleaned = cleaned.replace("\u20b9", "")  # ₹
    cleaned = cleaned.replace("INR", "").replace("inr", "")
    cleaned = cleaned.replace("Rs.", "").replace("Rs", "").replace("rs.", "").replace("rs", "")
    cleaned = cleaned.strip()

    # Keep digits and at most one decimal dot.
    cleaned = re.sub(r"[^0-9.]", "", cleaned)
    if not cleaned:
        return None

    # If there are multiple dots due to bad cleaning, keep first portion + first dot segment.
    if cleaned.count(".") > 1:
        first, *rest = cleaned.split(".")
        cleaned = first + "." + rest[0]

    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None

    # Convert rupees to paise
    paise = int((amount * 100).quantize(Decimal("1")))
    return paise


def _pick_first_nonempty(values: list[str | None]) -> str:
    for v in values:
        vv = _strip_text(v)
        if vv:
            return vv
    return ""


def _find_json_ld_objects(html: str) -> list[dict[str, Any]]:
    """Extract JSON-LD objects from the HTML.

    We intentionally do not add BeautifulSoup dependency; instead we regex the <script type="application/ld+json">
    blocks, then json.loads them. This is resilient enough for many e-commerce sites.
    """
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    out: list[dict[str, Any]] = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        try:
            parsed = json.loads(b)
        except json.JSONDecodeError:
            # Some sites have trailing commas or multiple JSON objects; ignore for now.
            continue

        if isinstance(parsed, dict):
            out.append(parsed)
        elif isinstance(parsed, list):
            out.extend([p for p in parsed if isinstance(p, dict)])

    return out


def _extract_from_jsonld_product(jsonld_objects: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Try to extract (title, price_text) from JSON-LD Product schema."""
    for obj in jsonld_objects:
        # Some sites embed an @graph array inside a dict
        if "@graph" in obj and isinstance(obj["@graph"], list):
            nested = [n for n in obj["@graph"] if isinstance(n, dict)]
            title, price = _extract_from_jsonld_product(nested)
            if title or price:
                return title, price

        obj_type = obj.get("@type")
        if isinstance(obj_type, list):
            is_product = any(t.lower() == "product" for t in obj_type if isinstance(t, str))
        else:
            is_product = isinstance(obj_type, str) and obj_type.lower() == "product"

        if not is_product:
            continue

        title = obj.get("name") if isinstance(obj.get("name"), str) else ""
        offers = obj.get("offers")

        # offers can be dict or list
        price_text: str | None = None
        if isinstance(offers, dict):
            price_text = offers.get("price") or offers.get("priceSpecification", {}).get("price")  # type: ignore[union-attr]
        elif isinstance(offers, list):
            for o in offers:
                if isinstance(o, dict) and (o.get("price") or o.get("priceSpecification", {}).get("price")):  # type: ignore[union-attr]
                    price_text = o.get("price") or o.get("priceSpecification", {}).get("price")  # type: ignore[union-attr]
                    break

        # price might be numeric
        if isinstance(price_text, (int, float, Decimal)):
            price_text = str(price_text)

        if title or price_text:
            return _strip_text(title), _strip_text(price_text) if price_text else None

    return "", None


def _extract_title_from_html_fallback(html: str) -> str:
    # Try <meta property="og:title" content="...">
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    if m:
        return _strip_text(m.group(1))

    # Try <title> ... </title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return _strip_text(re.sub(r"<[^>]+>", "", m.group(1)))

    return ""


def _extract_price_from_html_fallback(html: str) -> str | None:
    # Common patterns like ₹ 2,999 in the visible HTML.
    # We look for the first plausible INR price occurrence.
    m = re.search(r"(₹\s?[0-9][0-9,]*(?:\.[0-9]{1,2})?)", html)
    if m:
        return _strip_text(m.group(1))

    m = re.search(r"(?:Rs\.?|INR)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", html, flags=re.IGNORECASE)
    if m:
        return _strip_text(m.group(0))

    return None


async def _fetch_html(url: str, user_agent: str) -> str:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        resp.raise_for_status()
        return resp.text


async def _scrape_generic_product_page(url: str, *, user_agent: str) -> ScrapeResult:
    """Generic product page scraper using JSON-LD, with HTML fallbacks."""
    html = await _fetch_html(url, user_agent=user_agent)
    jsonld = _find_json_ld_objects(html)
    title, price_text = _extract_from_jsonld_product(jsonld)

    if not title:
        title = _extract_title_from_html_fallback(html)
    if not price_text:
        price_text = _extract_price_from_html_fallback(html)

    price_amount = _parse_inr_to_paise(price_text)

    return ScrapeResult(
        title=title,
        currency="INR",
        price_amount=price_amount,
        raw={
            "strategy": "generic_jsonld_fallback",
            "price_text": price_text,
            "jsonld_count": len(jsonld),
        },
    )


async def _scrape_gameloot(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gameloot.in product pages.

    Implementation: JSON-LD Product is commonly present; fall back to generic extraction.
    """
    result = await _scrape_generic_product_page(url, user_agent=user_agent)
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gameloot.in"},
    )


async def _scrape_gamestheshop(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gamestheshop.com product pages.

    Implementation: JSON-LD Product is commonly present; fall back to generic extraction.
    """
    result = await _scrape_generic_product_page(url, user_agent=user_agent)
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gamestheshop.com"},
    )


async def _scrape_gamenation(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gamenation.in product pages.

    Implementation: JSON-LD Product is commonly present; fall back to generic extraction.
    """
    result = await _scrape_generic_product_page(url, user_agent=user_agent)
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gamenation.in"},
    )


# PUBLIC_INTERFACE
async def scrape_product_title_and_price(url: str, *, user_agent: str = "PriceComparisonHubBot") -> ScrapeResult:
    """Scrape a product page URL and return normalized title and price.

    Supported domains:
      - gameloot.in
      - gamestheshop.com
      - gamenation.in

    Contract:
      - Inputs: product page URL (string)
      - Output: ScrapeResult with:
          * title: best-effort product title
          * currency: ISO-ish currency code ("INR")
          * price_amount: integer minor units (paise), or None if not detected
          * raw: debug metadata
      - Errors:
          * ValueError: if URL invalid or domain unsupported
          * httpx.HTTPError: if the page cannot be fetched
    """
    domain = _extract_domain(url)
    if not domain:
        raise ValueError("Invalid URL: missing domain.")

    # Normalize common www prefix
    domain = domain[4:] if domain.startswith("www.") else domain

    if domain == "gameloot.in":
        return await _scrape_gameloot(url, user_agent=user_agent)
    if domain == "gamestheshop.com":
        return await _scrape_gamestheshop(url, user_agent=user_agent)
    if domain == "gamenation.in":
        return await _scrape_gamenation(url, user_agent=user_agent)

    raise ValueError(f"Unsupported domain for scraping: {domain}")
