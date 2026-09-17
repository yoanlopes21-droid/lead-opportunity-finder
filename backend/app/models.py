from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TimestampedModel:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Company(TimestampedModel, Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(255), index=True)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    siren: Mapped[Optional[str]] = mapped_column(String(9), unique=True, index=True)
    website_url: Mapped[Optional[str]] = mapped_column(String(2048))
    industry_code: Mapped[Optional[str]] = mapped_column(String(20))
    industry_label: Mapped[Optional[str]] = mapped_column(String(255))
    employee_range: Mapped[Optional[str]] = mapped_column(String(50))


class CompanyLocation(TimestampedModel, Base):
    __tablename__ = "company_locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    siret: Mapped[Optional[str]] = mapped_column(String(14), unique=True, index=True)
    address: Mapped[Optional[str]] = mapped_column(Text)
    postal_code: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    city: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    department_code: Mapped[Optional[str]] = mapped_column(String(3), index=True)


class JobOffer(TimestampedModel, Base):
    __tablename__ = "job_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(120), index=True)
    source_offer_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    location_text: Mapped[Optional[str]] = mapped_column(String(500))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(30), default="observed")
    __table_args__ = (UniqueConstraint("source_name", "source_offer_id", name="uq_job_offer_source_id"),)


class CollectionRun(Base):
    """A bounded source collection, used to distinguish complete runs from failures."""

    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(120), index=True)
    scope_type: Mapped[str] = mapped_column(String(50))
    scope_value: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    is_full_scope: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    offers_received: Mapped[int] = mapped_column(Integer, default=0)
    offers_new: Mapped[int] = mapped_column(Integer, default=0)
    offers_updated: Mapped[int] = mapped_column(Integer, default=0)
    offers_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    offers_skipped: Mapped[int] = mapped_column(Integer, default=0)
    offers_deactivated: Mapped[int] = mapped_column(Integer, default=0)


class ObservedJobOffer(Base):
    """A source-independent offer snapshot with local observation metadata."""

    __tablename__ = "observed_job_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(120), index=True)
    source_offer_id: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    company_name: Mapped[Optional[str]] = mapped_column(String(500), index=True)
    location_label: Mapped[Optional[str]] = mapped_column(String(500))
    commune: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    department_code: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    created_at: Mapped[Optional[str]] = mapped_column(String(64))
    updated_at: Mapped[Optional[str]] = mapped_column(String(64))
    contract_type: Mapped[Optional[str]] = mapped_column(String(100))
    salary: Mapped[Optional[str]] = mapped_column(String(500))
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    origin: Mapped[Optional[str]] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("collection_runs.id"), index=True)
    __table_args__ = (
        UniqueConstraint("source", "source_offer_id", name="uq_observed_job_offer_source_id"),
    )


class EnrichmentRun(Base):
    """One auditable batch of source-independent company enrichments."""

    __tablename__ = "enrichment_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(120), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    high_confidence_count: Mapped[int] = mapped_column(Integer, default=0)
    review_needed_count: Mapped[int] = mapped_column(Integer, default=0)
    ambiguous_count: Mapped[int] = mapped_column(Integer, default=0)
    generic_count: Mapped[int] = mapped_column(Integer, default=0)
    not_found_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)


class CompanyEnrichment(Base):
    """Latest enrichment state for one internal company key and provider."""

    __tablename__ = "company_enrichments"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_key: Mapped[str] = mapped_column(String(500), index=True)
    source_company_name: Mapped[str] = mapped_column(String(500))
    provider: Mapped[str] = mapped_column(String(120), index=True)
    match_status: Mapped[str] = mapped_column(String(40), index=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    entity_sector_type: Mapped[str] = mapped_column(String(20), default="unknown", index=True)

    # Confirmed legal identity: populated only for matched_high_confidence.
    siren: Mapped[Optional[str]] = mapped_column(String(9), index=True)
    siret: Mapped[Optional[str]] = mapped_column(String(14))
    official_name: Mapped[Optional[str]] = mapped_column(String(500))
    address: Mapped[Optional[str]] = mapped_column(Text)
    postal_code: Mapped[Optional[str]] = mapped_column(String(10))
    commune: Mapped[Optional[str]] = mapped_column(String(255))
    naf_code: Mapped[Optional[str]] = mapped_column(String(20))
    activity_label: Mapped[Optional[str]] = mapped_column(String(500))
    legal_nature: Mapped[Optional[str]] = mapped_column(String(50))
    employee_range: Mapped[Optional[str]] = mapped_column(String(50))
    administrative_status: Mapped[Optional[str]] = mapped_column(String(20))

    # A proposal requiring review is never exposed as confirmed identity.
    suggested_siren: Mapped[Optional[str]] = mapped_column(String(9))
    suggested_name: Mapped[Optional[str]] = mapped_column(String(500))
    suggested_score: Mapped[Optional[float]] = mapped_column(Float)

    enriched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    provider_source: Mapped[Optional[str]] = mapped_column(String(2048))
    input_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    last_error_type: Mapped[Optional[str]] = mapped_column(String(100))
    last_error_message: Mapped[Optional[str]] = mapped_column(String(500))
    last_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("enrichment_runs.id"), index=True)
    __table_args__ = (
        UniqueConstraint("company_key", "provider", name="uq_company_enrichment_key_provider"),
    )


class CompanyEnrichmentDetail(Base):
    """Provider-neutral explanation attached to the latest enrichment state."""

    __tablename__ = "company_enrichment_details"
    id: Mapped[int] = mapped_column(primary_key=True)
    enrichment_id: Mapped[int] = mapped_column(
        ForeignKey("company_enrichments.id"), unique=True, index=True
    )
    match_reasons: Mapped[list] = mapped_column(JSON, default=list)
    match_signals: Mapped[list] = mapped_column(JSON, default=list)
    suggested_commune: Mapped[Optional[str]] = mapped_column(String(255))
    suggested_postal_code: Mapped[Optional[str]] = mapped_column(String(10))
    suggested_entity_sector_type: Mapped[Optional[str]] = mapped_column(String(20))
    candidate_aliases: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EnrichmentRunItem(Base):
    """Durable membership and progress state for one enrichment batch item."""

    __tablename__ = "enrichment_run_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("enrichment_runs.id"), index=True)
    company_key: Mapped[str] = mapped_column(String(500), index=True)
    source_company_name: Mapped[str] = mapped_column(String(500))
    selection_position: Mapped[int] = mapped_column(Integer)
    input_snapshot: Mapped[dict] = mapped_column(JSON)
    input_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    match_status: Mapped[Optional[str]] = mapped_column(String(40))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    enrichment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("company_enrichments.id"), index=True
    )
    last_error_type: Mapped[Optional[str]] = mapped_column(String(100))
    last_error_message: Mapped[Optional[str]] = mapped_column(String(500))
    __table_args__ = (
        UniqueConstraint("run_id", "company_key", name="uq_enrichment_run_item_company"),
        UniqueConstraint("run_id", "selection_position", name="uq_enrichment_run_item_position"),
    )


class Contact(TimestampedModel, Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[Optional[str]] = mapped_column(String(255))
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(2048))
    professional_email: Mapped[Optional[str]] = mapped_column(String(320))
    phone: Mapped[Optional[str]] = mapped_column(String(50))


class Evidence(TimestampedModel, Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    job_offer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_offers.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(120), index=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    field_name: Mapped[str] = mapped_column(String(120))
    observed_value: Mapped[Optional[str]] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Signal(TimestampedModel, Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    job_offer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_offers.id"), index=True)
    signal_type: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SearchRun(TimestampedModel, Base):
    __tablename__ = "search_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    territory: Mapped[str] = mapped_column(String(50), default="94")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    companies_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    opportunities_detected: Mapped[int] = mapped_column(Integer, default=0)
    qualified_leads: Mapped[int] = mapped_column(Integer, default=0)


class LeadAssessment(TimestampedModel, Base):
    __tablename__ = "lead_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    search_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("search_runs.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    priority: Mapped[str] = mapped_column(String(30), index=True)
    score_explanation: Mapped[dict] = mapped_column(JSON, default=dict)
    recommended_channel: Mapped[Optional[str]] = mapped_column(String(50))


class Exclusion(TimestampedModel, Base):
    __tablename__ = "exclusions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    exclusion_type: Mapped[str] = mapped_column(String(50))
    last_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)


class CommercialExclusion(Base):
    """An additive, auditable local exclusion for commercial lead eligibility."""

    __tablename__ = "commercial_exclusions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_key: Mapped[str] = mapped_column(String(500), index=True)
    siren: Mapped[Optional[str]] = mapped_column(String(9), index=True)
    company_name_snapshot: Mapped[str] = mapped_column(String(500))
    exclusion_type: Mapped[str] = mapped_column(String(50), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PersonContact(Base):
    """A sourced professional person candidate; contact methods live in ContactPoint."""

    __tablename__ = "person_contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_key: Mapped[str] = mapped_column(String(500), index=True)
    scope: Mapped[str] = mapped_column(String(30), index=True)
    local_key: Mapped[Optional[str]] = mapped_column(String(500), index=True)
    siren: Mapped[Optional[str]] = mapped_column(String(9), index=True)
    organization_name_snapshot: Mapped[str] = mapped_column(String(500))
    local_commune_snapshot: Mapped[Optional[str]] = mapped_column(String(255))
    local_location_label_snapshot: Mapped[Optional[str]] = mapped_column(String(500))
    full_name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    job_title: Mapped[Optional[str]] = mapped_column(String(255))
    relevance_role: Mapped[str] = mapped_column(String(30), index=True)
    confidence_level: Mapped[str] = mapped_column(String(30), index=True)
    verification_status: Mapped[str] = mapped_column(String(30), index=True)
    attribution_reason: Mapped[Optional[str]] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    __table_args__ = (
        CheckConstraint("scope != 'local' OR local_key IS NOT NULL", name="ck_person_contact_local_scope_key"),
    )


class ContactPoint(Base):
    """A non-secret public contact method, independently sourced and scoped."""

    __tablename__ = "contact_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_key: Mapped[str] = mapped_column(String(500), index=True)
    scope: Mapped[str] = mapped_column(String(30), index=True)
    local_key: Mapped[Optional[str]] = mapped_column(String(500), index=True)
    siren: Mapped[Optional[str]] = mapped_column(String(9), index=True)
    organization_name_snapshot: Mapped[str] = mapped_column(String(500))
    local_commune_snapshot: Mapped[Optional[str]] = mapped_column(String(255))
    local_location_label_snapshot: Mapped[Optional[str]] = mapped_column(String(500))
    contact_type: Mapped[str] = mapped_column(String(30), index=True)
    value: Mapped[str] = mapped_column(String(2048))
    normalized_value: Mapped[str] = mapped_column(String(2048), index=True)
    confidence_level: Mapped[str] = mapped_column(String(30), index=True)
    verification_status: Mapped[str] = mapped_column(String(30), index=True)
    attribution_reason: Mapped[Optional[str]] = mapped_column(Text)
    person_contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("person_contacts.id"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    __table_args__ = (
        CheckConstraint("scope != 'local' OR local_key IS NOT NULL", name="ck_contact_point_local_scope_key"),
    )


class ContactEvidence(Base):
    """Minimal provenance for exactly one contact point or person candidate."""

    __tablename__ = "contact_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_point_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("contact_points.id"), index=True
    )
    person_contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("person_contacts.id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(120), index=True)
    source_name: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    source_identifier: Mapped[Optional[str]] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    evidence_reason: Mapped[Optional[str]] = mapped_column(Text)
    excerpt: Mapped[Optional[str]] = mapped_column(String(1000))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    __table_args__ = (
        CheckConstraint(
            "(contact_point_id IS NOT NULL AND person_contact_id IS NULL) "
            "OR (contact_point_id IS NULL AND person_contact_id IS NOT NULL)",
            name="ck_contact_evidence_exactly_one_target",
        ),
    )
