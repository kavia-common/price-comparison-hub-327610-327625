from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from src.api.adapters.http_fetch import DEFAULT_USER_AGENT, fetch_html_or_raise
from src.api.domain.pricing import cleanPrice

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeResult:
    """Normalized scrape result for a product page.

    Invariants:
      - All scrapers in this module standardize currency to INR.
      - price_amount is expressed in "minor units" for INR (paise).
      - currency is an ISO-ish code used by the rest of the backend (best-effort).
    """

    title: str
    currency: str
    price_amount: int | None  # INR minor units (paise)
    raw: dict[str, Any]


def _inr_rupees_to_paise(amount_inr: float | None) -> int | None:
    """Convert INR major units (rupees float) to minor units (paise int)."""
    if amount_inr is None:
        return None
    # Use round to avoid truncation issues from floats; amounts are approximate anyway (scraped).
    return int(round(amount_inr * 100))


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _strip_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_decimal_minor_units(amount_text: str | None, *, minor_units: int) -> int | None:
    """Parse a decimal-like amount string into integer minor units.

    Contract:
      - Inputs:
          * amount_text: string containing a number (may include commas/extra chars)
          * minor_units: multiplier (100 for cents/paise)
      - Output:
          * integer minor units or None if parse fails.
      - Notes:
          This helper is used by currency-specific parsers after stripping symbols/labels.
    """
    if not amount_text:
        return None

    cleaned = _strip_text(amount_text)
    cleaned = cleaned.replace(",", "")

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

    return int((amount * minor_units).quantize(Decimal("1")))


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
    return _parse_decimal_minor_units(cleaned, minor_units=100)


def _parse_usd_to_cents(price_text: str | None) -> int | None:
    """Parse a USD price string into cents (minor units).

    Accepts common formats such as:
      - "$199.99"
      - "USD 199.99"
      - "199.99"
    """
    if not price_text:
        return None

    cleaned = price_text
    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace("USD", "").replace("usd", "")
    return _parse_decimal_minor_units(cleaned, minor_units=100)


def _pick_first_nonempty(values: list[str | None]) -> str:
    for v in values:
        vv = _strip_text(v)
        if vv:
            return vv
    return ""


def _find_json_ld_objects(html: str) -> list[dict[str, Any]]:
    """Extract JSON-LD objects from the HTML.

    We intentionally do not add BeautifulSoup dependency; instead we regex the
    <script type="application/ld+json"> blocks, then json.loads them.
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


def _extract_from_jsonld_product(jsonld_objects: list[dict[str, Any]]) -> tuple[str, str | None, str | None]:
    """Try to extract (title, price_text, price_currency) from JSON-LD Product schema."""
    for obj in jsonld_objects:
        # Some sites embed an @graph array inside a dict
        if "@graph" in obj and isinstance(obj["@graph"], list):
            nested = [n for n in obj["@graph"] if isinstance(n, dict)]
            title, price, currency = _extract_from_jsonld_product(nested)
            if title or price or currency:
                return title, price, currency

        obj_type = obj.get("@type")
        if isinstance(obj_type, list):
            is_product = any(t.lower() == "product" for t in obj_type if isinstance(t, str))
        else:
            is_product = isinstance(obj_type, str) and obj_type.lower() == "product"

        if not is_product:
            continue

        title = obj.get("name") if isinstance(obj.get("name"), str) else ""
        offers = obj.get("offers")

        price_text: str | None = None
        price_currency: str | None = None

        def _extract_offer_fields(offer: dict[str, Any]) -> tuple[str | None, str | None]:
            price = offer.get("price") or offer.get("priceSpecification", {}).get("price")  # type: ignore[union-attr]
            curr = offer.get("priceCurrency") or offer.get("priceSpecification", {}).get("priceCurrency")  # type: ignore[union-attr]

            if isinstance(price, (int, float, Decimal)):
                price = str(price)
            if not isinstance(price, str):
                price = None
            if not isinstance(curr, str):
                curr = None
            return price, curr

        # offers can be dict or list
        if isinstance(offers, dict):
            price_text, price_currency = _extract_offer_fields(offers)
        elif isinstance(offers, list):
            for o in offers:
                if not isinstance(o, dict):
                    continue
                p, c = _extract_offer_fields(o)
                if p or c:
                    price_text, price_currency = p, c
                    if price_text:
                        break

        if title or price_text or price_currency:
            return (
                _strip_text(title),
                _strip_text(price_text) if price_text else None,
                _strip_text(price_currency) if price_currency else None,
            )

    return "", None, None


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


def _extract_price_inr_from_html_fallback(html: str) -> str | None:
    """Fallback INR price extraction from raw HTML text."""
    m = re.search(r"(₹\s?[0-9][0-9,]*(?:\.[0-9]{1,2})?)", html)
    if m:
        return _strip_text(m.group(1))

    m = re.search(r"(?:Rs\.?|INR)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", html, flags=re.IGNORECASE)
    if m:
        return _strip_text(m.group(0))

    return None


def _extract_price_usd_from_html_fallback(html: str) -> str | None:
    """Fallback USD price extraction from raw HTML text."""
    m = re.search(r"(\$\s?[0-9][0-9,]*(?:\.[0-9]{1,2})?)", html)
    if m:
        return _strip_text(m.group(1))

    m = re.search(r"(?:USD)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", html, flags=re.IGNORECASE)
    if m:
        return _strip_text(m.group(0))

    return None


async def _fetch_html(url: str, user_agent: str) -> str:
    """Fetch HTML for scrapers with polite delays and basic anti-bot handling.

    This function is intentionally thin: the canonical flow is implemented in
    `src.api.adapters.http_fetch.fetch_html_or_raise` so all scrapers share the same
    request behavior (headers, delay, and block detection).
    """
    return await fetch_html_or_raise(url, user_agent=user_agent, timeout_seconds=15.0)


async def _scrape_generic_product_page(
    url: str,
    *,
    user_agent: str,
    default_currency: str = "INR",
) -> ScrapeResult:
    """Generic product page scraper using JSON-LD, with HTML fallbacks."""
    html = await _fetch_html(url, user_agent=user_agent)
    jsonld = _find_json_ld_objects(html)
    title, price_text, price_currency = _extract_from_jsonld_product(jsonld)

    if not title:
        title = _extract_title_from_html_fallback(html)

    currency_hint = (price_currency or default_currency or "INR").upper()

    if not price_text:
        # Keep using currency-aware fallback extraction to maximize hit rate.
        if currency_hint == "USD":
            price_text = _extract_price_usd_from_html_fallback(html)
        else:
            price_text = _extract_price_inr_from_html_fallback(html)

    amount_inr = cleanPrice(price_text, currency_hint=currency_hint, target_currency="INR")
    price_amount = _inr_rupees_to_paise(amount_inr)

    return ScrapeResult(
        title=title,
        currency="INR",
        price_amount=price_amount,
        raw={
            "strategy": "generic_jsonld_fallback",
            "price_text": price_text,
            "currency_hint": currency_hint,
            "amount_inr": amount_inr,
            "jsonld_count": len(jsonld),
            "jsonld_price_currency": price_currency,
        },
    )


async def _scrape_gameloot(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gameloot.in product pages."""
    result = await _scrape_generic_product_page(url, user_agent=user_agent, default_currency="INR")
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gameloot.in"},
    )


async def _scrape_gamestheshop(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gamestheshop.com product pages."""
    result = await _scrape_generic_product_page(url, user_agent=user_agent, default_currency="INR")
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gamestheshop.com"},
    )


async def _scrape_gamenation(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gamenation.in product pages."""
    result = await _scrape_generic_product_page(url, user_agent=user_agent, default_currency="INR")
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gamenation.in"},
    )


async def _scrape_amazon(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for amazon.com product pages (best-effort)."""
    html = await _fetch_html(url, user_agent=user_agent)

    jsonld = _find_json_ld_objects(html)
    title_ld, price_ld, curr_ld = _extract_from_jsonld_product(jsonld)

    title = title_ld
    if not title:
        m = re.search(
            r'<span[^>]+id=["\']productTitle["\'][^>]*>(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            title = _strip_text(re.sub(r"<[^>]+>", "", m.group(1)))
    if not title:
        title = _extract_title_from_html_fallback(html)

    price_text = price_ld
    if not price_text:
        price_candidates: list[str | None] = []
        for pid in ("priceblock_ourprice", "priceblock_dealprice", "priceblock_saleprice"):
            m = re.search(
                rf'<span[^>]+id=["\']{pid}["\'][^>]*>(.*?)</span>',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m:
                price_candidates.append(_strip_text(re.sub(r"<[^>]+>", "", m.group(1))))

        # Newer markup: whole+fraction
        m_whole = re.search(
            r'<span[^>]+class=["\']a-price-whole["\'][^>]*>(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m_whole:
            whole = _strip_text(re.sub(r"<[^>]+>", "", m_whole.group(1)))
            m_frac = re.search(
                r'<span[^>]+class=["\']a-price-fraction["\'][^>]*>(.*?)</span>',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            frac = _strip_text(re.sub(r"<[^>]+>", "", m_frac.group(1))) if m_frac else "00"
            if whole:
                price_candidates.append(f"${whole}.{frac}")

        price_text = _pick_first_nonempty(price_candidates) or None

    if not price_text:
        price_text = _extract_price_usd_from_html_fallback(html)

    currency_hint = (curr_ld or "USD").upper() or "USD"
    currency_hint = "USD"  # amazon.com expected currency

    amount_inr = cleanPrice(price_text, currency_hint=currency_hint, target_currency="INR")
    price_amount = _inr_rupees_to_paise(amount_inr)

    return ScrapeResult(
        title=title,
        currency="INR",
        price_amount=price_amount,
        raw={
            "site": "amazon.com",
            "strategy": "amazon_best_effort",
            "price_text": price_text,
            "currency_hint": currency_hint,
            "amount_inr": amount_inr,
            "jsonld_count": len(jsonld),
            "jsonld_price_currency": curr_ld,
        },
    )


async def _scrape_flipkart(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for flipkart.com product pages (best-effort)."""
    html = await _fetch_html(url, user_agent=user_agent)

    jsonld = _find_json_ld_objects(html)
    title_ld, price_ld, curr_ld = _extract_from_jsonld_product(jsonld)

    title = title_ld
    if not title:
        m = re.search(
            r'<span[^>]+class=["\']VU-ZEz["\'][^>]*>(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            title = _strip_text(re.sub(r"<[^>]+>", "", m.group(1)))
    if not title:
        title = _extract_title_from_html_fallback(html)

    price_text = price_ld
    if not price_text:
        m = re.search(
            r'<div[^>]+class=["\']Nx9bqj[^"\']*["\'][^>]*>(.*?)</div>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            price_text = _strip_text(re.sub(r"<[^>]+>", "", m.group(1)))

    if not price_text:
        price_text = _extract_price_inr_from_html_fallback(html)

    currency_hint = (curr_ld or "INR").upper() or "INR"
    currency_hint = "INR"  # flipkart expected currency

    amount_inr = cleanPrice(price_text, currency_hint=currency_hint, target_currency="INR")
    price_amount = _inr_rupees_to_paise(amount_inr)

    return ScrapeResult(
        title=title,
        currency="INR",
        price_amount=price_amount,
        raw={
            "site": "flipkart.com",
            "strategy": "flipkart_best_effort",
            "price_text": price_text,
            "currency_hint": currency_hint,
            "amount_inr": amount_inr,
            "jsonld_count": len(jsonld),
            "jsonld_price_currency": curr_ld,
        },
    )


async def _scrape_gamecube(url: str, *, user_agent: str) -> ScrapeResult:
    """Scraper for gamecube.* product pages.

    Information not available from current sources: the exact GameCube domain/markup is unknown.
    We implement a safe default: generic JSON-LD + INR fallbacks.
    """
    result = await _scrape_generic_product_page(url, user_agent=user_agent, default_currency="INR")
    return ScrapeResult(
        title=result.title,
        currency=result.currency,
        price_amount=result.price_amount,
        raw={**result.raw, "site": "gamecube"},
    )


ScraperFn = Callable[[str], Awaitable[ScrapeResult]]

_SCRAPER_REGISTRY: dict[str, Callable[[str, Any], Awaitable[ScrapeResult]]] = {
    "gameloot.in": _scrape_gameloot,
    "gamestheshop.com": _scrape_gamestheshop,
    "gamenation.in": _scrape_gamenation,
    "amazon.com": _scrape_amazon,
    "flipkart.com": _scrape_flipkart,
}


def _resolve_scraper(domain: str) -> Callable[[str, Any], Awaitable[ScrapeResult]] | None:
    """Resolve a scraper function for a normalized domain (single dispatch point)."""
    if domain in _SCRAPER_REGISTRY:
        return _SCRAPER_REGISTRY[domain]

    # GameCube: domain not provided; support heuristic match.
    if "gamecube" in domain:
        return _scrape_gamecube

    return None


# PUBLIC_INTERFACE
async def scrape_product_title_and_price(url: str, *, user_agent: str = DEFAULT_USER_AGENT) -> ScrapeResult:
    """Scrape a product page URL and return normalized title and price.

    Flow name: ProductPageScrapeDispatcher

    Supported domains (best-effort):
      - gameloot.in
      - gamestheshop.com
      - gamenation.in
      - amazon.com
      - flipkart.com
      - gamecube.* (heuristic match: domain contains "gamecube")

    Contract:
      - Inputs:
          * url: product page URL (string)
          * user_agent: HTTP user-agent string (defaults to a realistic desktop browser UA)
      - Output:
          * ScrapeResult with:
              - title: best-effort product title (may be empty string if not found)
              - currency: best-effort currency code (INR/USD today)
              - price_amount: integer minor units, or None if not detected
              - raw: debug metadata including strategy hints
      - Errors:
          * ValueError: if URL invalid or domain unsupported
          * RuntimeError: fetch failures may surface as structured fetch errors (e.g., blocked/ratelimited)
      - Side effects:
          * Performs an HTTP GET for the provided URL (with per-host 2–3s polite delay).
    """
    domain = _extract_domain(url)
    if not domain:
        raise ValueError("Invalid URL: missing domain.")

    # Normalize common www prefix
    domain = domain[4:] if domain.startswith("www.") else domain

    scraper = _resolve_scraper(domain)
    if scraper is None:
        raise ValueError(f"Unsupported domain for scraping: {domain}")

    return await scraper(url, user_agent=user_agent)
