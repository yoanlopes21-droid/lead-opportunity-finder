"""API for lightweight commercial follow-up attached to known local leads."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CommercialRelationship
from app.schemas import (
    CommercialRelationshipFromLeadRequest,
    CommercialRelationshipListResponse,
    CommercialRelationshipResponse,
    CommercialRelationshipUpdateRequest,
)
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.commercial_relationships import (
    CLOSED_STATUSES,
    ONGOING_STATUSES,
    CommercialRelationshipInput,
    CommercialRelationshipRecord,
    RelationshipStatus,
    reopen_opportunity,
    upsert_relationship,
)


router = APIRouter(prefix="/api/v1/commercial-relationships", tags=["commercial relationships"])
RelationshipView = Literal["all", "ongoing", "follow_up", "clients", "closed"]


def _input(request: CommercialRelationshipUpdateRequest) -> CommercialRelationshipInput:
    return CommercialRelationshipInput(
        status=request.status,
        last_contact_at=request.last_contact_at,
        next_action_at=request.next_action_at,
        next_action=request.next_action,
        note=request.note,
        outcome=request.outcome,
        contact_point_id=request.contact_point_id,
        person_contact_id=request.person_contact_id,
        used_channel=request.used_channel,
    )


def _error_message(message: str) -> str:
    return {
        "invalid relationship status": "Le statut commercial est invalide.",
        "follow_up requires next_action_at": "Une date de relance est obligatoire pour le statut À relancer.",
        "meeting_scheduled requires next_action_at": "La date du rendez-vous est obligatoire.",
        "invalid contact point": "La coordonnée sélectionnée n’appartient pas à cette entreprise.",
        "invalid person contact": "La personne sélectionnée n’appartient pas à cette entreprise.",
    }.get(message, message)


def _response(row: CommercialRelationship, now: Optional[datetime] = None) -> CommercialRelationshipResponse:
    record = CommercialRelationshipRecord.from_model(row)
    timing = None
    if record.next_action_at is not None:
        observed_at = now or datetime.now(timezone.utc)
        next_date = record.next_action_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Paris")).date() if record.next_action_at.tzinfo is None else record.next_action_at.astimezone(ZoneInfo("Europe/Paris")).date()
        today = observed_at.astimezone(ZoneInfo("Europe/Paris")).date()
        timing = "overdue" if next_date < today else "today" if next_date == today else "upcoming"
    return CommercialRelationshipResponse(**record.__dict__, follow_up_timing=timing)


@router.get("", response_model=CommercialRelationshipListResponse)
def list_relationships(
    view: RelationshipView = "ongoing",
    search: Annotated[Optional[str], Query(min_length=1)] = None,
    session: Session = Depends(get_db),
) -> CommercialRelationshipListResponse:
    statement = select(CommercialRelationship).where(CommercialRelationship.is_active.is_(True))
    if search:
        statement = statement.where(CommercialRelationship.company_name_snapshot.ilike(f"%{search.strip()}%"))
    rows = list(session.scalars(statement).all())
    if view == "ongoing":
        rows = [row for row in rows if row.status in ONGOING_STATUSES]
    elif view == "follow_up":
        rows = [row for row in rows if (row.next_action_at is not None or row.status == RelationshipStatus.FOLLOW_UP) and row.status not in {
            RelationshipStatus.CLIENT, RelationshipStatus.DO_NOT_CONTACT,
        }]
    elif view == "clients":
        rows = [row for row in rows if row.status == RelationshipStatus.CLIENT]
    elif view == "closed":
        rows = [row for row in rows if row.status in CLOSED_STATUSES]
    rows.sort(key=lambda row: (
        row.next_action_at is None,
        row.next_action_at.isoformat() if row.next_action_at else "",
        row.company_name_snapshot.casefold(),
    ))
    now = datetime.now(timezone.utc)
    return CommercialRelationshipListResponse(items=[_response(row, now) for row in rows], total=len(rows))


@router.post("/from-lead", response_model=CommercialRelationshipResponse, status_code=201)
def create_from_lead(
    request: CommercialRelationshipFromLeadRequest,
    session: Session = Depends(get_db),
) -> CommercialRelationshipResponse:
    leads = list_commercial_leads(session, CommercialLeadQuery(
        department_code="94", include_excluded=True, limit=None,
    )).items
    lead = next((item for item in leads if item.company_key == request.company_key), None)
    if lead is None:
        raise HTTPException(status_code=404, detail="Cette entreprise n’est pas une opportunité locale connue.")
    try:
        row = upsert_relationship(
            session,
            company_key=lead.company_key,
            siren=lead.siren,
            company_name=lead.official_name or lead.company_name,
            item=_input(request),
        )
        session.commit()
        session.refresh(row)
        return _response(row)
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=_error_message(str(error))) from error


@router.patch("/{relationship_id}", response_model=CommercialRelationshipResponse)
def update_relationship(
    relationship_id: int,
    request: CommercialRelationshipUpdateRequest,
    session: Session = Depends(get_db),
) -> CommercialRelationshipResponse:
    row = session.get(CommercialRelationship, relationship_id)
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Ce suivi commercial n’existe pas.")
    try:
        upsert_relationship(
            session,
            company_key=row.company_key,
            siren=row.siren,
            company_name=row.company_name_snapshot,
            item=_input(request),
            relationship=row,
        )
        session.commit()
        session.refresh(row)
        return _response(row)
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=_error_message(str(error))) from error


@router.post("/{relationship_id}/reopen-opportunity", status_code=204)
def reopen_as_opportunity(
    relationship_id: int,
    session: Session = Depends(get_db),
) -> None:
    row = session.get(CommercialRelationship, relationship_id)
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Ce suivi commercial n’existe pas.")
    reopen_opportunity(session, row)
    session.commit()
