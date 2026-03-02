from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


def _strip_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def _detect_currency(price_text: str, currency_hint: str | None) -> str | None:
    """Best-effort currency detection from a price string + optional hint."""
    hint = (currency_hint or "").strip().upper()
    text_u = price_text.upper()

    # Prefer explicit hint if it looks valid.
    if hint in {"INR", "USD"}:
        return hint

    # Symbol / label heuristics.
    if "₹" in price_text or "INR" in text_u or "RS" in text_u:
        return "INR"
    if "$" in price_text or "USD" in text_u:
        return "USD"

    return None


def _extract_number_text(price_text: str) -> str:
    """Extract a decimal-like number from free-form text.

    Keeps digits and at most one decimal dot. Removes commas and other symbols.
    """
    cleaned = _strip_text(price_text).replace(",", "")

    # Keep digits, dot, minus (rare, but keep it).
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned:
        return ""

    # If there are multiple dots due to bad cleaning, keep first portion + first dot segment.
    if cleaned.count(".") > 1:
        first, *rest = cleaned.split(".")
        cleaned = first + "." + rest[0]

    # If there are multiple '-' characters, keep only a leading '-'.
    if cleaned.count("-") > 1:
        cleaned = "-" + cleaned.replace("-", "")
    if "-" in cleaned and not cleaned.startswith("-"):
        cleaned = cleaned.replace("-", "")

    return cleaned


# PUBLIC_INTERFACE
def cleanPrice(
    price: str | int | float | Decimal | None,
    *,
    currency_hint: str | None = None,
    target_currency: str = "INR",
    fx_rates: dict[tuple[str, str], float] | None = None,
) -> float | None:
    """Normalize and standardize a scraped price into a numeric INR value.

    Flow name: CleanPriceNormalization

    Contract:
      - Inputs:
          * price: raw price value (string/number) or None
          * currency_hint: optional currency code hint from the scraper/JSON-LD (e.g., "INR", "USD")
          * target_currency: currency code to standardize to (default "INR")
          * fx_rates: optional mapping of (from_currency, to_currency) -> multiplier.
              Example: {("USD", "INR"): 83.2}

      - Behavior:
          * Strips common currency symbols/labels and thousands separators (commas).
          * Extracts the first decimal-like number and parses it as float.
          * Converts to target currency when:
              - detected_currency != target_currency, AND
              - fx_rates contains a conversion rate for (detected_currency, target_currency).
          * If conversion rate is missing, returns the numeric amount unchanged but still
            considered standardized to target_currency by the caller (best-effort).

      - Output:
          * float numeric amount in target currency major units (e.g., INR rupees), or None if parsing fails.

      - Errors:
          * None raised intentionally; returns None on parse failures to keep scraper flows resilient.

    Notes:
      - This function does NOT perform network calls for exchange rates.
      - Callers that require accurate conversion should pass fx_rates explicitly.
    """
    if price is None:
        return None

    target = (target_currency or "INR").strip().upper()

    detected_currency: str | None = None
    amount: Decimal | None = None

    if isinstance(price, (int, float, Decimal)):
        detected_currency = (currency_hint or "").strip().upper() or None
        try:
            amount = Decimal(str(price))
        except (InvalidOperation, ValueError):
            return None
    elif isinstance(price, str):
        if not price.strip():
            return None
        detected_currency = _detect_currency(price, currency_hint)

        number_text = _extract_number_text(price)
        if not number_text:
            return None
        try:
            amount = Decimal(number_text)
        except (InvalidOperation, ValueError):
            return None
    else:
        return None

    detected = (detected_currency or target).upper()

    # Convert if we can; otherwise best-effort keep numeric value.
    if detected != target and fx_rates:
        rate = fx_rates.get((detected, target))
        if rate is not None:
            amount = amount * Decimal(str(rate))

    try:
        return float(amount)
    except (TypeError, ValueError):
        return None
