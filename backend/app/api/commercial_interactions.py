"""History is written only after explicit user confirmation."""

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CommercialInteraction, CommercialRelationship, CompanyEnrichment
from app.schemas import CommercialRelationshipResponse
from app.services.commercial_interactions import record_interaction
from app.services.commercial_relationships import RelationshipStatus
from app.services.commercial_leads.service import get_commercial_lead
from app.services.commercial_leads.approach_pack import get_commercial_approach_pack
from app.services.commercial_configuration import list_offers
from app.api.commercial_relationships import _response as relationship_response


router = APIRouter(prefix="/api/v1/commercial-interactions", tags=["commercial interactions"])


class InteractionCreate(BaseModel):
    request_id: UUID
    company_key: str = Field(min_length=1, max_length=500)
    happened_at: datetime
    channel: Literal["phone", "email", "other", "professional_network"]
    outcome: Literal["no_answer", "switchboard", "wrong_contact", "conversation", "email_requested",
                     "email_sent", "callback_requested", "interested", "meeting_scheduled", "no_current_need",
                     "position_filled", "refused", "proposal_sent", "client", "do_not_contact"]
    offer_code: Optional[str] = Field(default=None, max_length=100)
    need_id: Optional[str] = Field(default=None, max_length=600)
    contacted_person: Optional[str] = Field(default=None, max_length=255)
    note: Optional[str] = Field(default=None, max_length=2000)
    next_action: Optional[str] = Field(default=None, max_length=255)
    next_action_at: Optional[datetime] = None
    next_action_mode: Literal["preserve", "replace", "clear"] = "preserve"
    priority_expressed: Optional[str] = Field(default=None, max_length=500)


class InteractionResponse(BaseModel):
    id: int
    company_key: str
    siren: Optional[str]
    happened_at: datetime
    channel: str
    outcome: str
    resulting_status: str
    need_id: Optional[str]
    need_source: Optional[str]
    need_location: Optional[str]
    need_source_url: Optional[str]
    need_status: Optional[str]
    offer_code: Optional[str]
    job_title: Optional[str]
    contacted_person: Optional[str]
    note: Optional[str]
    next_action: Optional[str]
    next_action_at: Optional[datetime]
    priority_expressed: Optional[str]
    next_action_mode: str
    applied_to_current_state: bool
    created_at: datetime
    model_config = {"from_attributes": True}

    @field_validator("happened_at", "next_action_at", "created_at", mode="before")
    @classmethod
    def attach_utc_to_sqlite_datetimes(cls, value):
        return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value


class InteractionSaved(BaseModel):
    interaction: InteractionResponse
    relationship: CommercialRelationshipResponse


class InteractionHistory(BaseModel):
    items: list[InteractionResponse]


@router.get("/{company_key}", response_model=InteractionHistory)
def history(company_key: str, session: Session = Depends(get_db)) -> InteractionHistory:
    identity = [CommercialInteraction.company_key == company_key]
    relationship = session.scalar(select(CommercialRelationship).where(
        CommercialRelationship.company_key == company_key,
        CommercialRelationship.is_active.is_(True),
    ))
    if relationship and relationship.siren:
        identity.append(CommercialInteraction.siren == relationship.siren)
    items = session.scalars(select(CommercialInteraction).where(or_(*identity))
                            .order_by(CommercialInteraction.happened_at.desc(), CommercialInteraction.id.desc()).limit(30)).all()
    return InteractionHistory(items=[InteractionResponse.model_validate(item) for item in items])


@router.post("", response_model=InteractionSaved, status_code=201)
def create_interaction(request: InteractionCreate, session: Session = Depends(get_db)) -> InteractionSaved:
    existing = session.scalar(select(CommercialInteraction).where(CommercialInteraction.request_id == str(request.request_id)))
    if existing:
        if existing.company_key != request.company_key:
            raise HTTPException(409, "Ce formulaire a déjà été utilisé pour une autre entreprise.")
        relationship = session.scalar(select(CommercialRelationship).where(CommercialRelationship.company_key == request.company_key))
        if relationship:
            return InteractionSaved(interaction=InteractionResponse.model_validate(existing), relationship=relationship_response(relationship))
    lead = get_commercial_lead(session, request.company_key)
    relationship = session.scalar(select(CommercialRelationship).where(
        CommercialRelationship.company_key == request.company_key,
        CommercialRelationship.is_active.is_(True),
    ))
    if lead is not None and relationship is None and lead.siren:
        relationship = session.scalar(select(CommercialRelationship).where(
            CommercialRelationship.siren == lead.siren,
            CommercialRelationship.is_active.is_(True),
        ).order_by(CommercialRelationship.id))
    if lead is None and relationship and relationship.siren:
        keys = session.scalars(select(CompanyEnrichment.company_key).where(
            CompanyEnrichment.siren == relationship.siren,
        ).order_by(CompanyEnrichment.id.desc())).all()
        lead = next((candidate for key in keys if (candidate := get_commercial_lead(session, key)) is not None), None)
    if lead is None and relationship is None:
        raise HTTPException(404, "Cette entreprise n’est pas un dossier commercial local connu.")
    if request.offer_code and not any(item.code == request.offer_code and item.enabled_for_prospecting for item in list_offers(session)):
        raise HTTPException(422, "Cette offre commerciale n’est pas active.")
    active_need_ids = {
        f"{item.source}:{item.offer_id}" for item in lead.active_job_offers
    } if lead is not None else set()
    selected_is_historical = bool(request.need_id and request.need_id not in active_need_ids)
    pack = get_commercial_approach_pack(
        session, lead.company_key, selected_offer_code=request.offer_code,
        selected_need_id=request.need_id,
    ) if lead is not None and not selected_is_historical else None
    if relationship is None and (pack is None or pack.communication_status == "blocked"):
        raise HTTPException(409, "La prospection est suspendue pour cette entreprise.")
    selected_need_id = None
    need_source = need_location = need_source_url = job_title = None
    if pack is not None:
        selected_need_id = f"{pack.entry_offer.source}:{pack.entry_offer.offer_id}"
        if request.need_id and request.need_id != selected_need_id:
            raise HTTPException(422, "Le besoin sélectionné n’est pas actif dans ce dossier.")
        need_source = pack.entry_offer.source
        need_location = pack.entry_offer.location
        need_source_url = pack.entry_offer.source_urls[0] if pack.entry_offer.source_urls else None
        job_title = pack.entry_offer.title
    elif relationship is not None:
        previous = session.scalar(select(CommercialInteraction).where(
            or_(CommercialInteraction.company_key == relationship.company_key,
                CommercialInteraction.siren == relationship.siren if relationship.siren else False),
            CommercialInteraction.need_id == request.need_id if request.need_id else True,
        ).order_by(CommercialInteraction.happened_at.desc(), CommercialInteraction.id.desc()))
        if previous:
            selected_need_id = previous.need_id
            need_source, need_location = previous.need_source, previous.need_location
            need_source_url, job_title = previous.need_source_url, previous.job_title
    next_action_mode = request.next_action_mode
    if "next_action_mode" not in request.model_fields_set and (
        "next_action" in request.model_fields_set or "next_action_at" in request.model_fields_set
    ):
        next_action_mode = "replace"
    if request.outcome == "meeting_scheduled" and next_action_mode != "replace":
        raise HTTPException(422, "Un rendez-vous doit définir une nouvelle suite datée.")
    current_status = relationship.status if relationship else None
    protected = current_status in {RelationshipStatus.CLIENT, RelationshipStatus.DO_NOT_CONTACT}
    multiple_active_needs = bool(lead and len(lead.active_job_offers) > 1)
    projected_status = (
        RelationshipStatus.CONTACTED
        if request.outcome == "position_filled" and multiple_active_needs and relationship is None
        else None
    )
    allow_projection = not protected and not (request.outcome == "position_filled" and multiple_active_needs and relationship is not None)
    try:
        event, relationship = record_interaction(session, request_id=str(request.request_id),
            company_key=lead.company_key if lead else relationship.company_key,
            siren=lead.siren if lead else relationship.siren,
            company_name=(lead.official_name or lead.company_name) if lead else relationship.company_name_snapshot,
            happened_at=request.happened_at, channel=request.channel, outcome=request.outcome,
            need_id=selected_need_id, need_source=need_source, need_location=need_location,
            need_source_url=need_source_url,
            need_status="filled" if request.outcome == "position_filled" else None,
            offer_code=pack.internal.selected_offer if pack else request.offer_code,
            job_title=job_title,
            contacted_person=request.contacted_person,
            note=request.note, next_action=request.next_action, next_action_at=request.next_action_at,
            priority_expressed=request.priority_expressed,
            next_action_mode=next_action_mode,
            allow_status_projection=allow_projection,
            relationship_status=projected_status)
        session.commit()
        session.refresh(event)
        session.refresh(relationship)
        return InteractionSaved(interaction=InteractionResponse.model_validate(event), relationship=relationship_response(relationship))
    except ValueError as error:
        session.rollback()
        raise HTTPException(422, str(error)) from error
