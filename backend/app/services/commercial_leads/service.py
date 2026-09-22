"""Read-only composition and deterministic querying of commercial leads."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    CommercialExclusion,
    CompanyEnrichment,
    ContactEvidence,
    ContactPoint,
    ObservedJobOffer,
    PersonContact,
    VerifiedWebsiteRecord,
)
from app.services.commercial_leads.exclusions import (
    CommercialExclusionRecord,
    ExclusionDecision,
    ExclusionTarget,
    evaluate_eligibility,
)
from app.services.company_enrichment.contracts import MatchStatus
from app.services.opportunities.company import (
    ActiveJobOffer,
    CompanyOpportunity,
    LocalOpportunity,
    OpportunitySignal,
    aggregate_active_company_opportunities,
    normalize_company_key,
)
from app.services.scoring.company import (
    CompanyScoringResult,
    EnrichmentSnapshot,
    score_company_opportunity,
)
from app.services.contactability.contracts import ContactScope, ContactTarget, VerificationStatus
from app.services.contactability.strategy import (
    ContactStrategy,
    StrategyEvidenceReference,
    build_contact_strategy,
)
from app.services.contactability.relevance import (
    ChannelRelevance, ChannelRelevanceAssessment, assess_channel_relevance,
)


@dataclass(frozen=True)
class LeadEvidence:
    source_name: str
    source_url: Optional[str]
    observed_at: datetime


@dataclass(frozen=True)
class ContactabilityFacts:
    """Already-persisted contact facts for one commercial key, loaded in batches."""

    contact_points: tuple[ContactPoint, ...] = ()
    people: tuple[PersonContact, ...] = ()
    evidence_by_contact_point_id: dict[int, tuple[ContactEvidence, ...]] = field(default_factory=dict)
    evidence_by_person_contact_id: dict[int, tuple[ContactEvidence, ...]] = field(default_factory=dict)
    verified_websites: tuple[VerifiedWebsiteRecord, ...] = ()
    channel_relevance_by_contact_point_id: dict[int, ChannelRelevanceAssessment] = field(default_factory=dict)


@dataclass(frozen=True)
class CommercialLead:
    company_key: str
    company_name: str
    official_name: Optional[str]
    siren: Optional[str]
    siret: Optional[str]
    entity_sector_type: str
    employee_range: Optional[str]
    principal_location: Optional[str]
    active_offer_count: int
    active_job_offers: tuple[ActiveJobOffer, ...]
    distinct_job_title_count: int
    representative_job_titles: tuple[str, ...]
    newest_offer_created_at: Optional[str]
    oldest_offer_created_at: Optional[str]
    local_opportunities: tuple[LocalOpportunity, ...]
    latent_signals: tuple[OpportunitySignal, ...]
    scoring: CompanyScoringResult
    evidence: tuple[LeadEvidence, ...]
    is_eligible: bool
    exclusion: Optional[CommercialExclusionRecord]
    recommended_channel: Optional[str] = None
    contactability: ContactabilityFacts = field(default_factory=ContactabilityFacts)
    contact_strategy: Optional[ContactStrategy] = None


@dataclass(frozen=True)
class CommercialLeadQuery:
    department_code: str = "94"
    include_excluded: bool = False
    categories: Optional[frozenset[str]] = None
    entity_sector_types: Optional[frozenset[str]] = None
    minimum_score: Optional[int] = None
    offset: int = 0
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        if self.minimum_score is not None and not 0 <= self.minimum_score <= 100:
            raise ValueError("minimum_score must be between 0 and 100")
        if self.offset < 0:
            raise ValueError("offset must not be negative")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be positive")


@dataclass(frozen=True)
class CommercialLeadPage:
    items: tuple[CommercialLead, ...]
    total_count: int
    offset: int
    limit: Optional[int]


def list_commercial_leads(
    session: Session,
    query: CommercialLeadQuery = CommercialLeadQuery(),
    now: Optional[datetime] = None,
    provider: str = "dinum",
) -> CommercialLeadPage:
    """Build sorted leads from local facts without persisting derived records."""
    observed_at = now or datetime.now(timezone.utc)
    aggregation = aggregate_active_company_opportunities(
        session, department_code=query.department_code, now=observed_at
    )
    enrichments = {
        row.company_key: row
        for row in session.scalars(
            select(CompanyEnrichment).where(CompanyEnrichment.provider == provider)
        )
    }
    exclusions = tuple(
        CommercialExclusionRecord.from_model(row)
        for row in session.scalars(select(CommercialExclusion))
    )
    evidence_by_key = _evidence_by_company_key(session, query.department_code)
    leads = [
        _build_lead(
            opportunity, enrichments.get(opportunity.company_key), exclusions,
            evidence_by_key.get(opportunity.company_key, ()), observed_at,
        )
        for opportunity in aggregation.opportunities
    ]
    filtered = [lead for lead in leads if _matches_query(lead, query)]
    ordered = sorted(filtered, key=lambda item: (-item.scoring.total_score, item.company_name.casefold(), item.company_key))
    total_count = len(ordered)
    paged = ordered[query.offset:] if query.limit is None else ordered[query.offset:query.offset + query.limit]
    facts_by_key = _contactability_by_company_key(session, (item.company_key for item in paged))
    paged = tuple(_attach_contactability(item, facts_by_key.get(item.company_key, ContactabilityFacts())) for item in paged)
    return CommercialLeadPage(
        items=tuple(paged), total_count=total_count, offset=query.offset, limit=query.limit
    )


def _build_lead(
    opportunity: CompanyOpportunity,
    enrichment: Optional[CompanyEnrichment],
    exclusions: tuple[CommercialExclusionRecord, ...],
    evidence: tuple[LeadEvidence, ...],
    now: datetime,
) -> CommercialLead:
    snapshot = EnrichmentSnapshot.from_model(enrichment)
    scoring = score_company_opportunity(opportunity, snapshot)
    is_high_confidence = enrichment is not None and enrichment.match_status == MatchStatus.HIGH_CONFIDENCE
    siren = enrichment.siren if is_high_confidence else None
    decision = evaluate_eligibility(
        ExclusionTarget(company_key=opportunity.company_key, siren=siren), exclusions, now
    )
    return CommercialLead(
        company_key=opportunity.company_key,
        company_name=opportunity.company_name,
        official_name=enrichment.official_name if is_high_confidence else None,
        siren=siren,
        siret=enrichment.siret if is_high_confidence else None,
        entity_sector_type=(enrichment.entity_sector_type if enrichment else "unknown"),
        employee_range=enrichment.employee_range if is_high_confidence else None,
        principal_location=_principal_location(opportunity, enrichment, is_high_confidence),
        active_offer_count=opportunity.active_offer_count,
        active_job_offers=opportunity.active_job_offers,
        distinct_job_title_count=opportunity.distinct_job_title_count,
        representative_job_titles=opportunity.job_titles,
        newest_offer_created_at=opportunity.newest_offer_created_at,
        oldest_offer_created_at=opportunity.oldest_offer_created_at,
        local_opportunities=opportunity.local_opportunities,
        latent_signals=tuple(signal for signal in opportunity.signals if signal.active),
        scoring=scoring,
        evidence=evidence,
        is_eligible=decision.is_eligible,
        exclusion=decision.exclusion,
    )


def _matches_query(lead: CommercialLead, query: CommercialLeadQuery) -> bool:
    if not query.include_excluded and not lead.is_eligible:
        return False
    if query.categories is not None and lead.scoring.category not in query.categories:
        return False
    if query.entity_sector_types is not None and lead.entity_sector_type not in query.entity_sector_types:
        return False
    return query.minimum_score is None or lead.scoring.total_score >= query.minimum_score


def _principal_location(
    opportunity: CompanyOpportunity,
    enrichment: Optional[CompanyEnrichment],
    is_high_confidence: bool,
) -> Optional[str]:
    if is_high_confidence and enrichment:
        return enrichment.address or enrichment.commune or enrichment.postal_code
    return (opportunity.location_labels or opportunity.communes or (None,))[0]


def _evidence_by_company_key(
    session: Session, department_code: str
) -> dict[str, tuple[LeadEvidence, ...]]:
    grouped: dict[str, list[LeadEvidence]] = {}
    offers = session.scalars(
        select(ObservedJobOffer)
        .where(
            ObservedJobOffer.is_active.is_(True),
            ObservedJobOffer.department_code == department_code,
        )
        .order_by(ObservedJobOffer.id)
    )
    for offer in offers:
        company_key = normalize_company_key(offer.company_name)
        if company_key is None:
            continue
        grouped.setdefault(company_key, []).append(LeadEvidence(
            source_name=offer.source,
            source_url=offer.source_url,
            observed_at=offer.last_seen_at,
        ))
    return {
        key: tuple(sorted(
            {(item.source_name, item.source_url, item.observed_at): item for item in values}.values(),
            key=lambda item: (item.source_name.casefold(), item.source_url or "", item.observed_at),
        ))
        for key, values in grouped.items()
    }


def _contactability_by_company_key(
    session: Session, company_keys: object,
) -> dict[str, ContactabilityFacts]:
    """Load all contactability relations in bounded set queries, never per lead."""
    keys = tuple(sorted(set(company_keys)))
    if not keys:
        return {}
    points = tuple(session.scalars(select(ContactPoint).where(ContactPoint.company_key.in_(keys)).order_by(ContactPoint.id)))
    people = tuple(session.scalars(select(PersonContact).where(PersonContact.company_key.in_(keys)).order_by(PersonContact.id)))
    point_ids = tuple(item.id for item in points)
    person_ids = tuple(item.id for item in people)
    clauses = []
    if point_ids:
        clauses.append(ContactEvidence.contact_point_id.in_(point_ids))
    if person_ids:
        clauses.append(ContactEvidence.person_contact_id.in_(person_ids))
    evidence = tuple(session.scalars(select(ContactEvidence).where(or_(*clauses)).order_by(ContactEvidence.id))) if clauses else ()
    websites = tuple(session.scalars(select(VerifiedWebsiteRecord).where(VerifiedWebsiteRecord.company_key.in_(keys)).order_by(VerifiedWebsiteRecord.id)))

    points_by_key: dict[str, list[ContactPoint]] = {key: [] for key in keys}
    people_by_key: dict[str, list[PersonContact]] = {key: [] for key in keys}
    for item in points:
        points_by_key.setdefault(item.company_key, []).append(item)
    for item in people:
        people_by_key.setdefault(item.company_key, []).append(item)
    evidence_by_point: dict[int, list[ContactEvidence]] = {}
    evidence_by_person: dict[int, list[ContactEvidence]] = {}
    for item in evidence:
        if item.contact_point_id is not None:
            evidence_by_point.setdefault(item.contact_point_id, []).append(item)
        if item.person_contact_id is not None:
            evidence_by_person.setdefault(item.person_contact_id, []).append(item)
    websites_by_key: dict[str, list[VerifiedWebsiteRecord]] = {key: [] for key in keys}
    for item in websites:
        websites_by_key.setdefault(item.company_key, []).append(item)
    return {
        key: ContactabilityFacts(
            contact_points=tuple(points_by_key.get(key, ())),
            people=tuple(people_by_key.get(key, ())),
            evidence_by_contact_point_id={item.id: tuple(evidence_by_point.get(item.id, ())) for item in points_by_key.get(key, ())},
            evidence_by_person_contact_id={item.id: tuple(evidence_by_person.get(item.id, ())) for item in people_by_key.get(key, ())},
            verified_websites=tuple(websites_by_key.get(key, ())),
        )
        for key in keys
    }


def _attach_contactability(lead: CommercialLead, facts: ContactabilityFacts) -> CommercialLead:
    """Recalculate the primary strategy from preloaded facts, without provider I/O."""
    relationship = lead.scoring.employer_relationship_status
    scope = ContactScope.INTERMEDIARY if relationship == "intermediary" else ContactScope.COMPANY
    target = ContactTarget(
        company_key=lead.company_key,
        organization_name_snapshot=lead.official_name or lead.company_name,
        scope=scope,
        siren=lead.siren,
        local_key=None,
        local_commune_snapshot=None,
        local_location_label_snapshot=None,
        employer_relationship_status=relationship,
        identity_match_status="matched_high_confidence" if lead.siren else None,
        warnings=(
            ("Les coordonnées sont attribuées à l'intermédiaire, pas à un employeur final.",)
            if scope == ContactScope.INTERMEDIARY else
            (("Intermédiaire ou diffuseur possible : vérification recommandée avant attribution.",)
             if relationship == "intermediary_suspected" else ())
        ),
    )
    people = tuple(item for item in facts.people if item.scope == scope and item.is_active and item.verification_status != VerificationStatus.REJECTED)
    points = tuple(item for item in facts.contact_points if item.scope == scope and item.is_active)
    related_domains = tuple(
        item.registrable_domain for item in facts.verified_websites
        if item.target_scope == scope and item.local_key is None and item.status != "rejected"
    )
    assessments = {
        item.id: assess_channel_relevance(
            item, facts.evidence_by_contact_point_id.get(item.id, ()),
            related_domains=related_domains,
        )
        for item in points
    }
    recommended_points = tuple(
        item for item in points
        if assessments[item.id].status in {ChannelRelevance.RELEVANT, ChannelRelevance.NATIONAL_FRANCE}
    )
    evidence = _strategy_evidence(facts, people, recommended_points)
    strategy = build_contact_strategy(
        target, people, recommended_points,
        employee_range=lead.employee_range,
        recruitment_context=lead.representative_job_titles,
        evidence_references=evidence,
    )
    selected_assessment = assessments.get(strategy.contact_point_id) if strategy.contact_point_id else None
    geography_warnings = [
        warning for assessment in assessments.values()
        if assessment.status not in {ChannelRelevance.RELEVANT}
        for warning in assessment.warnings
    ]
    strategy = replace(
        strategy,
        channel_relevance=(selected_assessment.status if selected_assessment else ChannelRelevance.RELEVANT),
        warnings=tuple(dict.fromkeys((*strategy.warnings, *geography_warnings))),
        rationale_codes=tuple(dict.fromkeys((
            *strategy.rationale_codes,
            *(selected_assessment.rationale_codes if selected_assessment else ()),
        ))),
    )
    facts = replace(facts, channel_relevance_by_contact_point_id=assessments)
    return replace(lead, contactability=facts, contact_strategy=strategy)


def _strategy_evidence(
    facts: ContactabilityFacts,
    people: tuple[PersonContact, ...],
    points: tuple[ContactPoint, ...],
) -> tuple[StrategyEvidenceReference, ...]:
    rows = []
    for person in people:
        rows.extend(facts.evidence_by_person_contact_id.get(person.id, ()))
    for point in points:
        rows.extend(facts.evidence_by_contact_point_id.get(point.id, ()))
    return tuple(StrategyEvidenceReference(
        evidence_id=item.id, provider=item.provider, source_name=item.source_name,
        source_url=item.source_url, evidence_reason=item.evidence_reason,
    ) for item in sorted({item.id: item for item in rows}.values(), key=lambda item: item.id))
