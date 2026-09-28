"""Explicitly recorded commercial events and their current-state projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import CommercialInteraction, CommercialNeedVerification, CommercialRelationship
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
NEXT_ACTION_MODES = {"preserve", "replace", "clear"}


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
    CommercialNeedVerification.__table__.create(bind=engine, checkfirst=True)
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("commercial_interactions")}
    additions = {
        "need_id": "VARCHAR(600)",
        "need_source": "VARCHAR(120)",
        "need_location": "VARCHAR(500)",
        "need_source_url": "VARCHAR(2048)",
        "need_status": "VARCHAR(30)",
        "next_action_mode": "VARCHAR(20) NOT NULL DEFAULT 'preserve'",
        "applied_to_current_state": "BOOLEAN NOT NULL DEFAULT 1",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE commercial_interactions ADD COLUMN {name} {definition}"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_commercial_interactions_need_id "
            "ON commercial_interactions (need_id)"
        ))


def record_interaction(
    session: Session, *, request_id: str, company_key: str, siren: Optional[str],
    company_name: str, happened_at: datetime, channel: str, outcome: str,
    need_id: Optional[str] = None, need_source: Optional[str] = None,
    need_location: Optional[str] = None, need_source_url: Optional[str] = None,
    need_status: Optional[str] = None,
    offer_code: Optional[str] = None, job_title: Optional[str] = None,
    contacted_person: Optional[str] = None, note: Optional[str] = None,
    next_action: Optional[str] = None, next_action_at: Optional[datetime] = None,
    priority_expressed: Optional[str] = None, next_action_mode: str = "preserve",
    allow_status_projection: bool = True, relationship_status: Optional[str] = None,
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
    if next_action_mode not in NEXT_ACTION_MODES:
        raise ValueError("invalid next action mode")
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
    current = _find_relationship(session, company_key, siren)
    is_latest = current is None or current.last_contact_at is None or happened_at >= _as_utc(current.last_contact_at)
    apply_current = current is None or (allow_status_projection and is_latest)
    if current is None or apply_current:
        preserved_action = current.next_action if current else None
        preserved_at = current.next_action_at if current else None
        action = preserved_action
        action_at = preserved_at
        if next_action_mode == "replace":
            action = _clean(next_action)
            action_at = next_action_at
        elif next_action_mode == "clear":
            action = None
            action_at = None
        projected_status = (relationship_status or status) if apply_current or current is None else current.status
        projected_contact = happened_at if apply_current or current is None else current.last_contact_at
        row = upsert_relationship(
            session, company_key=company_key, siren=siren, company_name=company_name,
            relationship=current,
            item=CommercialRelationshipInput(
                status=projected_status, last_contact_at=projected_contact,
                next_action_at=action_at, next_action=action,
                note=note if apply_current else current.note,
                outcome=outcome if apply_current else current.outcome,
                contact_point_id=current.contact_point_id if current else None,
                person_contact_id=current.person_contact_id if current else None,
                used_channel=channel if apply_current else current.used_channel,
            ),
        )
    else:
        row = current
    event = CommercialInteraction(request_id=request_id, company_key=company_key, siren=siren,
        happened_at=happened_at, channel=channel, outcome=outcome, resulting_status=status,
        need_id=_clean(need_id), need_source=_clean(need_source),
        need_location=_clean(need_location), need_source_url=_clean(need_source_url),
        need_status=_clean(need_status),
        offer_code=offer_code, job_title=job_title, contacted_person=contacted_person,
        note=note, next_action=next_action, next_action_at=next_action_at,
        priority_expressed=priority_expressed, next_action_mode=next_action_mode,
        applied_to_current_state=apply_current)
    session.add(event)
    session.flush()
    return event, row


def _find_relationship(
    session: Session, company_key: str, siren: Optional[str],
) -> Optional[CommercialRelationship]:
    if siren:
        row = session.scalar(select(CommercialRelationship).where(
            CommercialRelationship.siren == siren,
            CommercialRelationship.is_active.is_(True),
        ).order_by(CommercialRelationship.id))
        if row is not None:
            return row
    return session.scalar(select(CommercialRelationship).where(
        CommercialRelationship.company_key == company_key,
        CommercialRelationship.is_active.is_(True),
    ))


def _clean(value: Optional[str]) -> Optional[str]:
    return value.strip() if value and value.strip() else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
