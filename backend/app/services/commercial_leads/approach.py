"""Read-only, explainable inputs for a future commercial approach composer.

This layer interprets existing leads; it never generates client-facing copy or
changes scoring, source observations, contact facts, or tracking records.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import CommercialRelationship, ObservedJobOffer
from app.services.commercial_leads.service import CommercialLead, get_commercial_lead
from app.services.contactability.contracts import (
    ContactConfidence, ContactScope, ContactTarget, ContactType, VerificationStatus,
)
from app.services.contactability.relevance import ChannelRelevance, assess_channel_relevance
from app.services.contactability.strategy import PreferredChannel, build_contact_strategy
from app.services.opportunities.company import ActiveJobOffer


class ApproachReadiness:
    READY_TO_CONTACT = "ready_to_contact"
    ROUTING_REQUIRED = "routing_required"
    CHANNEL_MISSING = "channel_missing"
    VERIFY_CONTACT = "verify_contact"
    VERIFY_OFFER = "verify_offer"
    VERIFY_EMPLOYER = "verify_employer"
    INTERMEDIARY_NOT_EMPLOYER = "intermediary_not_employer"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class ApproachFact:
    code: str
    value: str
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApproachOffer:
    offer_id: str
    source: str
    title: str
    location: Optional[str]
    local_key: str
    published_at: Optional[str]
    first_seen_at: datetime
    last_seen_at: datetime
    age_days: Optional[int]
    source_offer_ids: tuple[str, ...]
    source_urls: tuple[str, ...]
    source_observation_count: int
    collection_observation_count: int
    description_excerpt: Optional[str]
    selection_reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ApproachContact:
    id: int
    type: str
    value: str
    scope: str
    local_key: Optional[str]
    reach: str
    person_contact_id: Optional[int]
    role: str
    use: str
    confidence: str
    verification_status: str
    commercial_relevance: str
    reason_codes: tuple[str, ...]
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class ApproachHistory:
    id: int
    status: str
    is_active: bool
    last_contact_at: Optional[datetime]
    next_action_at: Optional[datetime]


@dataclass(frozen=True)
class CommercialApproachContext:
    company_key: str
    company_name: str
    official_name: Optional[str]
    siren: Optional[str]
    siret: Optional[str]
    identity_match_status: Optional[str]
    identity_source_url: Optional[str]
    employer_relationship_status: str
    employer_attribution: str
    employer_reasons: tuple[str, ...]
    entry_offer: ApproachOffer
    canonical_need_count: int
    source_listing_count: int
    contacts: tuple[ApproachContact, ...]
    recommended_contact_id: Optional[int]
    recommended_channel: str
    recommended_person_contact_id: Optional[int]
    contact_strategy_target: str
    contact_strategy_reasons: tuple[str, ...]
    active_relationship_status: Optional[str]
    relationship_history: tuple[ApproachHistory, ...]
    exclusion_type: Optional[str]
    readiness: str
    approach_preparable: bool
    contact_now_possible: bool
    verification_required: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    missing_information: tuple[str, ...]
    usable_facts: tuple[ApproachFact, ...]
    prohibited_claims: tuple[str, ...]


def get_commercial_approach_context(
    session: Session, company_key: str, department_code: str = "94",
    now: Optional[datetime] = None,
) -> Optional[CommercialApproachContext]:
    """Read one existing lead and its minimal supporting records without I/O."""
    observed_at = now or datetime.now(timezone.utc)
    lead = get_commercial_lead(session, company_key, department_code, observed_at)
    if lead is None or not lead.active_job_offers:
        return None
    descriptions = _offer_descriptions(session, lead)
    history = _relationship_history(session, lead)
    return build_commercial_approach_context(
        lead, descriptions=descriptions, history=history, now=observed_at,
    )


def build_commercial_approach_context(
    lead: CommercialLead, *,
    descriptions: Optional[dict[str, str]] = None,
    history: tuple[ApproachHistory, ...] = (),
    now: Optional[datetime] = None,
) -> CommercialApproachContext:
    """Pure composition from an already assembled lead and optional read facts."""
    if not lead.active_job_offers:
        raise ValueError("an approach context requires an active canonical need")
    observed_at = _as_utc(now or datetime.now(timezone.utc))
    descriptions = descriptions or {}
    entry, selection_reasons = _select_entry_offer(lead.active_job_offers, lead.company_name, descriptions)
    description = descriptions.get(_offer_key(entry))
    entry_warnings = _offer_warnings(entry, description, observed_at)
    attribution, employer_reasons = _employer_attribution(lead, descriptions)
    contacts, preferred_id, preferred_channel, preferred_person_id, target_type, contact_reasons = _contacts_for_entry(lead, entry)
    preferred = next((item for item in contacts if item.id == preferred_id), None)
    exclusion = lead.exclusion
    active_status = lead.commercial_relationship.status if lead.commercial_relationship else None
    suspended = bool(exclusion) or active_status in {"client", "do_not_contact"}

    if suspended:
        readiness = ApproachReadiness.SUSPENDED
    elif attribution == "intermediary_confirmed":
        readiness = ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER
    elif attribution in {"intermediary_suspected", "attribution_ambiguous"}:
        readiness = ApproachReadiness.VERIFY_EMPLOYER
    elif "last_observation_over_7_days" in entry_warnings:
        readiness = ApproachReadiness.VERIFY_OFFER
    elif preferred is not None:
        readiness = (
            ApproachReadiness.ROUTING_REQUIRED if preferred.role == "general_routing"
            else ApproachReadiness.READY_TO_CONTACT
        )
    elif any(item.use == "verify" for item in contacts):
        readiness = ApproachReadiness.VERIFY_CONTACT
    else:
        readiness = ApproachReadiness.CHANNEL_MISSING

    if readiness in {
        ApproachReadiness.SUSPENDED, ApproachReadiness.VERIFY_EMPLOYER,
        ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER, ApproachReadiness.VERIFY_OFFER,
    }:
        preferred_id, preferred_person_id, preferred_channel = None, None, PreferredChannel.NONE

    reasons = (*employer_reasons, *selection_reasons, f"readiness:{readiness}")
    warnings = list(entry_warnings)
    if lead.identity_match_status != "matched_high_confidence":
        warnings.append("legal_identity_unconfirmed")
    if any(item.use == "rejected" for item in contacts):
        warnings.append("some_contacts_not_for_recruitment_outreach")
    if history and any(not item.is_active for item in history):
        warnings.append("inactive_relationship_history_exists")
    if attribution != "direct_employer_plausible":
        warnings.append("employer_attribution_not_confirmed")
    if lead.contact_strategy:
        warnings.extend(lead.contact_strategy.warnings)

    missing = []
    if preferred is None:
        missing.append("verified_professional_channel")
    if entry.display_location is None:
        missing.append("job_location")
    if not lead.contactability.people:
        missing.append("named_contact")
    if lead.siren is None:
        missing.append("confirmed_legal_identity")
    if attribution in {"attribution_ambiguous", "intermediary_suspected"}:
        missing.append("employer_attribution_confirmation")

    facts = [
        ApproachFact("observed_company_name", lead.company_name, entry.source_urls),
        ApproachFact("observed_job_title", entry.title, entry.source_urls),
    ]
    if entry.display_location:
        facts.append(ApproachFact("observed_job_location", entry.display_location, entry.source_urls))
    if lead.official_name:
        facts.append(ApproachFact("confirmed_legal_name", lead.official_name,
                                  (lead.identity_source_url,) if lead.identity_source_url else ()))
    if lead.siren:
        facts.append(ApproachFact("confirmed_siren", lead.siren,
                                  (lead.identity_source_url,) if lead.identity_source_url else ()))
    if len(lead.active_job_offers) > 1:
        facts.append(ApproachFact("multiple_canonical_needs_observed", str(len(lead.active_job_offers))))

    prohibited = ["unproven_recruitment_difficulty", "unproven_urgency", "unproven_vacancy_count"]
    if attribution != "direct_employer_plausible":
        prohibited.append("direct_employer_claim")
    if preferred is None:
        prohibited.append("known_contact_channel")
    if preferred and preferred.role == "general_routing":
        prohibited.append("confirmed_hr_recipient")
    if entry.display_location is None:
        prohibited.append("job_location_claim")
    if "last_observation_over_7_days" in entry_warnings:
        prohibited.append("currently_open_job_claim")
    if history or lead.commercial_relationship:
        prohibited.append("first_contact_claim_without_review")
    return CommercialApproachContext(
        company_key=lead.company_key, company_name=lead.company_name,
        official_name=lead.official_name, siren=lead.siren, siret=lead.siret,
        identity_match_status=lead.identity_match_status,
        identity_source_url=lead.identity_source_url,
        employer_relationship_status=lead.scoring.employer_relationship_status,
        employer_attribution=attribution, employer_reasons=employer_reasons,
        entry_offer=ApproachOffer(
            offer_id=entry.offer_id, source=entry.source, title=entry.title,
            location=entry.display_location, local_key=entry.local_key,
            published_at=entry.published_at, first_seen_at=entry.first_seen_at,
            last_seen_at=entry.last_seen_at, age_days=entry.age_days,
            source_offer_ids=entry.source_offer_ids, source_urls=entry.source_urls,
            source_observation_count=len(entry.evidence),
            collection_observation_count=entry.observation_count,
            description_excerpt=_excerpt(description),
            selection_reasons=selection_reasons, warnings=entry_warnings,
        ),
        canonical_need_count=len(lead.active_job_offers),
        source_listing_count=sum(len(item.evidence) for item in lead.active_job_offers),
        contacts=contacts, recommended_contact_id=preferred_id,
        recommended_channel=preferred_channel, recommended_person_contact_id=preferred_person_id,
        contact_strategy_target=target_type, contact_strategy_reasons=contact_reasons,
        active_relationship_status=active_status, relationship_history=history,
        exclusion_type=exclusion.exclusion_type if exclusion else None,
        readiness=readiness, approach_preparable=readiness not in {
            ApproachReadiness.SUSPENDED, ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER,
        },
        contact_now_possible=readiness in {
            ApproachReadiness.READY_TO_CONTACT, ApproachReadiness.ROUTING_REQUIRED,
        },
        verification_required=readiness in {
            ApproachReadiness.VERIFY_EMPLOYER, ApproachReadiness.VERIFY_CONTACT,
            ApproachReadiness.VERIFY_OFFER,
        },
        reasons=tuple(dict.fromkeys(reasons)), warnings=tuple(dict.fromkeys(warnings)),
        missing_information=tuple(dict.fromkeys(missing)), usable_facts=tuple(facts),
        prohibited_claims=tuple(dict.fromkeys(prohibited)),
    )


def _offer_key(offer: ActiveJobOffer) -> str:
    return f"{offer.source}:{offer.offer_id}"


def _select_entry_offer(
    offers: tuple[ActiveJobOffer, ...], company_name: str, descriptions: dict[str, str],
) -> tuple[ActiveJobOffer, tuple[str, ...]]:
    def quality(offer: ActiveJobOffer) -> tuple:
        description = descriptions.get(_offer_key(offer))
        ambiguous = bool(_related_entity_name(description, company_name) or _is_generic_publisher(description))
        age = offer.age_days
        return (
            1 if age is not None and age <= 30 else 0,
            0 if ambiguous else 1,
            1 if offer.display_location else 0,
            1 if offer.source_urls else 0,
            -(age if age is not None else 100000),
            -offer.first_seen_at.timestamp(),
        )
    selected = max(offers, key=lambda item: (quality(item), item.title.casefold(), _offer_key(item)))
    reasons = ["canonical_need_selected"]
    if selected.display_location:
        reasons.append("job_location_available")
    if selected.source_urls:
        reasons.append("source_url_available")
    if selected.age_days is not None and selected.age_days <= 30:
        reasons.append("published_within_30_days")
    if selected != offers[0]:
        reasons.append("quality_preferred_over_newest")
    return selected, tuple(reasons)


def _offer_warnings(offer: ActiveJobOffer, description: Optional[str], now: datetime) -> tuple[str, ...]:
    warnings = []
    if offer.published_at is None:
        warnings.append("publication_date_unknown")
    if (_as_utc(now) - _as_utc(offer.last_seen_at)).days > 7:
        warnings.append("last_observation_over_7_days")
    if offer.age_days is not None and offer.age_days > 30:
        warnings.append("publication_over_30_days")
    if not offer.source_urls:
        warnings.append("source_url_missing")
    if _is_generic_publisher(description):
        warnings.append("generic_publisher_description")
    return tuple(warnings)


def _employer_attribution(
    lead: CommercialLead, descriptions: dict[str, str],
) -> tuple[str, tuple[str, ...]]:
    status = lead.scoring.employer_relationship_status
    if status == "intermediary":
        return "intermediary_confirmed", ("existing_intermediary_classification",)
    if status == "intermediary_suspected":
        return "intermediary_suspected", ("existing_intermediary_warning",)
    ambiguous = []
    generic_count = sum(_is_generic_publisher(text) for text in descriptions.values())
    if generic_count >= 3 and generic_count * 2 >= len(lead.active_job_offers) and lead.distinct_job_title_count >= 3:
        ambiguous.append("generic_multi_role_publication")
    for offer in lead.active_job_offers:
        text = descriptions.get(_offer_key(offer))
        if _related_entity_name(text, lead.company_name):
            ambiguous.append("related_entity_named_in_offer")
            break
    if ambiguous:
        return "attribution_ambiguous", tuple(ambiguous)
    return "direct_employer_plausible", (
        "no_intermediary_marker_detected", "employer_not_independently_confirmed",
    )


_ENTITY_CLAIM = re.compile(
    r"(?:et si c['’]était\s+(?P<suggested>[^?!.]{3,80}?)\s+qu['’]il vous fallait)"
    r"|(?:^|[.!?,]\s+)(?P<recruiting>[A-ZÀ-ÖØ-Þ][\wÀ-ÿ &'’.-]{2,65}?)\s+(?:recherche|recrute)\b",
    re.IGNORECASE,
)


def _related_entity_name(description: Optional[str], company_name: str) -> bool:
    if not description:
        return False
    match = _ENTITY_CLAIM.search(description[:700])
    if match is None:
        return False
    named = _name_tokens(match.group("suggested") or match.group("recruiting") or "")
    observed = _name_tokens(company_name)
    if not named or not observed:
        return bool(named) and not observed
    return named != observed


def _name_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    words = re.findall(r"[a-z0-9]+", plain)
    return frozenset(words) - {"la", "le", "les", "l", "de", "du", "des", "societe", "association", "sas", "sarl", "sa", "filiale"}


def _is_generic_publisher(description: Optional[str]) -> bool:
    return bool(description and description.lstrip().casefold().startswith("offre collectée par la bonne alternance"))


def _excerpt(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    return re.sub(r"\s+", " ", description).strip()[:500] or None


def _contacts_for_entry(
    lead: CommercialLead, entry: ActiveJobOffer,
) -> tuple[tuple[ApproachContact, ...], Optional[int], str, Optional[int], str, tuple[str, ...]]:
    facts = lead.contactability
    candidates = tuple(_assess_contact(point, lead, entry) for point in facts.contact_points)
    usable_ids = {item.id for item in candidates if item.use == "usable"}
    usable_points = tuple(point for point in facts.contact_points if point.id in usable_ids)
    people = tuple(person for person in facts.people if person.scope == ContactScope.COMPANY or (
        person.scope == ContactScope.LOCAL and person.local_key == entry.local_key
    ))
    target = ContactTarget(
        company_key=lead.company_key, organization_name_snapshot=lead.official_name or lead.company_name,
        scope=ContactScope.COMPANY, siren=lead.siren, local_key=None,
        local_commune_snapshot=None, local_location_label_snapshot=None,
        employer_relationship_status=lead.scoring.employer_relationship_status,
        identity_match_status=lead.identity_match_status,
    )
    company_points = tuple(point for point in usable_points if point.scope == ContactScope.COMPANY)
    company_people = tuple(person for person in people if person.scope == ContactScope.COMPANY)
    strategy = build_contact_strategy(target, company_people, company_points)
    if strategy.contact_point_id is None and company_points:
        # A generic route can still reach a known HR person indirectly; it is
        # never represented as that person's direct contact method.
        routed = build_contact_strategy(target, (), company_points)
        if routed.contact_point_id is not None:
            strategy = routed
    local_points = tuple(point for point in usable_points if point.scope == ContactScope.LOCAL and point.local_key == entry.local_key)
    if local_points:
        local_target = ContactTarget(
            company_key=lead.company_key, organization_name_snapshot=lead.company_name,
            scope=ContactScope.LOCAL, siren=lead.siren, local_key=entry.local_key,
            local_commune_snapshot=entry.commune, local_location_label_snapshot=entry.location_label,
            employer_relationship_status=lead.scoring.employer_relationship_status,
            identity_match_status=lead.identity_match_status,
        )
        local_people = tuple(person for person in people if person.scope == ContactScope.LOCAL and person.local_key == entry.local_key)
        local_strategy = build_contact_strategy(local_target, local_people, local_points)
        company_has_hr_route = strategy.target_type in {"hr", "recruitment"} and strategy.contact_point_id is not None
        local_has_named_route = local_strategy.person_contact_id is not None and local_strategy.contact_point_id is not None
        if not company_has_hr_route and (strategy.contact_point_id is None or local_has_named_route):
            strategy = local_strategy
    selected_id = strategy.contact_point_id
    person_id = strategy.person_contact_id
    channel = strategy.preferred_channel
    target_type = strategy.target_type
    if selected_id is not None:
        selected = next(item for item in candidates if item.id == selected_id)
        if selected.type == ContactType.EMAIL and selected.role == "general_routing":
            channel = "general_email"
    return candidates, selected_id, channel, person_id, target_type, strategy.rationale_codes


def _assess_contact(point, lead: CommercialLead, entry: ActiveJobOffer) -> ApproachContact:
    evidence = lead.contactability.evidence_by_contact_point_id.get(point.id, ())
    relevance = lead.contactability.channel_relevance_by_contact_point_id.get(point.id)
    if relevance is None:
        related_domains = tuple(item.registrable_domain for item in lead.contactability.verified_websites
                                if item.target_scope == point.scope and item.local_key == point.local_key
                                and item.status != "rejected")
        relevance = assess_channel_relevance(point, evidence, related_domains=related_domains)
    relevance_status = relevance.status
    reasons = []
    use = "usable"
    if not point.is_active or point.verification_status == VerificationStatus.REJECTED:
        use, reasons = "rejected", ["inactive_or_rejected"]
    elif _unrelated_contact_purpose(point, evidence):
        use, reasons = "rejected", ["non_recruitment_function"]
    elif point.contact_type in {ContactType.WEBSITE, ContactType.PROFESSIONAL_URL} and _non_contact_page(point.value):
        use, reasons = "rejected", ["not_a_contact_page"]
    elif point.contact_type in {ContactType.WEBSITE, ContactType.PROFESSIONAL_URL} and not _is_contact_route(point.value):
        use, reasons = "verify", ["contact_route_unconfirmed"]
    elif relevance_status == ChannelRelevance.IRRELEVANT_FOREIGN:
        use, reasons = "rejected", ["foreign_channel_without_france_relevance"]
    elif point.scope not in {ContactScope.COMPANY, ContactScope.LOCAL}:
        use, reasons = "rejected", ["scope_not_employer"]
    elif point.scope == ContactScope.LOCAL and point.local_key != entry.local_key:
        use, reasons = "rejected", ["different_local_need"]
    elif point.scope == ContactScope.COMPANY and _specific_site_in_company_scope(evidence):
        use, reasons = "verify", ["specific_site_scope_unconfirmed"]
    elif point.verification_status in {VerificationStatus.STALE, VerificationStatus.UNVERIFIED} or point.confidence_level in {
        ContactConfidence.REVIEW_NEEDED, ContactConfidence.AMBIGUOUS,
    }:
        use, reasons = "verify", ["contact_attribution_or_freshness_unconfirmed"]
    elif relevance_status == ChannelRelevance.REVIEW_NEEDED:
        use, reasons = "verify", ["geographic_relevance_unconfirmed"]
    elif not evidence:
        use, reasons = "verify", ["contact_provenance_missing"]
    elif point.contact_type not in {ContactType.EMAIL, ContactType.PHONE, ContactType.PROFESSIONAL_URL, ContactType.WEBSITE}:
        use, reasons = "verify", ["unsupported_contact_type"]
    if point.contact_type == ContactType.EMAIL and not point.person_contact_id:
        role = "recruitment_service" if _recruitment_mailbox(point.value) else "general_routing"
    elif point.person_contact_id:
        person = next((item for item in lead.contactability.people if item.id == point.person_contact_id), None)
        role = person.relevance_role if person else "person_unconfirmed"
        if person is None or person.verification_status in {
            VerificationStatus.REJECTED, VerificationStatus.STALE, VerificationStatus.UNVERIFIED,
        }:
            use, reasons = "verify", ["linked_person_unconfirmed"]
    else:
        role = "general_routing"
    if use == "usable":
        reasons.append("sourced_professional_channel")
    return ApproachContact(
        id=point.id, type=point.contact_type, value=point.value,
        scope=point.scope, local_key=point.local_key,
        reach="national_france" if relevance_status == ChannelRelevance.NATIONAL_FRANCE else point.scope,
        person_contact_id=point.person_contact_id, role=role, use=use,
        confidence=point.confidence_level, verification_status=point.verification_status,
        commercial_relevance=relevance_status, reason_codes=tuple(reasons),
        source_urls=tuple(dict.fromkeys(item.source_url for item in evidence if item.source_url)),
    )


_NON_COMMERCIAL_LOCAL_PARTS = {
    "dpo", "dpd", "privacy", "confidentialite", "donneespersonnelles", "presse", "press", "media", "medias",
    "support", "sav", "serviceclient", "serviceclients", "consommateurs", "residents",
}


def _unrelated_contact_purpose(point, evidence) -> bool:
    if point.contact_type == ContactType.EMAIL:
        local_part = point.value.partition("@")[0].casefold().replace("-", "").replace("_", "").replace(".", "")
        if local_part in _NON_COMMERCIAL_LOCAL_PARTS:
            return True
    for row in evidence:
        path = urlsplit(row.source_url or "").path.casefold()
        if any(segment in path for segment in ("/presse", "/media")):
            return True
        if any(segment in path for segment in ("/politique-de-confidentialite", "/privacy")) and any(
            term in (row.excerpt or "").casefold() for term in ("délégué à la protection", "relations médias", "demande d’information presse")
        ):
            return True
    return False


def _non_contact_page(value: str) -> bool:
    path = urlsplit(value).path.casefold()
    return any(segment in path for segment in (
        "/politique-de-confidentialite", "/privacy", "/mentions-legales", "/legal", "/presse",
    ))


def _is_contact_route(value: str) -> bool:
    path = urlsplit(value).path.casefold()
    return any(segment in path for segment in ("contact", "recrut", "emploi", "career"))


def _specific_site_in_company_scope(evidence) -> bool:
    return any(
        any(segment in urlsplit(row.source_url or "").path.casefold() for segment in (
            "/residence/", "/etablissement/", "/agence/", "/magasin/",
        ))
        for row in evidence
    )


def _recruitment_mailbox(value: str) -> bool:
    local = value.partition("@")[0].casefold()
    return bool(re.fullmatch(r"(?:rh|recrutement|recruitment|jobs|emplois|talents)(?:[._-].+)?", local))


def _relationship_history(session: Session, lead: CommercialLead) -> tuple[ApproachHistory, ...]:
    clauses = [CommercialRelationship.company_key == lead.company_key]
    if lead.siren:
        clauses.append(CommercialRelationship.siren == lead.siren)
    return tuple(ApproachHistory(
        id=item.id, status=item.status, is_active=item.is_active,
        last_contact_at=item.last_contact_at, next_action_at=item.next_action_at,
    ) for item in session.scalars(select(CommercialRelationship).where(or_(*clauses)).order_by(CommercialRelationship.id)))


def _offer_descriptions(session: Session, lead: CommercialLead) -> dict[str, str]:
    keys = {identifier for offer in lead.active_job_offers for identifier in offer.source_offer_ids}
    ids = {identifier.partition(":")[2] for identifier in keys}
    if not ids:
        return {}
    return {
        f"{item.source}:{item.source_offer_id}": item.description
        for item in session.scalars(select(ObservedJobOffer).where(
            ObservedJobOffer.source_offer_id.in_(ids), ObservedJobOffer.is_active.is_(True),
        ))
        if item.description and f"{item.source}:{item.source_offer_id}" in keys
    }


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
