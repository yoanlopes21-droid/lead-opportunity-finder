"""Read-only local Brave Search usage estimates; never a provider billing API."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas import BraveUsageResponse
from app.services.brave_usage import BraveBudgetPolicy, BraveUsageService


router = APIRouter(prefix="/api/v1/brave-usage", tags=["brave usage"])


@router.get("", response_model=BraveUsageResponse)
def get_brave_usage(session: Session = Depends(get_db)) -> BraveUsageResponse:
    settings = get_settings()
    snapshot = BraveUsageService(session, BraveBudgetPolicy(
        monthly_request_budget=settings.brave_search_monthly_request_budget,
        default_run_hard_cap=settings.brave_search_default_run_hard_cap,
        estimated_price_per_1000_usd=settings.brave_search_estimated_price_per_1000_usd,
        estimated_monthly_free_credit_usd=settings.brave_search_estimated_monthly_free_credit_usd,
    )).snapshot()
    return BraveUsageResponse(**snapshot.__dict__)
