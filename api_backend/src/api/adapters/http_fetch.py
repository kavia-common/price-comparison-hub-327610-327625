from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# A realistic desktop Chrome UA string (commonly accepted by many e-commerce sites).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# In-memory per-host rate-limiting. This is process-local (sufficient for single-container runtime).
_HOST_LOCKS: dict[str, asyncio.Lock] = {}
_HOST_LAST_REQUEST_AT: dict[str, float] = {}


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _safe_snippet(text: str, *, limit: int = 400) -> str:
    """Return a short, single-line snippet for logs/errors (best-effort)."""
    return _normalize_ws(text)[:limit]


def _get_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or "").lower()


def _get_lock_for_host(host: str) -> asyncio.Lock:
    # Single-process async lock per host.
    if host not in _HOST_LOCKS:
        _HOST_LOCKS[host] = asyncio.Lock()
    return _HOST_LOCKS[host]


async def _enforce_polite_delay(host: str, *, min_delay_s: float, max_delay_s: float) -> None:
    """Enforce a 2–3s (configurable) gap between requests to the same host.

    We randomize the delay within [min_delay_s, max_delay_s] to avoid an overly-regular pattern.
    """
    if not host:
        return

    desired_gap = random.uniform(min_delay_s, max_delay_s)
    now = time.monotonic()
    last = _HOST_LAST_REQUEST_AT.get(host)

    if last is None:
        return

    wait_s = desired_gap - (now - last)
    if wait_s > 0:
        logger.debug(
            "AntiBotFetchFlow: sleeping before request",
            extra={"host": host, "sleep_s": round(wait_s, 3)},
        )
        await asyncio.sleep(wait_s)


def _parse_retry_after_seconds(headers: dict[str, str]) -> int | None:
    raw = (headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    # Retry-After can be seconds or a date. We only handle integer seconds here.
    try:
        value = int(raw)
        return max(value, 0)
    except ValueError:
        return None


def _detect_block_page_reason(html_text: str) -> str | None:
    """Heuristic detection for bot-block/captcha pages in an HTML body.

    This is intentionally basic and conservative: it flags common captcha/cloudflare/access-denied pages.
    """
    t = (html_text or "").lower()
    if not t:
        return None

    # Common phrases across captcha / block pages.
    signals = [
        "captcha",
        "verify you are human",
        "human verification",
        "access denied",
        "request blocked",
        "blocked due to",
        "unusual traffic",
        "automated queries",
        "bot detection",
        "are you a robot",
        "not a robot",
        "cloudflare",
        "/cdn-cgi/",
        "attention required",
    ]
    for s in signals:
        if s in t:
            return f"Block-page heuristic matched: '{s}'"
    return None


@dataclass(frozen=True)
class FetchRequest:
    """Request contract for AntiBotFetchFlow.

    Attributes:
      url: Target URL to fetch.
      method: HTTP method (GET by default).
      timeout_seconds: Total request timeout in seconds.
      follow_redirects: Whether to follow redirects.
      user_agent: User-Agent header value.
      headers: Additional headers to include (may override defaults).
      min_delay_seconds/max_delay_seconds: Polite per-host delay window.
    """

    url: str
    method: str = "GET"
    timeout_seconds: float = 15.0
    follow_redirects: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    headers: dict[str, str] = field(default_factory=dict)
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 3.0


@dataclass(frozen=True)
class FetchResponse:
    """Response contract for AntiBotFetchFlow (raw fetch).

    Attributes:
      url: Original requested URL.
      final_url: Final URL after redirects.
      status_code: HTTP status code.
      headers: Response headers (best-effort shallow copy).
      text: Response body decoded as text.
      elapsed_ms: Request elapsed time in milliseconds (client-side).
    """

    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    text: str
    elapsed_ms: int


class FetchError(RuntimeError):
    """Base exception type for fetch failures."""


class FetchTransportError(FetchError):
    """Network/transport-level error (DNS/timeout/TLS/etc)."""

    def __init__(self, *, url: str, message: str) -> None:
        super().__init__(f"Fetch transport error for {url}: {message}")
        self.url = url
        self.message = message


class FetchHTTPStatusError(FetchError):
    """Non-2xx HTTP status codes surfaced with context."""

    def __init__(self, *, url: str, status_code: int, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"HTTP {status_code} for {url}: {message}")
        self.url = url
        self.status_code = status_code
        self.message = message
        self.details = details or {}


class FetchAntiBotBlockedError(FetchHTTPStatusError):
    """Raised when anti-bot blocking is detected (403/429 or captcha/block-page heuristics)."""

    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        message: str,
        retry_after_seconds: int | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        full_message = message
        if retry_after_seconds is not None:
            full_message = f"{message} (retry_after_seconds={retry_after_seconds})"
        super().__init__(url=url, status_code=status_code, message=full_message, details=details)
        self.retry_after_seconds = retry_after_seconds


# PUBLIC_INTERFACE
async def fetch(request: FetchRequest) -> FetchResponse:
    """AntiBotFetchFlow (raw fetch): fetch a URL with polite per-host delays.

    Contract:
      - Inputs: FetchRequest (url, headers, UA, timeout, delay window)
      - Output: FetchResponse (status_code, text, headers, final_url, elapsed_ms)
      - Errors:
          * ValueError if request.url is invalid
          * FetchTransportError for network/transport failures
      - Side effects:
          * Sleeps to enforce 2–3s delay between requests to the same host
          * Performs one outbound HTTP request
    """
    parsed = urlparse(request.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid URL: must include http(s) scheme and host.")

    host = _get_host(request.url)
    lock = _get_lock_for_host(host)

    async with lock:
        await _enforce_polite_delay(host, min_delay_s=request.min_delay_seconds, max_delay_s=request.max_delay_seconds)
        _HOST_LAST_REQUEST_AT[host] = time.monotonic()

        headers = {
            "User-Agent": request.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            **(request.headers or {}),
        }

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds,
                follow_redirects=request.follow_redirects,
            ) as client:
                resp = await client.request(request.method, request.url, headers=headers)
        except httpx.HTTPError as exc:  # noqa: BLE001 (boundary: adapter)
            raise FetchTransportError(url=request.url, message=str(exc)) from exc
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)

        # Ensure we always return a text (httpx will decode based on response encoding).
        try:
            text = resp.text
        except Exception as exc:  # noqa: BLE001 (boundary: adapter)
            # Extremely rare, but keep the contract stable.
            text = ""
            logger.warning(
                "AntiBotFetchFlow: failed decoding response text",
                extra={"url": request.url, "status_code": resp.status_code, "error": str(exc)},
            )

        # Shallow-copy headers into a normal dict for easier serialization.
        resp_headers = {k: v for k, v in resp.headers.items()}

        return FetchResponse(
            url=request.url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            headers=resp_headers,
            text=text,
            elapsed_ms=elapsed_ms,
        )


# PUBLIC_INTERFACE
async def fetch_html_or_raise(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_seconds: float = 15.0,
    min_delay_seconds: float = 2.0,
    max_delay_seconds: float = 3.0,
) -> str:
    """Fetch HTML with basic anti-bot detection and clear error results.

    Contract:
      - Inputs:
          * url: http(s) URL
          * user_agent: realistic UA string (default provided)
          * timeout_seconds: request timeout
          * min_delay_seconds/max_delay_seconds: polite delay window (2–3s default)
      - Output:
          * HTML body (text)
      - Errors:
          * FetchAntiBotBlockedError when status is 403/429 OR block-page heuristics match
          * FetchHTTPStatusError for other non-2xx statuses
          * FetchTransportError for network/transport failures
      - Side effects:
          * Enforces per-host delay (sleep)
          * Performs one outbound HTTP request
    """
    resp = await fetch(
        FetchRequest(
            url=url,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            min_delay_seconds=min_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    )

    # Explicit anti-bot status codes.
    if resp.status_code in {403, 429}:
        retry_after = _parse_retry_after_seconds(resp.headers)
        details = {
            "final_url": resp.final_url,
            "elapsed_ms": resp.elapsed_ms,
            "snippet": _safe_snippet(resp.text),
        }
        logger.warning(
            "AntiBotFetchFlow: blocked by status code",
            extra={"url": url, "status_code": resp.status_code, "retry_after": retry_after},
        )
        raise FetchAntiBotBlockedError(
            url=url,
            status_code=resp.status_code,
            message="Request blocked (possible anti-bot/ratelimit).",
            retry_after_seconds=retry_after,
            details=details,
        )

    # Non-2xx/3xx error.
    if resp.status_code >= 400:
        raise FetchHTTPStatusError(
            url=url,
            status_code=resp.status_code,
            message="Non-success HTTP response while fetching HTML.",
            details={"final_url": resp.final_url, "elapsed_ms": resp.elapsed_ms, "snippet": _safe_snippet(resp.text)},
        )

    # Heuristic block-page detection (captcha, cloudflare, access denied, etc).
    heuristic_reason = _detect_block_page_reason(resp.text)
    if heuristic_reason:
        logger.warning(
            "AntiBotFetchFlow: blocked by heuristic",
            extra={"url": url, "status_code": resp.status_code, "reason": heuristic_reason},
        )
        raise FetchAntiBotBlockedError(
            url=url,
            status_code=resp.status_code,
            message=heuristic_reason,
            retry_after_seconds=None,
            details={"final_url": resp.final_url, "elapsed_ms": resp.elapsed_ms, "snippet": _safe_snippet(resp.text)},
        )

    return resp.text
