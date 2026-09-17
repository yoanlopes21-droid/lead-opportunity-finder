"""Small provider-neutral persistence service for sourced contactability facts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ContactEvidence, ContactPoint, PersonContact
from app.services.contactability.contracts import (
    ContactEvidenceCandidate,
    ContactEvidenceInput,
    ContactPointCandidate,
    ContactPointInput,
    ContactProviderResult,
    PersonContactCandidate,
    PersonContactInput,
    VALID_CONTACT_CONFIDENCES,
    VALID_CONTACT_SCOPES,
    VALID_CONTACT_TYPES,
    VALID_PERSON_RELEVANCE_ROLES,
    VALID_VERIFICATION_STATUSES,
    VerificationStatus,
)
from app.services.contactability.normalization import (
    contact_evidence_fingerprint,
    contact_point_fingerprint,
    normalize_contact_value,
    normalize_generic,
    normalize_person_name,
    person_contact_fingerprint,
)


def ensure_contactability_schema(engine: Engine) -> None:
    """Create only additive contactability tables when explicitly requested."""
    PersonContact.__table__.create(bind=engine, checkfirst=True)
    ContactPoint.__table__.create(bind=engine, checkfirst=True)
    ContactEvidence.__table__.create(bind=engine, checkfirst=True)


def persist_contact_provider_result(
    session: Session, result: ContactProviderResult,
) -> tuple[tuple[ContactPoint, ...], tuple[PersonContact, ...]]:
    """Persist generic, already-sourced provider candidates without raw payloads."""
    people = tuple(_upsert_person_candidate(session, item) for item in result.person_candidates)
    points = tuple(_upsert_point_candidate(session, item) for item in result.candidates)
    return points, people


def upsert_person_contact(session: Session, item: PersonContactInput) -> PersonContact:
    _validate_common(item.scope, item.company_key, item.organization_name_snapshot, item.local_key)
    _validate_choice(item.relevance_role, VALID_PERSON_RELEVANCE_ROLES, "relevance role")
    _validate_choice(item.confidence_level, VALID_CONTACT_CONFIDENCES, "confidence level")
    _validate_choice(item.verification_status, VALID_VERIFICATION_STATUSES, "verification status")
    normalized_name = normalize_person_name(item.full_name)
    if normalized_name is None:
        raise ValueError("full name must be meaningful")
    fingerprint = person_contact_fingerprint(
        company_key=item.company_key,
        scope=item.scope,
        local_key=item.local_key,
        normalized_name=normalized_name,
    )
    row = session.scalar(select(PersonContact).where(PersonContact.fingerprint == fingerprint))
    if row is None:
        row = PersonContact(
            company_key=item.company_key.strip(), scope=item.scope, local_key=_optional_text(item.local_key),
            siren=_optional_text(item.siren), organization_name_snapshot=item.organization_name_snapshot.strip(),
            local_commune_snapshot=_optional_text(item.local_commune_snapshot),
            local_location_label_snapshot=_optional_text(item.local_location_label_snapshot),
            full_name=item.full_name.strip(), normalized_name=normalized_name,
            job_title=_optional_text(item.job_title), relevance_role=item.relevance_role,
            confidence_level=item.confidence_level, verification_status=item.verification_status,
            attribution_reason=_optional_text(item.attribution_reason), fingerprint=fingerprint,
            first_observed_at=item.observed_at, last_observed_at=item.observed_at, is_active=True,
        )
        session.add(row)
        session.flush()
        return row
    _refresh_person(row, item, normalized_name)
    session.flush()
    return row


def upsert_contact_point(session: Session, item: ContactPointInput) -> ContactPoint:
    _validate_common(item.scope, item.company_key, item.organization_name_snapshot, item.local_key)
    _validate_choice(item.contact_type, VALID_CONTACT_TYPES, "contact type")
    _validate_choice(item.confidence_level, VALID_CONTACT_CONFIDENCES, "confidence level")
    _validate_choice(item.verification_status, VALID_VERIFICATION_STATUSES, "verification status")
    if item.person_contact_id is not None and session.get(PersonContact, item.person_contact_id) is None:
        raise ValueError("person contact does not exist")
    normalized_value = normalize_contact_value(item.contact_type, item.value)
    if normalized_value is None:
        raise ValueError("contact value is invalid for its type")
    fingerprint = contact_point_fingerprint(
        company_key=item.company_key, scope=item.scope, local_key=item.local_key,
        contact_type=item.contact_type, normalized_value=normalized_value,
        person_contact_id=item.person_contact_id,
    )
    row = session.scalar(select(ContactPoint).where(ContactPoint.fingerprint == fingerprint))
    if row is None:
        row = ContactPoint(
            company_key=item.company_key.strip(), scope=item.scope, local_key=_optional_text(item.local_key),
            siren=_optional_text(item.siren), organization_name_snapshot=item.organization_name_snapshot.strip(),
            local_commune_snapshot=_optional_text(item.local_commune_snapshot),
            local_location_label_snapshot=_optional_text(item.local_location_label_snapshot),
            contact_type=item.contact_type, value=item.value.strip(), normalized_value=normalized_value,
            confidence_level=item.confidence_level, verification_status=item.verification_status,
            attribution_reason=_optional_text(item.attribution_reason), person_contact_id=item.person_contact_id,
            fingerprint=fingerprint, first_observed_at=item.observed_at,
            last_observed_at=item.observed_at, is_active=True,
        )
        session.add(row)
        session.flush()
        return row
    _refresh_point(row, item, normalized_value)
    session.flush()
    return row


def add_contact_evidence(
    session: Session,
    item: ContactEvidenceInput,
    *,
    contact_point_id: Optional[int] = None,
    person_contact_id: Optional[int] = None,
) -> ContactEvidence:
    if (contact_point_id is None) == (person_contact_id is None):
        raise ValueError("evidence must target exactly one contact point or person contact")
    if contact_point_id is not None and session.get(ContactPoint, contact_point_id) is None:
        raise ValueError("contact point does not exist")
    if person_contact_id is not None and session.get(PersonContact, person_contact_id) is None:
        raise ValueError("person contact does not exist")
    _required_text(item.provider, "provider")
    _required_text(item.source_name, "source name")
    target_kind = "contact_point" if contact_point_id is not None else "person_contact"
    target_id = contact_point_id if contact_point_id is not None else person_contact_id
    fingerprint = contact_evidence_fingerprint(
        target_kind=target_kind, target_id=target_id, provider=item.provider,
        source_name=item.source_name, source_url=item.source_url,
        source_identifier=item.source_identifier, evidence_reason=item.evidence_reason,
        excerpt=item.excerpt,
    )
    row = session.scalar(select(ContactEvidence).where(ContactEvidence.fingerprint == fingerprint))
    if row is None:
        row = ContactEvidence(
            contact_point_id=contact_point_id, person_contact_id=person_contact_id,
            provider=item.provider.strip(), source_name=item.source_name.strip(),
            source_url=_optional_text(item.source_url), source_identifier=_optional_text(item.source_identifier),
            observed_at=item.observed_at, evidence_reason=_optional_text(item.evidence_reason),
            excerpt=_optional_text(item.excerpt), fingerprint=fingerprint,
        )
        session.add(row)
    elif _as_utc(item.observed_at) > _as_utc(row.observed_at):
        row.observed_at = item.observed_at
    session.flush()
    return row


def set_contact_point_active(
    session: Session, contact_point_id: int, *, is_active: bool,
    verification_status: Optional[str] = None,
) -> ContactPoint:
    row = session.get(ContactPoint, contact_point_id)
    if row is None:
        raise ValueError("contact point does not exist")
    if verification_status is not None:
        _validate_choice(verification_status, VALID_VERIFICATION_STATUSES, "verification status")
        row.verification_status = verification_status
    row.is_active = is_active
    session.flush()
    return row


def mark_contact_point_stale(session: Session, contact_point_id: int) -> ContactPoint:
    return set_contact_point_active(
        session, contact_point_id, is_active=False,
        verification_status=VerificationStatus.STALE,
    )


def set_person_contact_active(
    session: Session, person_contact_id: int, *, is_active: bool,
    verification_status: Optional[str] = None,
) -> PersonContact:
    row = session.get(PersonContact, person_contact_id)
    if row is None:
        raise ValueError("person contact does not exist")
    if verification_status is not None:
        _validate_choice(verification_status, VALID_VERIFICATION_STATUSES, "verification status")
        row.verification_status = verification_status
    row.is_active = is_active
    session.flush()
    return row


def mark_person_contact_stale(session: Session, person_contact_id: int) -> PersonContact:
    return set_person_contact_active(
        session, person_contact_id, is_active=False,
        verification_status=VerificationStatus.STALE,
    )


def list_contact_points(
    session: Session, company_key: str, *, scope: Optional[str] = None,
    local_key: Optional[str] = None, person_contact_id: Optional[int] = None,
    include_inactive: bool = False,
) -> tuple[ContactPoint, ...]:
    statement = select(ContactPoint).where(ContactPoint.company_key == company_key)
    if scope is not None:
        statement = statement.where(ContactPoint.scope == scope)
    if local_key is not None:
        statement = statement.where(ContactPoint.local_key == local_key)
    if person_contact_id is not None:
        statement = statement.where(ContactPoint.person_contact_id == person_contact_id)
    if not include_inactive:
        statement = statement.where(ContactPoint.is_active.is_(True))
    return tuple(session.scalars(statement.order_by(ContactPoint.contact_type, ContactPoint.normalized_value)))


def list_person_contacts(
    session: Session, company_key: str, *, scope: Optional[str] = None,
    local_key: Optional[str] = None, include_inactive: bool = False,
) -> tuple[PersonContact, ...]:
    statement = select(PersonContact).where(PersonContact.company_key == company_key)
    if scope is not None:
        statement = statement.where(PersonContact.scope == scope)
    if local_key is not None:
        statement = statement.where(PersonContact.local_key == local_key)
    if not include_inactive:
        statement = statement.where(PersonContact.is_active.is_(True))
    return tuple(session.scalars(statement.order_by(PersonContact.normalized_name)))


def _refresh_person(row: PersonContact, item: PersonContactInput, normalized_name: str) -> None:
    row.siren = _optional_text(item.siren)
    row.organization_name_snapshot = item.organization_name_snapshot.strip()
    row.local_commune_snapshot = _optional_text(item.local_commune_snapshot)
    row.local_location_label_snapshot = _optional_text(item.local_location_label_snapshot)
    row.full_name = item.full_name.strip()
    row.normalized_name = normalized_name
    row.job_title = _optional_text(item.job_title)
    row.relevance_role = item.relevance_role
    row.confidence_level = item.confidence_level
    row.verification_status = item.verification_status
    row.attribution_reason = _optional_text(item.attribution_reason)
    row.last_observed_at = _latest_datetime(row.last_observed_at, item.observed_at)
    row.is_active = True


def _refresh_point(row: ContactPoint, item: ContactPointInput, normalized_value: str) -> None:
    row.siren = _optional_text(item.siren)
    row.organization_name_snapshot = item.organization_name_snapshot.strip()
    row.local_commune_snapshot = _optional_text(item.local_commune_snapshot)
    row.local_location_label_snapshot = _optional_text(item.local_location_label_snapshot)
    row.value = item.value.strip()
    row.normalized_value = normalized_value
    row.confidence_level = item.confidence_level
    row.verification_status = item.verification_status
    row.attribution_reason = _optional_text(item.attribution_reason)
    row.last_observed_at = _latest_datetime(row.last_observed_at, item.observed_at)
    row.is_active = True


def _validate_common(scope: str, company_key: str, organization_name: str, local_key: Optional[str]) -> None:
    _validate_choice(scope, VALID_CONTACT_SCOPES, "scope")
    _required_text(company_key, "company key")
    _required_text(organization_name, "organization name snapshot")
    if scope == "local" and not _optional_text(local_key):
        raise ValueError("local scope requires local key")


def _validate_choice(value: str, choices: set[str], label: str) -> None:
    if value not in choices:
        raise ValueError(f"invalid {label}")


def _required_text(value: Optional[str], label: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError(f"{label} must be meaningful")
    return normalized


def _optional_text(value: Optional[str]) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _latest_datetime(current: datetime, candidate: datetime) -> datetime:
    return candidate if _as_utc(candidate) > _as_utc(current) else current


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _upsert_person_candidate(session: Session, item: PersonContactCandidate) -> PersonContact:
    observed_at = max(evidence.observed_at for evidence in item.evidence)
    row = upsert_person_contact(session, PersonContactInput(
        company_key=item.company_key,
        organization_name_snapshot=item.organization_name_snapshot,
        scope=item.scope,
        full_name=item.full_name,
        relevance_role=item.relevance_role,
        confidence_level=item.confidence_level,
        verification_status=item.verification_status,
        observed_at=observed_at,
        local_key=item.local_key,
        siren=item.siren,
        local_commune_snapshot=item.local_commune_snapshot,
        local_location_label_snapshot=item.local_location_label_snapshot,
        job_title=item.job_title,
        attribution_reason=item.attribution_reason,
    ))
    _add_candidate_evidence(session, item.evidence, person_contact_id=row.id)
    return row


def _upsert_point_candidate(session: Session, item: ContactPointCandidate) -> ContactPoint:
    observed_at = max(evidence.observed_at for evidence in item.evidence)
    row = upsert_contact_point(session, ContactPointInput(
        company_key=item.company_key,
        organization_name_snapshot=item.organization_name_snapshot,
        scope=item.scope,
        contact_type=item.contact_type,
        value=item.value,
        confidence_level=item.confidence_level,
        verification_status=item.verification_status,
        observed_at=observed_at,
        local_key=item.local_key,
        siren=item.siren,
        local_commune_snapshot=item.local_commune_snapshot,
        local_location_label_snapshot=item.local_location_label_snapshot,
        attribution_reason=item.attribution_reason,
    ))
    _add_candidate_evidence(session, item.evidence, contact_point_id=row.id)
    return row


def _add_candidate_evidence(
    session: Session, evidence_items: tuple[ContactEvidenceCandidate, ...],
    *, contact_point_id: Optional[int] = None, person_contact_id: Optional[int] = None,
) -> None:
    for evidence in evidence_items:
        add_contact_evidence(session, ContactEvidenceInput(
            provider=evidence.provider,
            source_name=evidence.source_name,
            observed_at=evidence.observed_at,
            source_url=evidence.source_url,
            source_identifier=evidence.source_identifier,
            evidence_reason=evidence.evidence_reason,
            excerpt=evidence.excerpt,
        ), contact_point_id=contact_point_id, person_contact_id=person_contact_id)
