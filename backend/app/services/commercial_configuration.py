"""Typed private configuration consumed alongside commercial approach context.

No real configuration is seeded by application code. An absent profile, catalog,
or policy remains absent until entered on the local API.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CommercialCatalogOfferRecord, CommercialPolicyRecord, CommercialProfileRecord


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Specialty(StrictModel):
    label: str = Field(min_length=1)
    related_roles: list[str] = Field(default_factory=list)
    expertise_scope: Literal["personal", "network", "method_only"]


class Territory(StrictModel):
    department_code: str = Field(pattern=r"^(?:[0-9]{2,3}|2A|2B)$")
    territorial_familiarity: bool


class VerifiedReference(StrictModel):
    description: str = Field(min_length=1)
    evidence_scope: Literal["personal", "network"]
    approved_for_claims: bool = False


class CommercialProfile(StrictModel):
    consultant_name: str = Field(min_length=1)
    commercial_title: str = Field(min_length=1)
    network_name: str = Field(min_length=1)
    phone: Optional[str] = None
    specialties: list[Specialty] = Field(default_factory=list)
    territories: list[Territory] = Field(default_factory=list)
    verified_references: list[VerifiedReference] = Field(default_factory=list)


class OfferFeatures(StrictModel):
    launch_minutes_min: int = Field(ge=0)
    launch_minutes_max: int = Field(ge=0)
    distribution_site_count_min: int = Field(ge=0)
    distribution_months: Optional[int] = Field(default=None, ge=1)
    hunt_campaign_count: Optional[int] = Field(default=None, ge=0)
    hunted_candidates_per_campaign: Optional[int] = Field(default=None, ge=0)
    unlimited_hunt_with_deposit: bool = False
    unlimited_hunt_included: bool = False
    application_processing_hours: int = Field(gt=0)
    phone_screen: bool
    interview_screen: bool
    candidate_questionnaire: bool
    consultant_analysis: bool
    candidate_dossier: bool
    reference_checks: bool
    post_integration_days: list[int] = Field(default_factory=list)
    monthly_follow_up_months: Optional[int] = Field(default=None, ge=1)
    predictive_assessment: bool = False
    collaboration_report: bool = False
    role_video: bool = False
    additional_acquisition: Literal["none", "conditional_budget", "included"] = "none"
    option_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_features(self):
        if self.launch_minutes_max < self.launch_minutes_min:
            raise ValueError("launch maximum must be at least the minimum")
        if self.hunted_candidates_per_campaign is not None and self.hunt_campaign_count is None:
            raise ValueError("a candidate target requires a campaign count")
        return self


class GuaranteeDocumentation(StrictModel):
    inclusion_status: Literal["included", "optional", "conflicting", "not_documented"]
    period: Literal["initial_trial", "six_months", "not_documented"]
    source_note: Optional[str] = None


class CommercialOffer(StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    enabled_for_prospecting: bool
    is_default: bool = False
    features: OfferFeatures
    guarantee_documentation: GuaranteeDocumentation
    # Explicit per-field approval. Missing keys in older local catalogs remain
    # internal; a client document must be reviewed before enabling a claim.
    communication_scopes: dict[str, Literal[
        "internal_only", "client_communicable", "manual_approval_required",
    ]] = Field(default_factory=dict)
    claim_limitations: list[Literal[
        "hunt_target_not_response_or_presentation",
        "processing_deadline_not_hire_deadline",
        "replacement_effort_not_outcome",
        "claims_require_verified_reference",
    ]] = Field(default_factory=list)


class CommercialPolicy(StrictModel):
    usual_rate_min_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    usual_rate_max_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    negotiable_rate_min_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    negotiable_rate_max_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    discretionary_cap_eur_ex_vat: Optional[Decimal] = Field(default=None, ge=0)
    cap_only_for_nearby_amounts: bool = True
    rate_requires_manual_choice: bool = True
    discount_requires_manual_choice: bool = True
    cap_requires_manual_choice: bool = True
    client_price_requires_manual_approval: bool = True
    success_fee_trigger: Literal["effective_start", "other_agreed_terms"] = "effective_start"
    payment_term_days: Optional[int] = Field(default=None, ge=0)
    new_client_exclusive_by_default: bool = False
    existing_client_exclusivity_manual: bool = True
    guarantee_included: Optional[bool] = None
    guarantee_complimentary: Optional[bool] = None
    guarantee_reference_amount_eur_ex_vat: Optional[Decimal] = Field(default=None, ge=0)
    guarantee_restarts_agreed_efforts: bool = True
    guarantee_promises_result: bool = False
    contract_type_policy: dict[str, Literal["manual_review", "accepted", "declined"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_control(self):
        for low, high in ((self.usual_rate_min_percent, self.usual_rate_max_percent),
                          (self.negotiable_rate_min_percent, self.negotiable_rate_max_percent)):
            if low is not None and high is not None and low > high:
                raise ValueError("rate minimum exceeds maximum")
        if not (self.rate_requires_manual_choice and self.discount_requires_manual_choice
                and self.cap_requires_manual_choice and self.client_price_requires_manual_approval):
            raise ValueError("pricing decisions must remain manual")
        if self.guarantee_promises_result:
            raise ValueError("replacement guarantee cannot promise an outcome")
        if self.new_client_exclusive_by_default or not self.existing_client_exclusivity_manual:
            raise ValueError("exclusivity requires manual choice")
        return self


def read_profile(session: Session) -> Optional[CommercialProfile]:
    row = session.get(CommercialProfileRecord, 1)
    return CommercialProfile.model_validate(row.payload) if row else None


def save_profile(session: Session, profile: CommercialProfile) -> CommercialProfile:
    session.merge(CommercialProfileRecord(id=1, payload=profile.model_dump(mode="json")))
    session.commit()
    return profile


def list_offers(session: Session) -> list[CommercialOffer]:
    rows = session.scalars(select(CommercialCatalogOfferRecord).order_by(CommercialCatalogOfferRecord.code)).all()
    return [CommercialOffer.model_validate(row.payload) for row in rows]


def save_offer(session: Session, offer: CommercialOffer) -> CommercialOffer:
    if offer.is_default and not offer.enabled_for_prospecting:
        raise ValueError("the default offer must be enabled")
    if offer.is_default:
        for row in session.scalars(select(CommercialCatalogOfferRecord)).all():
            if row.code != offer.code and row.payload.get("is_default"):
                payload = dict(row.payload)
                payload["is_default"] = False
                row.payload = payload
    session.merge(CommercialCatalogOfferRecord(code=offer.code, payload=offer.model_dump(mode="json")))
    session.commit()
    return offer


def select_offer(session: Session, code: Optional[str] = None) -> Optional[CommercialOffer]:
    offers = list_offers(session)
    if code is not None:
        return next((offer for offer in offers if offer.code == code), None)
    return next((offer for offer in offers if offer.is_default and offer.enabled_for_prospecting), None)


def read_policy(session: Session) -> Optional[CommercialPolicy]:
    row = session.get(CommercialPolicyRecord, 1)
    return CommercialPolicy.model_validate(row.payload) if row else None


def save_policy(session: Session, policy: CommercialPolicy) -> CommercialPolicy:
    session.merge(CommercialPolicyRecord(id=1, payload=policy.model_dump(mode="json")))
    session.commit()
    return policy


def personal_specialties(profile: CommercialProfile) -> list[Specialty]:
    """Only personally asserted expertise may support a personal-specialist claim."""
    return [item for item in profile.specialties if item.expertise_scope == "personal"]


def approved_references(profile: CommercialProfile, scope: Literal["personal", "network"]) -> list[VerifiedReference]:
    """A specialty or marketing document never becomes a verified past mission."""
    return [item for item in profile.verified_references
            if item.approved_for_claims and item.evidence_scope == scope]


def resolve_exclusivity(policy: CommercialPolicy, manual_choice: Optional[bool] = None) -> bool:
    """A mission becomes exclusive only after an explicit consultant choice."""
    return manual_choice is True


def calculate_private_scenario(annual_gross: Decimal, selected_rate_percent: Optional[Decimal],
                               *, manually_selected_cap: Optional[Decimal] = None,
                               manually_selected_discount: Optional[Decimal] = None) -> Optional[Decimal]:
    """A private calculation only after an explicit rate; overrides are explicit inputs."""
    if selected_rate_percent is None:
        return None
    if annual_gross < 0 or not 0 <= selected_rate_percent <= 100:
        raise ValueError("invalid amount or rate")
    amount = annual_gross * selected_rate_percent / Decimal(100)
    if manually_selected_discount is not None:
        if not 0 <= manually_selected_discount <= amount:
            raise ValueError("invalid discount")
        amount -= manually_selected_discount
    if manually_selected_cap is not None:
        if manually_selected_cap < 0:
            raise ValueError("invalid cap")
        amount = min(amount, manually_selected_cap)
    return amount.quantize(Decimal("0.01"))
