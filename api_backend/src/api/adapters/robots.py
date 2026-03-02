from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from src.api.adapters.http_fetch import DEFAULT_USER_AGENT, FetchRequest, fetch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    reason: str
    details: dict


# PUBLIC_INTERFACE
async def check_robots_allowed(url: str, user_agent: str = DEFAULT_USER_AGENT) -> RobotsDecision:
    """Check robots.txt allowance for a given URL.

    Contract:
      - Inputs: url (string), user_agent (string)
      - Outputs: RobotsDecision
      - Failure modes:
          1) robots.txt unreachable -> allowed=True with reason explaining fallback
          2) parse errors -> allowed=True with reason explaining fallback
      - Notes:
          This is a minimal implementation (hook). It fetches /robots.txt and performs a naive
          'Disallow: /' detection. It is intentionally conservative in code complexity and
          should be upgraded to a full parser later.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return RobotsDecision(
            allowed=False,
            reason="Invalid URL; cannot evaluate robots.txt.",
            details={"url": url},
        )

    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = await fetch(
            FetchRequest(
                url=robots_url,
                timeout_seconds=5.0,
                follow_redirects=True,
                user_agent=user_agent,
                headers={"Accept": "text/plain,*/*;q=0.8"},
            )
        )
        if resp.status_code >= 400:
            return RobotsDecision(
                allowed=True,
                reason=f"robots.txt not accessible (status {resp.status_code}); allowing by fallback.",
                details={"robots_url": robots_url, "status_code": resp.status_code},
            )
        body = resp.text.lower()
    except Exception as exc:  # noqa: BLE001 (boundary: adapter)
        logger.warning("robots check failed; allowing by fallback", extra={"robots_url": robots_url})
        return RobotsDecision(
            allowed=True,
            reason="robots.txt fetch failed; allowing by fallback.",
            details={"robots_url": robots_url, "error": str(exc)},
        )

    # Extremely naive rule: if the robots.txt contains "disallow: /" anywhere, treat as disallowed.
    if "disallow: /" in body:
        return RobotsDecision(
            allowed=False,
            reason="robots.txt indicates full disallow (naive detection of 'Disallow: /').",
            details={"robots_url": robots_url},
        )

    return RobotsDecision(
        allowed=True,
        reason="No full-site disallow detected in robots.txt (naive).",
        details={"robots_url": robots_url},
    )
