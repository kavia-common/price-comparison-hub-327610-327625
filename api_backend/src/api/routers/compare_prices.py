from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.controllers.compare_prices import comparePrices
from src.api.schemas import ComparePricesRequest, ComparePricesResponse

router = APIRouter(prefix="/compare-prices", tags=["Compare"])


@router.post(
    "",
    response_model=ComparePricesResponse,
    summary="Compare prices via parallel scrapers (site1/site2/site3)",
    description=(
        "Accepts a product URL (preferred) or product name/keywords, runs three scrapers in parallel, "
        "and returns aggregated per-site successes/errors. This endpoint is a lightweight controller "
        "that does not use caching/persistence."
    ),
    operation_id="compare_prices_run",
)
async def compare_prices_run(payload: ComparePricesRequest) -> ComparePricesResponse:
    """Run comparePrices controller.

    Parameters:
      payload: ComparePricesRequest with `product` as URL or product name.

    Returns:
      ComparePricesResponse with per-site results and aggregated offers/errors.
    """
    try:
        data = await comparePrices(payload.product)
        return ComparePricesResponse(**data)
    except Exception as exc:  # noqa: BLE001 (boundary)
        # Any unexpected controller-level failure should not leak internals.
        raise HTTPException(status_code=500, detail="comparePrices controller failed.") from exc
