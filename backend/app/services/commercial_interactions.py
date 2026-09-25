"""Explicitly recorded commercial events and their current-state projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import CommercialInteraction, CommercialRelationship
from app.services.commercial_relationships import CommercialRelationshipInput, RelationshipStatus, upsert_relationship


OUTCOME_STATUSES = {
    "no_answer": RelationshipStatus.CONTACTED,
    "switchboard": RelationshipStatus.CONTACTED,
    "wrong_contact": RelationshipStatus.WRONG_CONTACT,
    "conversation": RelationshipStatus.CONTACTED,
    "email_requested": RelationshipStatus.AWAITING_REPLY,
    "email_sent": RelationshipStatus.AWAITING_REPLY,
    "callback_requested": RelationshipStatus.FOLLOW_UP,
    "interested": RelationshipStatus.INTERESTED,
    "meeting_scheduled": RelationshipStatus.MEETING_SCHEDULED,
    "no_current_need": RelationshipStatus.NO_CURRENT_NEED,
    "position_filled": RelationshipStatus.NO_CURRENT_NEED,
    "refused": RelationshipStatus.REFUSED,
    "proposal_sent": RelationshipStatus.PROPOSAL_SENT,
    "client": RelationshipStatus.CLIENT,
    "do_not_contact": RelationshipStatus.DO_NOT_CONTACT,
}
CHANNELS = {"phone", "email", "other", "professional_network"}


def status_for_outcome(outcome: str) -> str:
    if outcome not in OUTCOME_STATUSES:
        raise ValueError("invalid interaction outcome")
    return OUTCOME_STATUSES[outcome]


def ensure_commercial_interaction_schema(engine: Engine) -> None:
    """Only add missing storage; never rewrite existing commercial rows."""
    inspector = inspect(engine)
    if "commercial_relationships" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("commercial_relationships")}
        if "next_action" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE commercial_relationships ADD COLUMN next_action VARCHAR(255)"))
    CommercialInteraction.__table__.create(bind=engine, checkfirst=True)


def record_interaction(
    session: Session, *, request_id: str, company_key: str, siren: Optional[str],
    company_name: str, happened_at: datetime, channel: str, outcome: str,
    offer_code: Optional[str] = None, job_title: Optional[str] = None,
    contacted_person: Optional[str] = None, note: Optional[str] = None,
    next_action: Optional[str] = None, next_action_at: Optional[datetime] = None,
    priority_expressed: Optional[str] = None,
) -> tuple[CommercialInteraction, CommercialRelationship]:
    existing = session.scalar(select(CommercialInteraction).where(CommercialInteraction.request_id == request_id))
    if existing:
        if existing.company_key != company_key:
            raise ValueError("request id belongs to another company")
        relationship = session.scalar(select(CommercialRelationship).where(CommercialRelationship.company_key == company_key))
        if relationship is None:
            raise ValueError("interaction has no relationship")
        return existing, relationship
    if channel not in CHANNELS:
        raise ValueError("invalid interaction channel")
    happened_at = happened_at.replace(tzinfo=timezone.utc) if happened_at.tzinfo is None else happened_at.astimezone(timezone.utc)
    if happened_at > datetime.now(timezone.utc):
        raise ValueError("interaction cannot be in the future")
    if outcome == "email_requested" and channel != "phone":
        raise ValueError("email request requires a phone exchange")
    if outcome == "email_sent" and channel != "email":
        raise ValueError("email sent requires email channel")
    if priority_expressed and outcome in {"no_answer", "email_sent", "switchboard"}:
        raise ValueError("priority requires a real exchange")
    status = status_for_outcome(outcome)
    row = upsert_relationship(session, company_key=company_key, siren=siren, company_name=company_name,
        item=CommercialRelationshipInput(status=status, last_contact_at=happened_at,
            next_action_at=next_action_at, next_action=next_action, note=note, outcome=outcome,
            used_channel=channel))
    event = CommercialInteraction(request_id=request_id, company_key=company_key, siren=siren,
        happened_at=happened_at, channel=channel, outcome=outcome, resulting_status=status,
        offer_code=offer_code, job_title=job_title, contacted_person=contacted_person,
        note=note, next_action=next_action, next_action_at=next_action_at,
        priority_expressed=priority_expressed)
    session.add(event)
    session.flush()
    return event, row
