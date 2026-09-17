"""Read-only composition and deterministic querying of commercial leads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CommercialExclusion, CompanyEnrichment, ObservedJobOffer
from app.services.commercial_leads.exclusions import (
    CommercialExclusionRecord,
    ExclusionDecision,
    ExclusionTarget,
    evaluate_eligibility,
)
from app.services.company_enrichment.contracts import MatchStatus
from app.services.opportunities.company import (
    CompanyOpportunity,
    OpportunitySignal,
    aggregate_active_company_opportunities,
    normalize_company_key,
)
from app.services.scoring.company import (
    CompanyScoringResult,
    EnrichmentSnapshot,
    score_company_opportunity,
)


@dataclass(frozen=True)
class LeadEvidence:
    source_name: str
    source_url: Optional[str]
    observed_at: datetime


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
    distinct_job_title_count: int
    representative_job_titles: tuple[str, ...]
    newest_offer_created_at: Optional[str]
    oldest_offer_created_at: Optional[str]
    latent_signals: tuple[OpportunitySignal, ...]
    scoring: CompanyScoringResult
    evidence: tuple[LeadEvidence, ...]
    is_eligible: bool
    exclusion: Optional[CommercialExclusionRecord]
    recommended_channel: Optional[str] = None


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
        distinct_job_title_count=opportunity.distinct_job_title_count,
        representative_job_titles=opportunity.job_titles,
        newest_offer_created_at=opportunity.newest_offer_created_at,
        oldest_offer_created_at=opportunity.oldest_offer_created_at,
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
