from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    database: str


class AppSummary(BaseModel):
    app_name: str
    territory: str
    external_connectors_enabled: int
    contact_automation_enabled: bool
    generated_at: datetime


class BraveUsageResponse(BaseModel):
    monthly_budget: int
    monthly_used: int
    monthly_remaining: int
    percentage_used: Decimal
    estimated_cost_used_usd: Decimal
    estimated_credit_remaining_usd: Decimal
    project_total_requests: int
    project_total_attempts: int
    project_total_counted_requests: int
    current_period_start: datetime
    current_period_end: datetime
    days_remaining_in_period: int
    default_run_cap: int
    maximum_allowed_for_next_run: int
    pacing_per_day: Decimal
    status: str


class FranceTravailAuthCheckResponse(BaseModel):
    status: str
    message: str


class SearchRunCreateRequest(BaseModel):
    department: str = Field(default="94", min_length=1, max_length=3)
    requested_actionable_leads: int = Field(default=25, ge=1, le=100)
    brave_hard_cap: int = Field(default=40, ge=0, le=40)


class SearchRunProgressResponse(BaseModel):
    id: int
    status: str
    department: str
    requested_actionable_leads: int
    current_actionable_leads: int
    candidates_considered: int
    candidates_enriched: int
    brave_requests_used: int
    brave_hard_cap: int
    current_company_key: Optional[str]
    current_company_name: Optional[str]
    current_step: Optional[str]
    completion_reason: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error_summary: Optional[str]


class SearchRunResponse(SearchRunProgressResponse):
    stop_requested: bool
    configuration_fingerprint: Optional[str]


class OpportunitySignalResponse(BaseModel):
    name: str
    active: bool
    explanation: str


class ScoreReasonResponse(BaseModel):
    code: str
    message: str
    points: int


class ScoreSubscoresResponse(BaseModel):
    direct_need: int
    latent_signals: int
    commercial_relevance: int
    accessibility: int
    evidence_freshness: int


class LeadEvidenceResponse(BaseModel):
    source_name: str
    source_url: Optional[str]
    observed_at: datetime


class LocalOpportunityResponse(BaseModel):
    """Descriptive local offer bucket; it is not a legal establishment identity."""

    local_key: str
    commune: Optional[str]
    location_label: Optional[str]
    department: str
    active_offer_count: int
    role_diversity: int
    representative_roles: list[str]
    newest_offer_date: Optional[str]
    oldest_offer_date: Optional[str]
    source_offer_ids: list[str]
    source_urls: list[str]
    signals: list[OpportunitySignalResponse]
    contact_point_ids: list[int] = Field(default_factory=list)
    person_contact_ids: list[int] = Field(default_factory=list)


class ActiveJobOfferResponse(BaseModel):
    offer_id: str
    title: str
    commune: Optional[str]
    location_label: Optional[str]
    display_location: Optional[str]
    published_at: Optional[str]
    updated_at: Optional[str]
    contract_type: Optional[str]
    salary: Optional[str]
    source: str
    source_url: Optional[str]
    local_key: str
    age_days: Optional[int]


class ContactProvenanceResponse(BaseModel):
    id: int
    provider: str
    source_url: Optional[str]
    source_type: str
    observed_at: datetime
    short_excerpt: Optional[str]
    confidence: Optional[str]
    reason: Optional[str]


class ContactPointResponse(BaseModel):
    id: int
    type: str
    value: str
    scope: str
    local_key: Optional[str]
    confidence: str
    verification_status: str
    provider: Optional[str]
    person_contact_id: Optional[int]
    observed_at: Optional[datetime]
    last_verified_at: Optional[datetime]
    stale: bool
    evidence: list[ContactProvenanceResponse]
    warnings: list[str] = Field(default_factory=list)
    commercial_relevance: str = "relevant"


class PersonContactResponse(BaseModel):
    id: int
    display_name: str
    relevance: str
    role_title: Optional[str]
    scope: str
    local_key: Optional[str]
    confidence: str
    verification_status: str
    contact_point_ids: list[int]
    provenance: list[ContactProvenanceResponse]
    warnings: list[str] = Field(default_factory=list)


class ContactStrategyResponse(BaseModel):
    target_type: str
    preferred_channel: str
    preferred_contact_point_id: Optional[int]
    preferred_person_contact_id: Optional[int]
    fallback_channels: list[str]
    confidence: str
    rationale_codes: list[str]
    short_context: str
    warnings: list[str]
    missing_information: list[str]
    evidence: list[ContactProvenanceResponse]
    scope: str
    local_key: Optional[str]
    channel_relevance: str


class OfficialWebStatusResponse(BaseModel):
    verified_site_status: Optional[str]
    verified_domain: Optional[str]
    verification_score: int
    provider: Optional[str]
    warnings: list[str]


class RecruitmentContextResponse(BaseModel):
    active_offer_count: int
    representative_roles: list[str]
    newest_offer_date: Optional[str]
    primary_location: Optional[str]
    employee_range: Optional[str]
    employer_relationship_status: str
    scoring_reason_codes: list[str]


class ContactabilitySummaryResponse(BaseModel):
    scope: str
    official_web: OfficialWebStatusResponse
    recruitment_context: RecruitmentContextResponse
    warnings: list[str]


class IntermediaryDescriptionExampleResponse(BaseModel):
    offer_id: str
    title: str
    location_label: Optional[str]
    marker_types: list[str]


class IntermediaryDescriptionEvidenceResponse(BaseModel):
    total_offer_count: int
    strong_signal_offer_count: int
    strong_signal_proportion: float
    marker_types: list[str]
    examples: list[IntermediaryDescriptionExampleResponse]


class CommercialExclusionResponse(BaseModel):
    id: int
    company_key: str
    siren: Optional[str]
    company_name_snapshot: str
    exclusion_type: str
    reason: Optional[str]
    starts_at: datetime
    expires_at: Optional[datetime]
    created_at: Optional[datetime]


class CommercialLeadResponse(BaseModel):
    company_key: str
    company_name: str
    official_name: Optional[str]
    siren: Optional[str]
    siret: Optional[str]
    entity_sector_type: str
    employee_range: Optional[str]
    department: str
    primary_location: Optional[str]
    active_offer_count: int
    active_job_offers: list[ActiveJobOfferResponse]
    role_diversity: int
    representative_roles: list[str]
    newest_offer_date: Optional[str]
    oldest_relevant_offer_date: Optional[str]
    local_opportunities: list[LocalOpportunityResponse]
    latent_signals: list[OpportunitySignalResponse]
    total_score: int
    category: str
    subscores: ScoreSubscoresResponse
    adjustments: list[ScoreReasonResponse]
    positive_reasons: list[ScoreReasonResponse]
    penalties: list[ScoreReasonResponse]
    signals_used: list[OpportunitySignalResponse]
    employer_relationship_status: str
    employer_relationship_reasons: list[ScoreReasonResponse]
    intermediary_description_evidence: IntermediaryDescriptionEvidenceResponse
    evidence: list[LeadEvidenceResponse]
    is_eligible: bool
    exclusion: Optional[CommercialExclusionResponse]
    recommended_channel: Optional[str]
    contacts: list[ContactPointResponse] = Field(default_factory=list)
    people: list[PersonContactResponse] = Field(default_factory=list)
    contact_strategy: ContactStrategyResponse
    contactability_summary: ContactabilitySummaryResponse

    @classmethod
    def from_lead(cls, lead) -> "CommercialLeadResponse":
        return cls(
            company_key=lead.company_key,
            company_name=lead.company_name,
            official_name=lead.official_name,
            siren=lead.siren,
            siret=lead.siret,
            entity_sector_type=lead.entity_sector_type,
            employee_range=lead.employee_range,
            department=lead.scoring.department_code,
            primary_location=lead.principal_location,
            active_offer_count=lead.active_offer_count,
            active_job_offers=[ActiveJobOfferResponse(**item.__dict__) for item in lead.active_job_offers],
            role_diversity=lead.distinct_job_title_count,
            representative_roles=list(lead.representative_job_titles),
            newest_offer_date=lead.newest_offer_created_at,
            oldest_relevant_offer_date=lead.oldest_offer_created_at,
            local_opportunities=[LocalOpportunityResponse(
                local_key=item.local_key,
                commune=item.commune,
                location_label=item.location_label,
                department=item.department_code,
                active_offer_count=item.active_offer_count,
                role_diversity=item.distinct_job_title_count,
                representative_roles=list(item.representative_job_titles),
                newest_offer_date=item.newest_offer_created_at,
                oldest_offer_date=item.oldest_offer_created_at,
                source_offer_ids=list(item.source_offer_ids),
                source_urls=list(item.source_urls),
                signals=[OpportunitySignalResponse(**signal.__dict__) for signal in item.signals],
                contact_point_ids=sorted(point.id for point in lead.contactability.contact_points if point.scope == "local" and point.local_key == item.local_key),
                person_contact_ids=sorted(person.id for person in lead.contactability.people if person.scope == "local" and person.local_key == item.local_key and person.is_active and person.verification_status != "rejected"),
            ) for item in lead.local_opportunities],
            latent_signals=[OpportunitySignalResponse(**item.__dict__) for item in lead.latent_signals],
            total_score=lead.scoring.total_score,
            category=lead.scoring.category,
            subscores=ScoreSubscoresResponse(**lead.scoring.subscores.__dict__),
            adjustments=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.commercial_adjustments],
            positive_reasons=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.positive_reasons],
            penalties=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.penalties],
            signals_used=[OpportunitySignalResponse(**item.__dict__) for item in lead.scoring.signals_used],
            employer_relationship_status=lead.scoring.employer_relationship_status,
            employer_relationship_reasons=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.employer_relationship_reasons],
            intermediary_description_evidence=IntermediaryDescriptionEvidenceResponse(
                total_offer_count=lead.scoring.intermediary_description_evidence.total_offer_count,
                strong_signal_offer_count=lead.scoring.intermediary_description_evidence.strong_signal_offer_count,
                strong_signal_proportion=lead.scoring.intermediary_description_evidence.strong_signal_proportion,
                marker_types=list(lead.scoring.intermediary_description_evidence.marker_types),
                examples=[IntermediaryDescriptionExampleResponse(
                    offer_id=item.offer_id,
                    title=item.title,
                    location_label=item.location_label,
                    marker_types=list(item.marker_types),
                ) for item in lead.scoring.intermediary_description_evidence.examples],
            ),
            evidence=[LeadEvidenceResponse(**item.__dict__) for item in lead.evidence],
            is_eligible=lead.is_eligible,
            exclusion=(CommercialExclusionResponse(**lead.exclusion.__dict__) if lead.exclusion else None),
            recommended_channel=lead.recommended_channel,
            contacts=[_contact_point_response(item, lead) for item in lead.contactability.contact_points],
            people=[_person_contact_response(item, lead) for item in lead.contactability.people if item.is_active and item.verification_status != "rejected"],
            contact_strategy=_strategy_response(lead),
            contactability_summary=_contactability_summary(lead),
        )


def _provenance_responses(rows, confidence: Optional[str]) -> list[ContactProvenanceResponse]:
    """Keep compact, useful distinct sources; never return provider payloads or HTML."""
    unique = {}
    for row in rows:
        key = (row.provider, row.source_name, row.source_url, row.evidence_reason, row.excerpt)
        current = unique.get(key)
        if current is None or row.observed_at > current.observed_at:
            unique[key] = row
    return [ContactProvenanceResponse(
        id=row.id,
        provider=row.provider,
        source_url=row.source_url,
        source_type=row.source_name,
        observed_at=row.observed_at,
        short_excerpt=(row.excerpt[:400] if row.excerpt else None),
        confidence=confidence,
        reason=row.evidence_reason,
    ) for row in sorted(unique.values(), key=lambda item: (item.provider, item.source_url or "", item.id))]


def _contact_point_response(point, lead) -> ContactPointResponse:
    evidence = lead.contactability.evidence_by_contact_point_id.get(point.id, ())
    warnings = []
    if point.verification_status == "rejected":
        warnings.append("Coordonnée rejetée : ne pas utiliser comme canal de contact.")
    elif point.verification_status == "stale" or not point.is_active:
        warnings.append("Coordonnée obsolète ou inactive : vérification requise avant usage.")
    elif point.confidence_level in {"review_needed", "ambiguous"}:
        warnings.append("Coordonnée non recommandée comme canal fiable sans vérification.")
    relevance = lead.contactability.channel_relevance_by_contact_point_id.get(point.id)
    if relevance:
        warnings.extend(relevance.warnings)
    return ContactPointResponse(
        id=point.id,
        type=point.contact_type,
        value=point.normalized_value,
        scope=point.scope,
        local_key=point.local_key,
        confidence=point.confidence_level,
        verification_status=point.verification_status,
        provider=(evidence[0].provider if evidence else None),
        person_contact_id=point.person_contact_id,
        observed_at=point.first_observed_at,
        last_verified_at=(point.last_observed_at if point.verification_status in {"source_verified", "manually_verified"} else None),
        stale=point.verification_status == "stale" or not point.is_active,
        evidence=_provenance_responses(evidence, point.confidence_level),
        warnings=warnings,
        commercial_relevance=(relevance.status if relevance else "relevant"),
    )


def _person_contact_response(person, lead) -> PersonContactResponse:
    evidence = lead.contactability.evidence_by_person_contact_id.get(person.id, ())
    warnings = []
    if person.verification_status == "unverified":
        warnings.append("Rôle identifié mais non vérifié par la source.")
    if person.verification_status == "stale":
        warnings.append("Information personne obsolète : vérification requise.")
    return PersonContactResponse(
        id=person.id,
        display_name=person.full_name,
        relevance=person.relevance_role,
        role_title=person.job_title,
        scope=person.scope,
        local_key=person.local_key,
        confidence=person.confidence_level,
        verification_status=person.verification_status,
        contact_point_ids=sorted(point.id for point in lead.contactability.contact_points if point.person_contact_id == person.id),
        provenance=_provenance_responses(evidence, person.confidence_level),
        warnings=warnings,
    )


def _strategy_response(lead) -> ContactStrategyResponse:
    strategy = lead.contact_strategy
    assert strategy is not None
    evidence_by_id = {
        row.id: row
        for rows in (*lead.contactability.evidence_by_contact_point_id.values(), *lead.contactability.evidence_by_person_contact_id.values())
        for row in rows
    }
    rows = [evidence_by_id[item.evidence_id] for item in strategy.evidence_references if item.evidence_id in evidence_by_id]
    return ContactStrategyResponse(
        target_type=strategy.target_type,
        preferred_channel=strategy.preferred_channel,
        preferred_contact_point_id=strategy.contact_point_id,
        preferred_person_contact_id=strategy.person_contact_id,
        fallback_channels=list(strategy.fallback_channels),
        confidence=strategy.confidence,
        rationale_codes=list(strategy.rationale_codes),
        short_context=strategy.short_context,
        warnings=list(strategy.warnings),
        missing_information=list(strategy.missing_information),
        evidence=_provenance_responses(rows, strategy.confidence),
        scope=strategy.scope,
        local_key=strategy.local_key,
        channel_relevance=strategy.channel_relevance,
    )


def _contactability_summary(lead) -> ContactabilitySummaryResponse:
    strategy = lead.contact_strategy
    assert strategy is not None
    candidates = [item for item in lead.contactability.verified_websites if item.target_scope == strategy.scope and item.local_key == strategy.local_key]
    status_priority = {"high_confidence": 0, "review_needed": 1, "ambiguous": 2, "rejected": 3}
    presentable = [item for item in candidates if _is_presentable_official_site(item, lead.company_key)]
    site = sorted(presentable, key=lambda item: (status_priority.get(item.status, 99), -item.score, item.registrable_domain, item.id))[0] if presentable else None
    hidden_candidates = [item for item in candidates if item not in presentable]
    web_warnings = list(site.attribution_warnings or ()) + list(site.rejection_reasons or ()) if site else []
    if hidden_candidates and site is None:
        web_warnings.append("Aucun site officiel n'a pu être vérifié avec suffisamment de confiance.")
    reasons = [item.code for item in (*lead.scoring.positive_reasons, *lead.scoring.commercial_adjustments)]
    return ContactabilitySummaryResponse(
        scope=strategy.scope,
        official_web=OfficialWebStatusResponse(
            verified_site_status=site.status if site else ("rejected" if hidden_candidates else None),
            verified_domain=site.registrable_domain if site else None,
            verification_score=(site.score if site else 0),
            provider=site.provider if site else None,
            warnings=list(dict.fromkeys(web_warnings)),
        ),
        recruitment_context=RecruitmentContextResponse(
            active_offer_count=lead.active_offer_count,
            representative_roles=list(lead.representative_job_titles),
            newest_offer_date=lead.newest_offer_created_at,
            primary_location=lead.principal_location,
            employee_range=lead.employee_range,
            employer_relationship_status=lead.scoring.employer_relationship_status,
            scoring_reason_codes=reasons,
        ),
        warnings=list(strategy.warnings),
    )


_THIRD_PARTY_SUMMARY_REASONS = frozenset({
    "competing_domains", "third_party_commercial_aggregator", "third_party_directory",
})
_THIRD_PARTY_SUMMARY_DOMAINS = frozenset({"lefigaro.fr", "wikidata.org"})


def _is_presentable_official_site(site, company_key: str) -> bool:
    """Keep historic and third-party hypotheses auditable but out of the UI summary."""
    fresh_until = site.fresh_until
    if fresh_until.tzinfo is None:
        fresh_until = fresh_until.replace(tzinfo=timezone.utc)
    if fresh_until < datetime.now(timezone.utc) or site.status == "rejected":
        return False
    reasons = {str(value).casefold() for value in (*site.rejection_reasons, *site.attribution_warnings)}
    if reasons & _THIRD_PARTY_SUMMARY_REASONS:
        return False
    if site.registrable_domain.casefold() in _THIRD_PARTY_SUMMARY_DOMAINS:
        return False
    if site.status in {"review_needed", "ambiguous"}:
        domain = "".join(character for character in site.registrable_domain.casefold() if character.isalnum())
        tokens = [token for token in company_key.casefold().split() if len(token) >= 5]
        return any(token in domain for token in tokens)
    return True


class CommercialLeadListResponse(BaseModel):
    items: list[CommercialLeadResponse]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page) -> "CommercialLeadListResponse":
        return cls(
            items=[CommercialLeadResponse.from_lead(item) for item in page.items],
            total=page.total_count,
            limit=page.limit or page.total_count,
            offset=page.offset,
        )
