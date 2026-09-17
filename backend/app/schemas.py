from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    database: str


class AppSummary(BaseModel):
    app_name: str
    territory: str
    external_connectors_enabled: int
    contact_automation_enabled: bool
    generated_at: datetime


class FranceTravailAuthCheckResponse(BaseModel):
    status: str
    message: str


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
    role_diversity: int
    representative_roles: list[str]
    newest_offer_date: Optional[str]
    oldest_relevant_offer_date: Optional[str]
    latent_signals: list[OpportunitySignalResponse]
    total_score: int
    category: str
    subscores: ScoreSubscoresResponse
    adjustments: list[ScoreReasonResponse]
    positive_reasons: list[ScoreReasonResponse]
    penalties: list[ScoreReasonResponse]
    signals_used: list[OpportunitySignalResponse]
    evidence: list[LeadEvidenceResponse]
    is_eligible: bool
    exclusion: Optional[CommercialExclusionResponse]
    recommended_channel: Optional[str]

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
            role_diversity=lead.distinct_job_title_count,
            representative_roles=list(lead.representative_job_titles),
            newest_offer_date=lead.newest_offer_created_at,
            oldest_relevant_offer_date=lead.oldest_offer_created_at,
            latent_signals=[OpportunitySignalResponse(**item.__dict__) for item in lead.latent_signals],
            total_score=lead.scoring.total_score,
            category=lead.scoring.category,
            subscores=ScoreSubscoresResponse(**lead.scoring.subscores.__dict__),
            adjustments=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.commercial_adjustments],
            positive_reasons=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.positive_reasons],
            penalties=[ScoreReasonResponse(**item.__dict__) for item in lead.scoring.penalties],
            signals_used=[OpportunitySignalResponse(**item.__dict__) for item in lead.scoring.signals_used],
            evidence=[LeadEvidenceResponse(**item.__dict__) for item in lead.evidence],
            is_eligible=lead.is_eligible,
            exclusion=(CommercialExclusionResponse(**lead.exclusion.__dict__) if lead.exclusion else None),
            recommended_channel=lead.recommended_channel,
        )


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
