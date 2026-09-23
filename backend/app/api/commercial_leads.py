"""Read-only HTTP representation of composed commercial leads."""

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import CommercialLeadListResponse, RecentCommercialLeadListResponse
from app.services.commercial_leads.service import (
    CommercialLeadQuery,
    RecentCommercialLeadQuery,
    list_commercial_leads,
    list_recent_commercial_leads,
)
from app.services.scoring.company import ScoreCategory


router = APIRouter(prefix="/api/v1/commercial-leads", tags=["commercial leads"])

CategoryQuery = Literal[
    ScoreCategory.VERY_HIGH,
    ScoreCategory.GOOD,
    ScoreCategory.MEDIUM,
    ScoreCategory.LOW,
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("/recent", response_model=RecentCommercialLeadListResponse)
def get_recent_commercial_leads(
    department: Annotated[str, Query(min_length=1)] = "94",
    window_hours: Annotated[int, Query()] = 48,
    kind: Literal["all", "new_companies", "new_offers"] = "all",
    limit: Annotated[int, Query(gt=0, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_db),
) -> RecentCommercialLeadListResponse:
    """List recently first-seen canonical needs, still prioritized by score."""
    if window_hours not in {24, 48, 168, 720}:
        raise HTTPException(status_code=422, detail="window_hours must be 24, 48, 168, or 720")
    page = list_recent_commercial_leads(session, RecentCommercialLeadQuery(
        department_code=department,
        window_hours=window_hours,
        kind=kind,
        offset=offset,
        limit=limit,
    ))
    return RecentCommercialLeadListResponse.from_page(page)


@router.get("", response_model=CommercialLeadListResponse)
def get_commercial_leads(
    department: Annotated[str, Query(min_length=1)] = "94",
    category: Optional[CategoryQuery] = None,
    entity_sector_type: Annotated[Optional[str], Query(min_length=1)] = None,
    minimum_score: Annotated[Optional[int], Query(ge=0, le=100)] = None,
    include_excluded: bool = False,
    limit: Annotated[int, Query(gt=0, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_db),
) -> CommercialLeadListResponse:
    """List local leads without modifying offers, enrichments, scores, or exclusions."""
    query = CommercialLeadQuery(
        department_code=department,
        include_excluded=include_excluded,
        categories=frozenset((category,)) if category else None,
        entity_sector_types=frozenset((entity_sector_type,)) if entity_sector_type else None,
        minimum_score=minimum_score,
        offset=offset,
        limit=limit,
    )
    page = list_commercial_leads(session, query)
    return CommercialLeadListResponse.from_page(page)
