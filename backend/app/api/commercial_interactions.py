"""History is written only after explicit user confirmation."""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CommercialInteraction, CommercialRelationship
from app.schemas import CommercialRelationshipResponse
from app.services.commercial_interactions import record_interaction
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
    contacted_person: Optional[str] = Field(default=None, max_length=255)
    note: Optional[str] = Field(default=None, max_length=2000)
    next_action: Optional[str] = Field(default=None, max_length=255)
    next_action_at: Optional[datetime] = None
    priority_expressed: Optional[str] = Field(default=None, max_length=500)


class InteractionResponse(BaseModel):
    id: int
    company_key: str
    siren: Optional[str]
    happened_at: datetime
    channel: str
    outcome: str
    resulting_status: str
    offer_code: Optional[str]
    job_title: Optional[str]
    contacted_person: Optional[str]
    note: Optional[str]
    next_action: Optional[str]
    next_action_at: Optional[datetime]
    priority_expressed: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


class InteractionSaved(BaseModel):
    interaction: InteractionResponse
    relationship: CommercialRelationshipResponse


class InteractionHistory(BaseModel):
    items: list[InteractionResponse]


@router.get("/{company_key}", response_model=InteractionHistory)
def history(company_key: str, session: Session = Depends(get_db)) -> InteractionHistory:
    items = session.scalars(select(CommercialInteraction).where(CommercialInteraction.company_key == company_key)
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
    if lead is None:
        raise HTTPException(404, "Cette entreprise n’est pas une opportunité locale connue.")
    if lead.exclusion and lead.exclusion.exclusion_type in {"manual_exclusion", "current_client"}:
        raise HTTPException(409, "Cette entreprise est exclue de la prospection.")
    if request.offer_code and not any(item.code == request.offer_code and item.enabled_for_prospecting for item in list_offers(session)):
        raise HTTPException(422, "Cette offre commerciale n’est pas active.")
    pack = get_commercial_approach_pack(session, request.company_key, selected_offer_code=request.offer_code)
    if pack is None or pack.communication_status == "blocked":
        raise HTTPException(409, "La prospection est suspendue pour cette entreprise.")
    try:
        event, relationship = record_interaction(session, request_id=str(request.request_id),
            company_key=lead.company_key, siren=lead.siren, company_name=lead.official_name or lead.company_name,
            happened_at=request.happened_at, channel=request.channel, outcome=request.outcome,
            offer_code=pack.internal.selected_offer, job_title=pack.entry_offer.title,
            contacted_person=request.contacted_person,
            note=request.note, next_action=request.next_action, next_action_at=request.next_action_at,
            priority_expressed=request.priority_expressed)
        session.commit()
        session.refresh(event)
        session.refresh(relationship)
        return InteractionSaved(interaction=InteractionResponse.model_validate(event), relationship=relationship_response(relationship))
    except ValueError as error:
        session.rollback()
        raise HTTPException(422, str(error)) from error
