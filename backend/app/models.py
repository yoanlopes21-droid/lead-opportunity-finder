from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
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
