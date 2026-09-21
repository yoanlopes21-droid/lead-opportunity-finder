"""Pure, explainable selection of a commercial contact strategy.

This module only reads already sourced contactability facts.  It deliberately
does not invoke providers (including Societe.com) and never manufactures a
contact method from a name or a domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContactEvidence, ContactPoint, PersonContact
from app.services.contactability.contracts import (
    ContactConfidence, ContactScope, ContactTarget, ContactType, PersonRelevanceRole,
    VerificationStatus,
)


class StrategyTargetType:
    HR = "hr"
    RECRUITMENT = "recruitment"
    DIRECTOR = "director"
    MANAGER = "manager"
    COMPANY_GENERAL = "company_general"
    INTERMEDIARY = "intermediary"
    UNRESOLVED = "unresolved"


class PreferredChannel:
    DIRECT_EMAIL = "direct_email"
    FUNCTIONAL_EMAIL = "functional_email"
    DIRECT_PHONE = "direct_phone"
    SERVICE_PHONE = "service_phone"
    COMPANY_SWITCHBOARD = "company_switchboard"
    CONTACT_PAGE = "contact_page"
    PROFESSIONAL_URL = "professional_url"
    NONE = "none"


@dataclass(frozen=True)
class StrategyEvidenceReference:
    """A compact provenance reference fit for a future UI popover."""

    evidence_id: int
    provider: str
    source_name: str
    source_url: Optional[str]
    evidence_reason: Optional[str]


@dataclass(frozen=True)
class ContactStrategy:
    target_type: str
    person_contact_id: Optional[int]
    contact_point_id: Optional[int]
    preferred_channel: str
    fallback_channels: tuple[str, ...]
    confidence: str
    rationale_codes: tuple[str, ...]
    short_context: str
    warnings: tuple[str, ...]
    missing_information: tuple[str, ...]
    evidence_references: tuple[StrategyEvidenceReference, ...]
    scope: str
    local_key: Optional[str]


_ROLE_PRIORITY = {
    PersonRelevanceRole.HR: 0,
    PersonRelevanceRole.RECRUITMENT: 1,
    PersonRelevanceRole.DIRECTOR: 2,
    PersonRelevanceRole.MANAGER: 3,
    PersonRelevanceRole.OTHER: 4,
}
_CONFIDENCE_PRIORITY = {
    ContactConfidence.CONFIRMED: 0,
    ContactConfidence.HIGH_CONFIDENCE: 1,
    ContactConfidence.REVIEW_NEEDED: 2,
    ContactConfidence.AMBIGUOUS: 3,
}
_VERIFICATION_PRIORITY = {
    VerificationStatus.MANUALLY_VERIFIED: 0,
    VerificationStatus.SOURCE_VERIFIED: 1,
    VerificationStatus.UNVERIFIED: 2,
    VerificationStatus.STALE: 3,
    VerificationStatus.REJECTED: 4,
}
_FUNCTIONAL_EMAIL_MARKERS = ("rh", "recrut", "recruit", "talent", "jobs", "emploi", "career")
_SERVICE_PHONE_MARKERS = ("rh", "recrut", "recruit", "talent", "jobs", "emploi", "service")


def recommend_contact_strategy(
    session: Session,
    target: ContactTarget,
    *,
    employee_range: Optional[str] = None,
    recruitment_context: Sequence[str] = (),
) -> ContactStrategy:
    """Read persisted facts for exactly *target* and return a pure recommendation.

    Company facts are never used for a local target.  This is important because
    a company contact does not establish that the person manages every site.
    ``employee_range`` is deliberately explanatory only: it can add context but
    cannot select a target by itself.
    """
    people = _people_for_target(session, target)
    points = _points_for_target(session, target)
    return build_contact_strategy(
        target, people, points, employee_range=employee_range,
        recruitment_context=recruitment_context,
        evidence_references=_evidence_for(session, people, points),
    )


def build_contact_strategy(
    target: ContactTarget,
    person_contacts: Iterable[PersonContact],
    contact_points: Iterable[ContactPoint],
    *,
    employee_range: Optional[str] = None,
    recruitment_context: Sequence[str] = (),
    evidence_references: Iterable[StrategyEvidenceReference] = (),
) -> ContactStrategy:
    """Select a recommendation from supplied sourced facts without I/O.

    The iterable-based API makes the business rules directly unit-testable and
    prevents this layer from coupling strategy calculation to persistence.
    """
    people = tuple(sorted((item for item in person_contacts if item.is_active and item.verification_status != VerificationStatus.REJECTED), key=_person_sort_key))
    supplied_points = tuple(item for item in contact_points if item.is_active)
    # A review-needed coordinate remains visible to callers, but is not silently
    # promoted into a reliable recommended channel. Rejected data is never used.
    withheld_points = tuple(item for item in supplied_points if item.verification_status in {
        VerificationStatus.REJECTED, VerificationStatus.STALE
    } or item.confidence_level in {ContactConfidence.REVIEW_NEEDED, ContactConfidence.AMBIGUOUS})
    points = tuple(sorted((item for item in supplied_points if item not in withheld_points), key=_point_sort_key))
    warnings = list(target.warnings)
    if withheld_points:
        warnings.append("Certaines coordonnées sourcées exigent vérification et ne sont pas recommandées comme canal fiable.")
    if target.scope == ContactScope.LOCAL and target.local_key is None:
        warnings.append("Portée locale sans identifiant : recommandation non fiable.")

    if target.scope == ContactScope.INTERMEDIARY:
        return _intermediary_strategy(target, people, points, warnings, evidence_references)

    selected = _select_person(target, people)
    functional_hr = _best_functional_email(points)
    role = selected.relevance_role if selected else None
    has_hr = any(item.relevance_role in {PersonRelevanceRole.HR, PersonRelevanceRole.RECRUITMENT} for item in people)
    if functional_hr is not None:
        has_hr = True

    target_type = _target_type_for(target, selected, points, has_hr)
    person_points = tuple(point for point in points if selected and point.person_contact_id == selected.id)
    preferred = _preferred_point(target_type, selected, person_points, points, functional_hr)
    channel = _channel_for(preferred, selected, functional_hr)
    fallbacks = _fallback_channels(target_type, selected, person_points, points, preferred, functional_hr)
    confidence = _strategy_confidence(selected, preferred, functional_hr)
    rationale = _rationale(target, target_type, selected, has_hr, preferred, employee_range, recruitment_context)
    missing = _missing(target_type, selected, channel, has_hr)
    if selected and channel == PreferredChannel.NONE:
        warnings.append("Personne identifiée, mais aucun canal professionnel public n'est relié à elle.")
    if selected and selected.verification_status == VerificationStatus.UNVERIFIED:
        warnings.append("Le rôle identifié n'est pas vérifié par la source.")
    if preferred and preferred.verification_status in {VerificationStatus.UNVERIFIED, VerificationStatus.STALE}:
        warnings.append("Le canal recommandé demande vérification avant usage.")

    return ContactStrategy(
        target_type=target_type,
        person_contact_id=selected.id if selected else None,
        contact_point_id=preferred.id if preferred else None,
        preferred_channel=channel,
        fallback_channels=fallbacks,
        confidence=confidence,
        rationale_codes=tuple(rationale),
        short_context=_short_context(target_type, selected, channel, has_hr),
        warnings=tuple(_unique(warnings)),
        missing_information=tuple(missing),
        evidence_references=tuple(sorted(evidence_references, key=lambda item: item.evidence_id)),
        scope=target.scope,
        local_key=target.local_key,
    )


def _people_for_target(session: Session, target: ContactTarget) -> tuple[PersonContact, ...]:
    stmt = select(PersonContact).where(PersonContact.company_key == target.company_key, PersonContact.scope == target.scope, PersonContact.is_active.is_(True))
    if target.scope == ContactScope.LOCAL:
        stmt = stmt.where(PersonContact.local_key == target.local_key)
    return tuple(session.scalars(stmt))


def _points_for_target(session: Session, target: ContactTarget) -> tuple[ContactPoint, ...]:
    stmt = select(ContactPoint).where(ContactPoint.company_key == target.company_key, ContactPoint.scope == target.scope, ContactPoint.is_active.is_(True))
    if target.scope == ContactScope.LOCAL:
        stmt = stmt.where(ContactPoint.local_key == target.local_key)
    return tuple(session.scalars(stmt))


def _evidence_for(session: Session, people: Sequence[PersonContact], points: Sequence[ContactPoint]) -> tuple[StrategyEvidenceReference, ...]:
    ids = {item.id for item in people}
    point_ids = {item.id for item in points}
    if not ids and not point_ids:
        return ()
    clauses = []
    if ids:
        clauses.append(ContactEvidence.person_contact_id.in_(ids))
    if point_ids:
        clauses.append(ContactEvidence.contact_point_id.in_(point_ids))
    from sqlalchemy import or_
    rows = session.scalars(select(ContactEvidence).where(or_(*clauses)).order_by(ContactEvidence.id))
    return tuple(StrategyEvidenceReference(row.id, row.provider, row.source_name, row.source_url, row.evidence_reason) for row in rows)


def _select_person(target: ContactTarget, people: Sequence[PersonContact]) -> Optional[PersonContact]:
    allowed = people
    if target.scope == ContactScope.LOCAL:
        # Only a specifically local contact can be selected; see the repository query too.
        allowed = tuple(item for item in people if item.local_key == target.local_key)
    if target.scope == ContactScope.LOCAL:
        managers = tuple(item for item in allowed if item.relevance_role == PersonRelevanceRole.MANAGER)
        return managers[0] if managers else None
    preferred = tuple(item for item in allowed if item.relevance_role != PersonRelevanceRole.OTHER)
    return preferred[0] if preferred else None


def _target_type_for(target: ContactTarget, person: Optional[PersonContact], points: Sequence[ContactPoint], has_hr: bool) -> str:
    if target.scope == ContactScope.LOCAL:
        return StrategyTargetType.MANAGER if person else (StrategyTargetType.COMPANY_GENERAL if points else StrategyTargetType.UNRESOLVED)
    if person:
        return {
            PersonRelevanceRole.HR: StrategyTargetType.HR,
            PersonRelevanceRole.RECRUITMENT: StrategyTargetType.RECRUITMENT,
            PersonRelevanceRole.DIRECTOR: StrategyTargetType.DIRECTOR,
            PersonRelevanceRole.MANAGER: StrategyTargetType.MANAGER,
        }.get(person.relevance_role, StrategyTargetType.COMPANY_GENERAL)
    if has_hr:
        return StrategyTargetType.RECRUITMENT
    return StrategyTargetType.COMPANY_GENERAL if points else StrategyTargetType.UNRESOLVED


def _preferred_point(target_type: str, person: Optional[PersonContact], person_points: Sequence[ContactPoint], points: Sequence[ContactPoint], functional_hr: Optional[ContactPoint]) -> Optional[ContactPoint]:
    direct_email = _first_of_type(person_points, ContactType.EMAIL)
    if direct_email:
        return direct_email
    # A known HR/recruitment path is more relevant than an unrelated executive channel.
    if target_type in {StrategyTargetType.HR, StrategyTargetType.RECRUITMENT} and functional_hr:
        return functional_hr
    direct_phone = _first_of_type(person_points, ContactType.PHONE)
    if direct_phone:
        return direct_phone
    service_phone = _best_service_phone(points)
    if service_phone:
        return service_phone
    switchboard = _first_of_type(points, ContactType.PHONE)
    if switchboard:
        return switchboard
    # A generic public mailbox is still a usable company channel, but never
    # displaces a known HR/recruitment route for a named HR contact.
    if target_type == StrategyTargetType.COMPANY_GENERAL:
        generic_email = _first_of_type(points, ContactType.EMAIL)
        if generic_email:
            return generic_email
    contact_page = _best_contact_page(points)
    if contact_page:
        return contact_page
    return _first_of_type(points, ContactType.PROFESSIONAL_URL)


def _channel_for(point: Optional[ContactPoint], person: Optional[PersonContact], functional_hr: Optional[ContactPoint]) -> str:
    if point is None:
        return PreferredChannel.NONE
    if point.contact_type == ContactType.EMAIL:
        return PreferredChannel.DIRECT_EMAIL if person and point.person_contact_id == person.id else PreferredChannel.FUNCTIONAL_EMAIL
    if point.contact_type == ContactType.PHONE:
        if person and point.person_contact_id == person.id:
            return PreferredChannel.DIRECT_PHONE
        return PreferredChannel.SERVICE_PHONE if point is not None and _is_service_phone(point) else PreferredChannel.COMPANY_SWITCHBOARD
    if point.contact_type == ContactType.WEBSITE:
        return PreferredChannel.CONTACT_PAGE
    if point.contact_type == ContactType.PROFESSIONAL_URL:
        return PreferredChannel.CONTACT_PAGE if _is_contact_page(point) else PreferredChannel.PROFESSIONAL_URL
    return PreferredChannel.NONE


def _fallback_channels(target_type: str, person: Optional[PersonContact], person_points: Sequence[ContactPoint], points: Sequence[ContactPoint], preferred: Optional[ContactPoint], functional_hr: Optional[ContactPoint]) -> tuple[str, ...]:
    candidates = list(person_points)
    if target_type in {StrategyTargetType.HR, StrategyTargetType.RECRUITMENT} and functional_hr:
        candidates.append(functional_hr)
    candidates.extend(points)
    channels = [_channel_for(point, person, functional_hr) for point in sorted(candidates, key=_point_sort_key) if point.id != (preferred.id if preferred else None)]
    return tuple(_unique(channel for channel in channels if channel != PreferredChannel.NONE))


def _intermediary_strategy(target: ContactTarget, people: Sequence[PersonContact], points: Sequence[ContactPoint], warnings: list[str], evidence: Iterable[StrategyEvidenceReference]) -> ContactStrategy:
    point = _first_of_type(points, ContactType.EMAIL) or _first_of_type(points, ContactType.PHONE) or _best_contact_page(points) or _first_of_type(points, ContactType.PROFESSIONAL_URL)
    channel = _channel_for(point, None, None)
    return ContactStrategy(StrategyTargetType.INTERMEDIARY, None, point.id if point else None, channel, _fallback_channels(StrategyTargetType.INTERMEDIARY, None, (), points, point, None), _strategy_confidence(None, point, None), ("intermediary_scope_only",), "Intermédiaire identifié : contacter uniquement le cabinet ou diffuseur, jamais un client final supposé.", tuple(_unique(warnings)), ("interlocuteur nominatif de l'intermédiaire" if not people else "",) if not people else (), tuple(sorted(evidence, key=lambda item: item.evidence_id)), target.scope, target.local_key)


def _rationale(target: ContactTarget, target_type: str, person: Optional[PersonContact], has_hr: bool, point: Optional[ContactPoint], employee_range: Optional[str], recruitment_context: Sequence[str]) -> list[str]:
    result = [f"scope_{target.scope}"]
    if target_type in {StrategyTargetType.HR, StrategyTargetType.RECRUITMENT}:
        result.append("hr_or_recruitment_priority")
    elif target_type == StrategyTargetType.DIRECTOR:
        result.append("director_selected_without_hr_or_recruitment_contact")
    elif target_type == StrategyTargetType.MANAGER:
        result.append("explicit_local_manager" if target.scope == ContactScope.LOCAL else "manager_selected")
    elif target_type == StrategyTargetType.UNRESOLVED:
        result.append("no_exploitable_contact")
    if point:
        result.append(f"channel_{_channel_for(point, person, None)}")
    if employee_range:
        result.append("employee_range_context_only")
    if recruitment_context:
        result.append("recruitment_context_available")
    return result


def _short_context(target_type: str, person: Optional[PersonContact], channel: str, has_hr: bool) -> str:
    if target_type in {StrategyTargetType.HR, StrategyTargetType.RECRUITMENT} and person and channel == PreferredChannel.COMPANY_SWITCHBOARD:
        return "Responsable RH identifié, aucun canal direct public trouvé. Standard disponible : demander le service Ressources Humaines."
    if target_type in {StrategyTargetType.HR, StrategyTargetType.RECRUITMENT} and not person:
        return "Service recrutement identifié via des coordonnées sourcées ; aucune personne nominative vérifiée."
    if target_type == StrategyTargetType.DIRECTOR:
        return "Aucune fonction RH ou recrutement détectée ; le dirigeant est l'interlocuteur professionnel le plus pertinent actuellement identifié."
    if target_type == StrategyTargetType.MANAGER:
        return "Manager local retenu car son rattachement à cette opportunité locale est explicitement sourcé."
    if target_type == StrategyTargetType.INTERMEDIARY:
        return "Intermédiaire identifié : la stratégie ne vise pas les employeurs finaux supposés."
    if target_type == StrategyTargetType.UNRESOLVED:
        return "Aucun interlocuteur ou canal professionnel exploitable n'est actuellement sourcé."
    return "Coordonnée professionnelle sourcée disponible ; interlocuteur nominatif non confirmé."


def _missing(target_type: str, person: Optional[PersonContact], channel: str, has_hr: bool) -> list[str]:
    missing = []
    if person is None and target_type not in {StrategyTargetType.INTERMEDIARY, StrategyTargetType.UNRESOLVED}:
        missing.append("interlocuteur nominatif")
    if channel == PreferredChannel.NONE:
        missing.append("canal professionnel public")
    if not has_hr and target_type == StrategyTargetType.DIRECTOR:
        missing.append("confirmation d'une fonction RH ou recrutement")
    return missing


def _best_functional_email(points: Sequence[ContactPoint]) -> Optional[ContactPoint]:
    return next((item for item in points if item.contact_type == ContactType.EMAIL and _is_functional_hr_email(item)), None)


def _best_service_phone(points: Sequence[ContactPoint]) -> Optional[ContactPoint]:
    return next((item for item in points if item.contact_type == ContactType.PHONE and _is_service_phone(item)), None)


def _best_contact_page(points: Sequence[ContactPoint]) -> Optional[ContactPoint]:
    return next((item for item in points if _is_contact_page(item)), None)


def _is_contact_page(point: ContactPoint) -> bool:
    return point.contact_type in {ContactType.WEBSITE, ContactType.PROFESSIONAL_URL} and any(
        marker in point.value.casefold() for marker in ("contact", "nous-contacter", "recrut", "career", "emploi", "rh")
    )


def _is_functional_hr_email(point: ContactPoint) -> bool:
    local_part = point.value.partition("@")[0].casefold()
    return any(marker in local_part for marker in _FUNCTIONAL_EMAIL_MARKERS)


def _is_service_phone(point: ContactPoint) -> bool:
    text = (point.attribution_reason or "").casefold()
    return any(marker in text for marker in _SERVICE_PHONE_MARKERS)


def _first_of_type(points: Sequence[ContactPoint], contact_type: str) -> Optional[ContactPoint]:
    return next((item for item in points if item.contact_type == contact_type), None)


def _person_sort_key(item: PersonContact) -> tuple:
    return (_ROLE_PRIORITY.get(item.relevance_role, 99), _CONFIDENCE_PRIORITY.get(item.confidence_level, 99), _VERIFICATION_PRIORITY.get(item.verification_status, 99), item.normalized_name, item.id)


def _point_sort_key(item: ContactPoint) -> tuple:
    return (_CONFIDENCE_PRIORITY.get(item.confidence_level, 99), _VERIFICATION_PRIORITY.get(item.verification_status, 99), item.contact_type, item.normalized_value, item.id)


def _strategy_confidence(person: Optional[PersonContact], point: Optional[ContactPoint], functional: Optional[ContactPoint]) -> str:
    values = [item.confidence_level for item in (person, point) if item is not None]
    return min(values, key=lambda value: _CONFIDENCE_PRIORITY.get(value, 99)) if values else ContactConfidence.AMBIGUOUS


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
