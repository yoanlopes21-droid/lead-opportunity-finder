"""Lightweight commercial follow-up state layered over hard exclusions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CommercialExclusion, CommercialRelationship, ContactPoint, PersonContact
from app.services.commercial_leads.exclusions import (
    CommercialExclusionInput,
    ExclusionType,
    create_commercial_exclusion,
)


class RelationshipStatus:
    CONTACTED = "contacted"
    AWAITING_REPLY = "awaiting_reply"
    FOLLOW_UP = "follow_up"
    INTERESTED = "interested"
    MEETING_SCHEDULED = "meeting_scheduled"
    PROPOSAL_SENT = "proposal_sent"
    CLIENT = "client"
    NO_CURRENT_NEED = "no_current_need"
    REFUSED = "refused"
    WRONG_CONTACT = "wrong_contact"
    DO_NOT_CONTACT = "do_not_contact"


VALID_RELATIONSHIP_STATUSES = {
    RelationshipStatus.CONTACTED,
    RelationshipStatus.AWAITING_REPLY,
    RelationshipStatus.FOLLOW_UP,
    RelationshipStatus.INTERESTED,
    RelationshipStatus.MEETING_SCHEDULED,
    RelationshipStatus.PROPOSAL_SENT,
    RelationshipStatus.CLIENT,
    RelationshipStatus.NO_CURRENT_NEED,
    RelationshipStatus.REFUSED,
    RelationshipStatus.WRONG_CONTACT,
    RelationshipStatus.DO_NOT_CONTACT,
}
ONGOING_STATUSES = {
    RelationshipStatus.CONTACTED,
    RelationshipStatus.AWAITING_REPLY,
    RelationshipStatus.FOLLOW_UP,
    RelationshipStatus.INTERESTED,
    RelationshipStatus.MEETING_SCHEDULED,
    RelationshipStatus.PROPOSAL_SENT,
    RelationshipStatus.WRONG_CONTACT,
}
CLOSED_STATUSES = {RelationshipStatus.NO_CURRENT_NEED, RelationshipStatus.REFUSED}
HARD_EXCLUSION_TYPES = {
    RelationshipStatus.CLIENT: ExclusionType.CURRENT_CLIENT,
    RelationshipStatus.DO_NOT_CONTACT: ExclusionType.MANUAL_EXCLUSION,
}
HARD_EXCLUSION_REASONS = {
    RelationshipStatus.CLIENT: "Suivi commercial : client",
    RelationshipStatus.DO_NOT_CONTACT: "Suivi commercial : ne plus contacter",
}


@dataclass(frozen=True)
class CommercialRelationshipInput:
    status: str
    last_contact_at: Optional[datetime] = None
    next_action_at: Optional[datetime] = None
    note: Optional[str] = None
    outcome: Optional[str] = None
    contact_point_id: Optional[int] = None
    person_contact_id: Optional[int] = None
    used_channel: Optional[str] = None


@dataclass(frozen=True)
class CommercialRelationshipRecord:
    id: int
    company_key: str
    siren: Optional[str]
    company_name_snapshot: str
    status: str
    last_contact_at: Optional[datetime]
    next_action_at: Optional[datetime]
    note: Optional[str]
    outcome: Optional[str]
    contact_point_id: Optional[int]
    person_contact_id: Optional[int]
    used_channel: Optional[str]
    hard_exclusion_id: Optional[int]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: CommercialRelationship) -> "CommercialRelationshipRecord":
        return cls(**{name: getattr(row, name) for name in cls.__dataclass_fields__})


def find_relationship(
    company_key: str,
    siren: Optional[str],
    relationships: Sequence[CommercialRelationshipRecord],
) -> Optional[CommercialRelationshipRecord]:
    active = [item for item in relationships if item.is_active]
    if siren:
        by_siren = [item for item in active if item.siren == siren]
        if by_siren:
            return min(by_siren, key=lambda item: item.id)
    by_key = [item for item in active if item.company_key == company_key]
    return min(by_key, key=lambda item: item.id) if by_key else None


def validate_relationship_input(item: CommercialRelationshipInput) -> None:
    if item.status not in VALID_RELATIONSHIP_STATUSES:
        raise ValueError("invalid relationship status")
    if item.status == RelationshipStatus.FOLLOW_UP and item.next_action_at is None:
        raise ValueError("follow_up requires next_action_at")
    if item.status == RelationshipStatus.MEETING_SCHEDULED and item.next_action_at is None:
        raise ValueError("meeting_scheduled requires next_action_at")


def upsert_relationship(
    session: Session,
    *,
    company_key: str,
    siren: Optional[str],
    company_name: str,
    item: CommercialRelationshipInput,
    relationship: Optional[CommercialRelationship] = None,
    now: Optional[datetime] = None,
) -> CommercialRelationship:
    validate_relationship_input(item)
    observed_at = _as_utc(now or datetime.now(timezone.utc))
    row = relationship or _find_model(session, company_key, siren)
    if row is None:
        row = CommercialRelationship(
            company_key=company_key,
            siren=siren,
            company_name_snapshot=company_name,
            status=item.status,
            is_active=True,
            created_at=observed_at,
            updated_at=observed_at,
        )
        session.add(row)
    row.company_key = company_key
    row.siren = siren
    row.company_name_snapshot = company_name
    row.status = item.status
    row.last_contact_at = _as_utc(item.last_contact_at) if item.last_contact_at else None
    row.next_action_at = _as_utc(item.next_action_at) if item.next_action_at else None
    row.note = _clean(item.note)
    row.outcome = _clean(item.outcome)
    row.contact_point_id = item.contact_point_id
    row.person_contact_id = item.person_contact_id
    row.used_channel = _clean(item.used_channel)
    row.is_active = True
    row.updated_at = observed_at
    _validate_contact_references(session, row)
    session.flush()
    _sync_hard_exclusion(session, row, observed_at)
    session.flush()
    return row


def reopen_opportunity(session: Session, row: CommercialRelationship, now: Optional[datetime] = None) -> None:
    if row.hard_exclusion_id is not None:
        exclusion = session.get(CommercialExclusion, row.hard_exclusion_id)
        if exclusion is not None:
            session.delete(exclusion)
        row.hard_exclusion_id = None
    row.is_active = False
    row.updated_at = _as_utc(now or datetime.now(timezone.utc))
    session.flush()


def _find_model(session: Session, company_key: str, siren: Optional[str]) -> Optional[CommercialRelationship]:
    if siren:
        row = session.scalar(select(CommercialRelationship).where(CommercialRelationship.siren == siren))
        if row is not None:
            return row
    return session.scalar(select(CommercialRelationship).where(CommercialRelationship.company_key == company_key))


def _sync_hard_exclusion(session: Session, row: CommercialRelationship, now: datetime) -> None:
    exclusion_type = HARD_EXCLUSION_TYPES.get(row.status)
    linked = session.get(CommercialExclusion, row.hard_exclusion_id) if row.hard_exclusion_id else None
    if exclusion_type is None:
        if linked is not None:
            session.delete(linked)
        row.hard_exclusion_id = None
        return
    if linked is None:
        linked = create_commercial_exclusion(session, CommercialExclusionInput(
            company_key=row.company_key,
            company_name_snapshot=row.company_name_snapshot,
            exclusion_type=exclusion_type,
            siren=row.siren,
            reason=HARD_EXCLUSION_REASONS[row.status],
            starts_at=now,
        ))
        row.hard_exclusion_id = linked.id
        return
    linked.company_key = row.company_key
    linked.siren = row.siren
    linked.company_name_snapshot = row.company_name_snapshot
    linked.exclusion_type = exclusion_type
    linked.reason = HARD_EXCLUSION_REASONS[row.status]
    linked.expires_at = None


def _validate_contact_references(session: Session, row: CommercialRelationship) -> None:
    if row.contact_point_id is not None:
        point = session.get(ContactPoint, row.contact_point_id)
        if point is None or point.company_key != row.company_key:
            raise ValueError("invalid contact point")
    if row.person_contact_id is not None:
        person = session.get(PersonContact, row.person_contact_id)
        if person is None or person.company_key != row.company_key:
            raise ValueError("invalid person contact")


def _clean(value: Optional[str]) -> Optional[str]:
    return value.strip() if value and value.strip() else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
