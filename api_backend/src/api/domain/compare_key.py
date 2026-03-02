from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class NormalizedCompareInput:
    """Normalized user input for compare orchestration/caching."""

    product_name: str | None
    urls: tuple[str, ...]
    normalized_terms: str
    cache_key: str


def _normalize_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower()


def _normalize_urls(urls: list[str]) -> tuple[str, ...]:
    # Normalize by stripping whitespace and sorting deterministically.
    cleaned = [u.strip() for u in urls if u and u.strip()]
    # Keep duplicates removed to improve cache hits for same inputs.
    unique = sorted(set(cleaned))
    return tuple(unique)


def _normalize_terms(product_name: str | None, urls: tuple[str, ...]) -> str:
    # Invariant: deterministic normalization; must not depend on environment.
    name = (product_name or "").strip().lower()
    domains = ",".join(sorted({_normalize_domain(u) for u in urls})) if urls else ""
    return f"name={name};domains={domains}"


def _hash_to_cache_key(normalized_terms: str, urls: tuple[str, ...]) -> str:
    payload = normalized_terms + "|" + "|".join(urls)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# PUBLIC_INTERFACE
def normalize_compare_input(product_name: str | None, urls: list[str]) -> NormalizedCompareInput:
    """Normalize compare inputs and generate a stable cache key.

    Contract:
      - Inputs:
          * product_name: optional string
          * urls: list of URL strings (may include whitespace)
      - Outputs: NormalizedCompareInput with:
          * urls sorted+deduped
          * normalized_terms deterministic string
          * cache_key sha256 hex string
      - Errors:
          * None (URL validity is checked at API layer via Pydantic HttpUrl;
            this function assumes URLs are already well-formed strings).
    """
    norm_urls = _normalize_urls(urls)
    normalized_terms = _normalize_terms(product_name, norm_urls)
    cache_key = _hash_to_cache_key(normalized_terms, norm_urls)
    return NormalizedCompareInput(
        product_name=(product_name.strip() if product_name else None),
        urls=norm_urls,
        normalized_terms=normalized_terms,
        cache_key=cache_key,
    )
