"""CRUD and CSV import API for the existing commercial exclusion policy."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CommercialExclusion, CommercialRelationship
from app.schemas import (
    CommercialExclusionCreateRequest,
    CommercialExclusionCsvRequest,
    CommercialExclusionImportPreviewResponse,
    CommercialExclusionImportResponse,
    CommercialExclusionImportRowResponse,
    CommercialExclusionListResponse,
    CommercialExclusionManagementResponse,
)
from app.services.commercial_leads.exclusions import (
    CommercialExclusionInput,
    CommercialExclusionRecord,
    VALID_EXCLUSION_TYPES,
    create_commercial_exclusion,
    find_duplicate_exclusion,
    is_exclusion_active,
    parse_exclusion_csv,
)
from app.services.opportunities.company import normalize_company_key


router = APIRouter(prefix="/api/v1/commercial-exclusions", tags=["commercial exclusions"])
StatusFilter = Literal["all", "active", "expired"]


def _validation_message(message: str) -> str:
    return {
        "unsupported exclusion_type": "Le type d’exclusion est invalide.",
        "siren must contain exactly 9 digits": "Le SIREN doit contenir exactement 9 chiffres.",
        "company_key must exactly match the normalized company name": "Le nom de l’entreprise est invalide.",
        "expires_at must be after starts_at": "La date d’expiration doit être postérieure à la date de début.",
    }.get(message, message)


def _response(row: CommercialExclusion, now: Optional[datetime] = None) -> CommercialExclusionManagementResponse:
    record = CommercialExclusionRecord.from_model(row)
    active = is_exclusion_active(record, now)
    return CommercialExclusionManagementResponse(
        **record.__dict__, active=active, status=("active" if active else "expired"),
        matching_basis=("siren" if row.siren else "company_key"),
    )


def _input(request: CommercialExclusionCreateRequest) -> CommercialExclusionInput:
    company_name = request.company_name.strip()
    company_key = normalize_company_key(company_name)
    if company_key is None or len(company_name) < 3 or sum(character.isalpha() for character in company_name) < 2:
        raise HTTPException(status_code=422, detail="Saisissez un nom d’entreprise suffisamment précis.")
    if request.exclusion_type not in VALID_EXCLUSION_TYPES:
        raise HTTPException(status_code=422, detail="Le type d’exclusion est invalide.")
    if request.exclusion_type != "recent_prospect" and request.expires_at is not None:
        raise HTTPException(
            status_code=422,
            detail="Une date d’expiration ne peut être définie que pour un prospect récent.",
        )
    return CommercialExclusionInput(
        company_key=company_key,
        company_name_snapshot=company_name,
        exclusion_type=request.exclusion_type,
        siren=request.siren.strip() if request.siren and request.siren.strip() else None,
        reason=request.reason.strip() if request.reason and request.reason.strip() else None,
        starts_at=request.starts_at,
        expires_at=request.expires_at,
    )


@router.get("", response_model=CommercialExclusionListResponse)
def list_exclusions(
    exclusion_type: Annotated[Optional[str], Query(alias="type")] = None,
    status: StatusFilter = "all",
    search: Annotated[Optional[str], Query(min_length=1)] = None,
    session: Session = Depends(get_db),
) -> CommercialExclusionListResponse:
    if exclusion_type is not None and exclusion_type not in VALID_EXCLUSION_TYPES:
        raise HTTPException(status_code=422, detail="Le type d’exclusion est invalide.")
    statement = select(CommercialExclusion)
    if exclusion_type:
        statement = statement.where(CommercialExclusion.exclusion_type == exclusion_type)
    if search:
        statement = statement.where(CommercialExclusion.company_name_snapshot.ilike(f"%{search.strip()}%"))
    rows = session.scalars(statement.order_by(
        CommercialExclusion.created_at.desc(), CommercialExclusion.id.desc(),
    )).all()
    now = datetime.now(timezone.utc)
    items = [_response(row, now) for row in rows]
    if status != "all":
        active = status == "active"
        items = [item for item in items if item.active is active]
    return CommercialExclusionListResponse(items=items, total=len(items))


@router.post("", response_model=CommercialExclusionManagementResponse, status_code=201)
def add_exclusion(
    request: CommercialExclusionCreateRequest,
    session: Session = Depends(get_db),
) -> CommercialExclusionManagementResponse:
    item = _input(request)
    try:
        duplicate = find_duplicate_exclusion(session, item)
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Cette exclusion existe déjà.")
        row = create_commercial_exclusion(session, item)
        session.commit()
        session.refresh(row)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=_validation_message(str(error))) from error
    return _response(row)


@router.delete("/{exclusion_id}", status_code=204)
def delete_exclusion(exclusion_id: int, session: Session = Depends(get_db)) -> Response:
    row = session.get(CommercialExclusion, exclusion_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Cette exclusion n’existe pas.")
    linked_relationship = session.scalar(select(CommercialRelationship).where(
        CommercialRelationship.hard_exclusion_id == exclusion_id,
        CommercialRelationship.is_active.is_(True),
    ))
    if linked_relationship is not None:
        raise HTTPException(
            status_code=409,
            detail="Cette exclusion dépend d’un suivi commercial. Utilisez « Remettre dans les opportunités ».",
        )
    session.delete(row)
    session.commit()
    return Response(status_code=204)


def _signature(item: CommercialExclusionInput) -> tuple[object, ...]:
    return (
        item.company_key, item.exclusion_type, item.siren, item.reason,
        item.starts_at.isoformat() if item.starts_at else None,
        item.expires_at.isoformat() if item.expires_at else None,
    )


def _import_preview(
    content: str, session: Session, *, persist: bool,
) -> tuple[list[CommercialExclusionImportRowResponse], int]:
    try:
        parsed = parse_exclusion_csv(content)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    rows: list[CommercialExclusionImportRowResponse] = []
    seen: set[tuple[object, ...]] = set()
    added = 0
    for parsed_row in parsed:
        item = parsed_row.item
        duplicate = False
        errors = list(parsed_row.errors)
        if item is not None:
            signature = _signature(item)
            duplicate = signature in seen or find_duplicate_exclusion(session, item) is not None
            seen.add(signature)
            if persist and not duplicate:
                create_commercial_exclusion(session, item)
                added += 1
        values = parsed_row.values
        rows.append(CommercialExclusionImportRowResponse(
            line_number=parsed_row.line_number,
            company_name=(item.company_name_snapshot if item else values.get("company_name")),
            exclusion_type=(item.exclusion_type if item else values.get("exclusion_type")),
            siren=(item.siren if item else values.get("siren")),
            reason=(item.reason if item else values.get("reason")),
            starts_at=(item.starts_at if item else None),
            expires_at=(item.expires_at if item else None),
            valid=item is not None,
            duplicate=duplicate,
            errors=errors,
        ))
    return rows, added


@router.post("/import/preview", response_model=CommercialExclusionImportPreviewResponse)
def preview_import(
    request: CommercialExclusionCsvRequest,
    session: Session = Depends(get_db),
) -> CommercialExclusionImportPreviewResponse:
    rows, _ = _import_preview(request.content, session, persist=False)
    return CommercialExclusionImportPreviewResponse(
        rows=rows,
        valid_count=sum(row.valid and not row.duplicate for row in rows),
        duplicate_count=sum(row.duplicate for row in rows),
        invalid_count=sum(not row.valid for row in rows),
    )


@router.post("/import", response_model=CommercialExclusionImportResponse)
def import_exclusions(
    request: CommercialExclusionCsvRequest,
    session: Session = Depends(get_db),
) -> CommercialExclusionImportResponse:
    try:
        rows, added = _import_preview(request.content, session, persist=True)
        session.commit()
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=_validation_message(str(error))) from error
    duplicate_count = sum(row.duplicate for row in rows)
    invalid_count = sum(not row.valid for row in rows)
    return CommercialExclusionImportResponse(
        rows=rows,
        valid_count=added,
        duplicate_count=duplicate_count,
        invalid_count=invalid_count,
        added_count=added,
        ignored_count=duplicate_count + invalid_count,
    )
