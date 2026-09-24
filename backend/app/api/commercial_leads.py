"""Read-only HTTP representation and export of composed commercial leads."""

from datetime import datetime, timezone
from dataclasses import asdict
from io import BytesIO
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    CommercialApproachContextResponse, CommercialLeadListResponse, RecentCommercialLeadListResponse,
)
from app.services.commercial_leads.service import (
    CommercialLeadQuery,
    RecentCommercialLeadQuery,
    list_commercial_leads,
    list_recent_commercial_leads,
)
from app.services.commercial_leads.excel_export import (
    XLSX_MEDIA_TYPE,
    CommercialExcelItem,
    build_commercial_xlsx,
    export_filename,
)
from app.services.scoring.company import ScoreCategory
from app.services.commercial_leads.approach import get_commercial_approach_context


router = APIRouter(prefix="/api/v1/commercial-leads", tags=["commercial leads"])

CategoryQuery = Literal[
    ScoreCategory.VERY_HIGH,
    ScoreCategory.GOOD,
    ScoreCategory.MEDIUM,
    ScoreCategory.LOW,
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("/{company_key}/approach-context", response_model=CommercialApproachContextResponse)
def get_approach_context(
    company_key: str,
    department: Annotated[str, Query(min_length=1)] = "94",
    session: Session = Depends(get_db),
) -> CommercialApproachContextResponse:
    """Inspect reliable commercial inputs for one existing lead; no discovery or writes."""
    context = get_commercial_approach_context(session, company_key, department)
    if context is None:
        raise HTTPException(status_code=404, detail="Commercial lead not found")
    return CommercialApproachContextResponse.model_validate(asdict(context))


def _xlsx_response(content: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(content),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export.xlsx")
def export_commercial_leads(
    department: Annotated[str, Query(min_length=1)] = "94",
    category: Optional[CategoryQuery] = None,
    entity_sector_type: Annotated[Optional[str], Query(min_length=1)] = None,
    minimum_score: Annotated[Optional[int], Query(ge=0, le=100)] = None,
    include_excluded: bool = False,
    session: Session = Depends(get_db),
) -> StreamingResponse:
    """Export the complete matching lead set, independently of UI pagination."""
    generated_at = datetime.now(timezone.utc)
    page = list_commercial_leads(session, CommercialLeadQuery(
        department_code=department,
        include_excluded=include_excluded,
        categories=frozenset((category,)) if category else None,
        entity_sector_types=frozenset((entity_sector_type,)) if entity_sector_type else None,
        minimum_score=minimum_score,
        limit=None,
    ), now=generated_at)
    content = build_commercial_xlsx(
        tuple(CommercialExcelItem(lead=item) for item in page.items),
        generated_at=generated_at,
    )
    return _xlsx_response(content, export_filename(generated_at=generated_at))


@router.get("/recent/export.xlsx")
def export_recent_commercial_leads(
    department: Annotated[str, Query(min_length=1)] = "94",
    window_hours: Annotated[int, Query()] = 48,
    kind: Literal["all", "new_companies", "new_offers"] = "all",
    session: Session = Depends(get_db),
) -> StreamingResponse:
    """Export the complete recent view with the same window and kind rules."""
    if window_hours not in {24, 48, 168, 720}:
        raise HTTPException(status_code=422, detail="window_hours must be 24, 48, 168, or 720")
    generated_at = datetime.now(timezone.utc)
    page = list_recent_commercial_leads(session, RecentCommercialLeadQuery(
        department_code=department,
        window_hours=window_hours,
        kind=kind,
        limit=None,
    ), now=generated_at)
    content = build_commercial_xlsx(tuple(
        CommercialExcelItem(
            lead=item.lead,
            latest_new_opportunity_at=item.latest_new_opportunity_at,
            is_new_company_in_window=item.is_new_company_in_window,
            new_offer_ids_in_window=frozenset(item.new_offer_ids_in_window),
        )
        for item in page.items
    ), generated_at=generated_at)
    return _xlsx_response(
        content,
        export_filename(generated_at=generated_at, suffix=f"nouveautes-{window_hours}h"),
    )


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
