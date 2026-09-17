"""Explicit, provider-neutral contracts for sourced professional contacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, Sequence


class ContactScope:
    COMPANY = "company"
    LOCAL = "local"
    INTERMEDIARY = "intermediary"
    UNKNOWN = "unknown"


VALID_CONTACT_SCOPES = {
    ContactScope.COMPANY,
    ContactScope.LOCAL,
    ContactScope.INTERMEDIARY,
    ContactScope.UNKNOWN,
}


class ContactType:
    WEBSITE = "website"
    PHONE = "phone"
    EMAIL = "email"
    PROFESSIONAL_URL = "professional_url"
    OTHER = "other"


VALID_CONTACT_TYPES = {
    ContactType.WEBSITE,
    ContactType.PHONE,
    ContactType.EMAIL,
    ContactType.PROFESSIONAL_URL,
    ContactType.OTHER,
}


class ContactConfidence:
    CONFIRMED = "confirmed"
    HIGH_CONFIDENCE = "high_confidence"
    REVIEW_NEEDED = "review_needed"
    AMBIGUOUS = "ambiguous"


VALID_CONTACT_CONFIDENCES = {
    ContactConfidence.CONFIRMED,
    ContactConfidence.HIGH_CONFIDENCE,
    ContactConfidence.REVIEW_NEEDED,
    ContactConfidence.AMBIGUOUS,
}


class VerificationStatus:
    UNVERIFIED = "unverified"
    SOURCE_VERIFIED = "source_verified"
    MANUALLY_VERIFIED = "manually_verified"
    STALE = "stale"
    REJECTED = "rejected"


VALID_VERIFICATION_STATUSES = {
    VerificationStatus.UNVERIFIED,
    VerificationStatus.SOURCE_VERIFIED,
    VerificationStatus.MANUALLY_VERIFIED,
    VerificationStatus.STALE,
    VerificationStatus.REJECTED,
}


class PersonRelevanceRole:
    HR = "hr"
    RECRUITMENT = "recruitment"
    DIRECTOR = "director"
    MANAGER = "manager"
    OTHER = "other"


VALID_PERSON_RELEVANCE_ROLES = {
    PersonRelevanceRole.HR,
    PersonRelevanceRole.RECRUITMENT,
    PersonRelevanceRole.DIRECTOR,
    PersonRelevanceRole.MANAGER,
    PersonRelevanceRole.OTHER,
}


@dataclass(frozen=True)
class ContactTarget:
    """A non-juridical target context used before contact discovery."""

    company_key: str
    organization_name_snapshot: str
    scope: str
    siren: Optional[str]
    local_key: Optional[str]
    local_commune_snapshot: Optional[str]
    local_location_label_snapshot: Optional[str]
    employer_relationship_status: str
    identity_match_status: Optional[str]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContactPointInput:
    company_key: str
    organization_name_snapshot: str
    scope: str
    contact_type: str
    value: str
    confidence_level: str
    verification_status: str
    observed_at: datetime
    local_key: Optional[str] = None
    siren: Optional[str] = None
    local_commune_snapshot: Optional[str] = None
    local_location_label_snapshot: Optional[str] = None
    attribution_reason: Optional[str] = None
    person_contact_id: Optional[int] = None


@dataclass(frozen=True)
class PersonContactInput:
    company_key: str
    organization_name_snapshot: str
    scope: str
    full_name: str
    relevance_role: str
    confidence_level: str
    verification_status: str
    observed_at: datetime
    local_key: Optional[str] = None
    siren: Optional[str] = None
    local_commune_snapshot: Optional[str] = None
    local_location_label_snapshot: Optional[str] = None
    job_title: Optional[str] = None
    attribution_reason: Optional[str] = None


@dataclass(frozen=True)
class ContactEvidenceInput:
    provider: str
    source_name: str
    observed_at: datetime
    source_url: Optional[str] = None
    source_identifier: Optional[str] = None
    evidence_reason: Optional[str] = None
    excerpt: Optional[str] = None


class ContactProviderStatus:
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    NOT_APPLICABLE = "not_applicable"
    NOT_CONFIGURED = "not_configured"
    ERROR = "error"


VALID_CONTACT_PROVIDER_STATUSES = {
    ContactProviderStatus.COMPLETED,
    ContactProviderStatus.NOT_FOUND,
    ContactProviderStatus.NOT_APPLICABLE,
    ContactProviderStatus.NOT_CONFIGURED,
    ContactProviderStatus.ERROR,
}


@dataclass(frozen=True)
class ContactEvidenceCandidate:
    provider: str
    source_name: str
    observed_at: datetime
    source_url: Optional[str] = None
    source_identifier: Optional[str] = None
    evidence_reason: Optional[str] = None
    excerpt: Optional[str] = None


@dataclass(frozen=True)
class ContactPointCandidate:
    company_key: str
    organization_name_snapshot: str
    scope: str
    contact_type: str
    value: str
    normalized_value: str
    confidence_level: str
    verification_status: str
    attribution_reason: Optional[str]
    local_key: Optional[str] = None
    siren: Optional[str] = None
    local_commune_snapshot: Optional[str] = None
    local_location_label_snapshot: Optional[str] = None
    evidence: tuple[ContactEvidenceCandidate, ...] = ()


@dataclass(frozen=True)
class PersonContactCandidate:
    company_key: str
    organization_name_snapshot: str
    scope: str
    full_name: str
    normalized_name: str
    relevance_role: str
    confidence_level: str
    verification_status: str
    attribution_reason: Optional[str]
    job_title: Optional[str] = None
    local_key: Optional[str] = None
    siren: Optional[str] = None
    local_commune_snapshot: Optional[str] = None
    local_location_label_snapshot: Optional[str] = None
    evidence: tuple[ContactEvidenceCandidate, ...] = ()


@dataclass(frozen=True)
class ContactProviderAttemptMetadata:
    target_fingerprint: str
    attempted_at: Optional[datetime]
    request_count: int = 0
    error_type: Optional[str] = None


@dataclass(frozen=True)
class ContactProviderResult:
    provider: str
    status: str
    candidates: tuple[ContactPointCandidate, ...] = ()
    person_candidates: tuple[PersonContactCandidate, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: Optional[ContactProviderAttemptMetadata] = None


class ContactProvider(Protocol):
    name: str

    def discover(
        self, target: ContactTarget, offers: Sequence["OfferContactSource"]
    ) -> ContactProviderResult:
        ...


class OfferContactSource(Protocol):
    source: str
    source_offer_id: str
    description: Optional[str]
    source_url: Optional[str]
    company_name: Optional[str]
    commune: Optional[str]
    location_label: Optional[str]
    last_seen_at: datetime
