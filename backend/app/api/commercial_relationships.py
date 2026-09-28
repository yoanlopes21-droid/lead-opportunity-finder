"""API for lightweight commercial follow-up attached to known local leads."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    CommercialInteraction, CommercialNeedVerification, CommercialRelationship,
    CompanyEnrichment,
)
from app.schemas import (
    CommercialLeadResponse,
    CommercialRelationshipFromLeadRequest,
    CommercialRelationshipListResponse,
    CommercialRelationshipResponse,
    CommercialRelationshipUpdateRequest,
)
from app.services.commercial_leads.approach_pack import get_commercial_approach_pack
from app.services.commercial_leads.service import CommercialLeadQuery, get_commercial_lead, list_commercial_leads
from app.services.contactability.contracts import (
    ContactConfidence, ContactEvidenceInput, ContactPointInput, ContactScope,
    PersonContactInput, PersonRelevanceRole, VerificationStatus,
)
from app.services.contactability.persistence import add_contact_evidence, upsert_contact_point, upsert_person_contact
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
RelationshipView = Literal["all", "ongoing", "follow_up", "planning", "clients", "closed", "do_not_contact"]


class CommercialNeedResponse(BaseModel):
    need_id: str
    title: str
    location: Optional[str]
    source: Optional[str]
    source_url: Optional[str]
    active: bool
    first_seen_at: Optional[datetime]
    last_seen_at: Optional[datetime]
    newly_observed: bool = False


class CommercialDossierResponse(BaseModel):
    relationship: CommercialRelationshipResponse
    active_lead: Optional[CommercialLeadResponse]
    needs: list[CommercialNeedResponse]
    selected_need_id: Optional[str]
    can_prepare_new_outreach: bool
    can_record_interaction: bool
    communication_status: str


class HumanContactCreate(BaseModel):
    channel_type: Literal["phone", "email", "professional_url", "other"]
    value: str = Field(min_length=1, max_length=2048)
    person_name: Optional[str] = Field(default=None, max_length=255)
    role_title: Optional[str] = Field(default=None, max_length=255)
    verified_at: datetime
    provenance: Literal["switchboard", "contact_person", "other"]


class NeedVerificationCreate(BaseModel):
    need_id: str = Field(min_length=1, max_length=600)
    verified_at: datetime
    channel: Literal["phone", "email", "other", "professional_network"]
    note: Optional[str] = Field(default=None, max_length=2000)


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
    elif view == "planning":
        rows = [row for row in rows if row.status in {
            RelationshipStatus.CONTACTED, RelationshipStatus.AWAITING_REPLY,
            RelationshipStatus.FOLLOW_UP, RelationshipStatus.INTERESTED,
            RelationshipStatus.PROPOSAL_SENT, RelationshipStatus.WRONG_CONTACT,
        } and row.next_action_at is None]
    elif view == "clients":
        rows = [row for row in rows if row.status == RelationshipStatus.CLIENT]
    elif view == "closed":
        rows = [row for row in rows if row.status in CLOSED_STATUSES]
    elif view == "do_not_contact":
        rows = [row for row in rows if row.status == RelationshipStatus.DO_NOT_CONTACT]
    rows.sort(key=lambda row: (
        row.next_action_at is None,
        row.next_action_at.isoformat() if row.next_action_at else "",
        row.company_name_snapshot.casefold(),
    ))
    now = datetime.now(timezone.utc)
    return CommercialRelationshipListResponse(items=[_response(row, now) for row in rows], total=len(rows))


def _identity_interactions(session: Session, row: CommercialRelationship) -> list[CommercialInteraction]:
    identity = [CommercialInteraction.company_key == row.company_key]
    if row.siren:
        identity.append(CommercialInteraction.siren == row.siren)
    return list(session.scalars(select(CommercialInteraction).where(
        or_(*identity),
    ).order_by(CommercialInteraction.happened_at.desc(), CommercialInteraction.id.desc())))


def _active_lead(session: Session, row: CommercialRelationship):
    lead = get_commercial_lead(session, row.company_key)
    if lead is not None or not row.siren:
        return lead
    keys = session.scalars(select(CompanyEnrichment.company_key).where(
        CompanyEnrichment.siren == row.siren,
    ).order_by(CompanyEnrichment.id.desc())).all()
    for key in keys:
        lead = get_commercial_lead(session, key)
        if lead is not None:
            return lead
    return None


def _dossier(session: Session, row: CommercialRelationship) -> CommercialDossierResponse:
    interactions = _identity_interactions(session, row)
    lead = _active_lead(session, row)
    seen_need_ids = {item.need_id for item in interactions if item.need_id}
    needs: list[CommercialNeedResponse] = []
    if lead is not None:
        for offer in lead.active_job_offers:
            need_id = f"{offer.source}:{offer.offer_id}"
            needs.append(CommercialNeedResponse(
                need_id=need_id, title=offer.title, location=offer.display_location,
                source=offer.source, source_url=offer.source_url, active=True,
                first_seen_at=offer.first_seen_at, last_seen_at=offer.last_seen_at,
                newly_observed=(need_id not in seen_need_ids and _utc(offer.first_seen_at) > _utc(row.created_at)),
            ))
    active_ids = {item.need_id for item in needs}
    for event in interactions:
        if not event.need_id or event.need_id in active_ids or any(item.need_id == event.need_id for item in needs):
            continue
        needs.append(CommercialNeedResponse(
            need_id=event.need_id, title=event.job_title or "Besoin historique",
            location=event.need_location, source=event.need_source,
            source_url=event.need_source_url, active=False,
            first_seen_at=None, last_seen_at=event.happened_at,
        ))
    selected_need_id = next((item.need_id for item in interactions if item.need_id), None)
    if selected_need_id is None and needs:
        selected_need_id = needs[0].need_id
    if selected_need_id not in active_ids and active_ids:
        selected_need_id = next(item.need_id for item in needs if item.active)
    communication_status = "historical_only"
    if lead is not None and selected_need_id in active_ids:
        pack = get_commercial_approach_pack(session, lead.company_key, selected_need_id=selected_need_id)
        communication_status = pack.communication_status if pack else "blocked"
    can_prepare = communication_status in {"communicable", "prepared_no_channel", "verify_contact"}
    return CommercialDossierResponse(
        relationship=_response(row),
        active_lead=CommercialLeadResponse.from_lead(lead) if lead else None,
        needs=needs, selected_need_id=selected_need_id,
        can_prepare_new_outreach=can_prepare,
        can_record_interaction=row.status != RelationshipStatus.DO_NOT_CONTACT,
        communication_status=communication_status,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@router.get("/{relationship_id}/dossier", response_model=CommercialDossierResponse)
def get_dossier(
    relationship_id: int, session: Session = Depends(get_db),
) -> CommercialDossierResponse:
    row = session.get(CommercialRelationship, relationship_id)
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Ce dossier commercial n’existe pas.")
    return _dossier(session, row)


@router.post("/{relationship_id}/human-contacts", status_code=201)
def add_human_contact(
    relationship_id: int, request: HumanContactCreate,
    session: Session = Depends(get_db),
) -> dict:
    row = session.get(CommercialRelationship, relationship_id)
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Ce dossier commercial n’existe pas.")
    lead = _active_lead(session, row)
    company_key = lead.company_key if lead else row.company_key
    company_name = (lead.official_name or lead.company_name) if lead else row.company_name_snapshot
    person = None
    try:
        if request.person_name and request.person_name.strip():
            person = upsert_person_contact(session, PersonContactInput(
                company_key=company_key, organization_name_snapshot=company_name,
                scope=ContactScope.COMPANY, full_name=request.person_name,
                relevance_role=PersonRelevanceRole.OTHER,
                confidence_level=ContactConfidence.CONFIRMED,
                verification_status=VerificationStatus.MANUALLY_VERIFIED,
                observed_at=request.verified_at, siren=row.siren,
                job_title=request.role_title,
                attribution_reason=f"human:{request.provenance}",
            ))
        point = upsert_contact_point(session, ContactPointInput(
            company_key=company_key, organization_name_snapshot=company_name,
            scope=ContactScope.COMPANY, contact_type=request.channel_type,
            value=request.value, confidence_level=ContactConfidence.CONFIRMED,
            verification_status=VerificationStatus.MANUALLY_VERIFIED,
            observed_at=request.verified_at, siren=row.siren,
            attribution_reason=f"human:{request.provenance}",
            person_contact_id=person.id if person else None,
        ))
        evidence = add_contact_evidence(session, ContactEvidenceInput(
            provider="human_reported", source_name=request.provenance,
            observed_at=request.verified_at,
            evidence_reason="professional_contact_received_during_real_exchange",
        ), contact_point_id=point.id)
        session.commit()
        return {"contact_point_id": point.id, "person_contact_id": person.id if person else None,
                "evidence_id": evidence.id}
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{relationship_id}/need-verifications", status_code=201)
def add_need_verification(
    relationship_id: int, request: NeedVerificationCreate,
    session: Session = Depends(get_db),
) -> dict:
    row = session.get(CommercialRelationship, relationship_id)
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Ce dossier commercial n’existe pas.")
    known_ids = {item.need_id for item in _dossier(session, row).needs}
    if request.need_id not in known_ids:
        raise HTTPException(status_code=422, detail="Ce besoin n’appartient pas à ce dossier.")
    verification = CommercialNeedVerification(
        company_key=row.company_key, siren=row.siren, need_id=request.need_id,
        verified_at=request.verified_at, channel=request.channel,
        note=request.note.strip() if request.note and request.note.strip() else None,
        scope="need_exists",
    )
    session.add(verification)
    session.commit()
    session.refresh(verification)
    return {"id": verification.id, "need_id": verification.need_id,
            "verified_at": verification.verified_at, "scope": verification.scope}


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
