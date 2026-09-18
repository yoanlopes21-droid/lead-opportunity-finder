"""Typed, minimal artifacts for official website discovery and verification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class WebsiteCandidateClassification:
    POTENTIAL_OFFICIAL = "potential_official"
    STRUCTURED_SOURCE = "structured_source"
    CACHED_VERIFIED = "cached_verified"
    EXCLUDED = "excluded"


class WebsiteVerificationStatus:
    HIGH_CONFIDENCE = "high_confidence"
    REVIEW_NEEDED = "review_needed"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"


class SignalPolarity:
    POSITIVE = "positive"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class WebsiteSeed:
    url: str
    source_provider: str
    observed_at: datetime
    source_url: Optional[str] = None
    title: Optional[str] = None
    snippet: Optional[str] = None
    structured: bool = False


@dataclass(frozen=True)
class WebsiteCandidate:
    company_key: str
    target_scope: str
    local_key: Optional[str]
    target_fingerprint: str
    query_fingerprint: str
    search_provider: str
    query_index: int
    result_rank: int
    url: str
    canonical_url: str
    registrable_domain: str
    title: Optional[str]
    snippet: Optional[str]
    observed_at: datetime
    classification: str
    rejection_reasons: tuple[str, ...]
    candidate_fingerprint: str


@dataclass(frozen=True)
class WebsiteVerificationSignal:
    signal_type: str
    polarity: str
    weight: int
    reason: str
    source_url: str
    excerpt: Optional[str]
    observed_value: Optional[str]
    expected_value: Optional[str]
    observed_at: datetime


@dataclass(frozen=True)
class VerifiedOfficialSite:
    company_key: str
    target_scope: str
    local_key: Optional[str]
    target_fingerprint: str
    candidate_fingerprint: str
    candidate_set_fingerprint: str
    provider: str
    canonical_url: str
    registrable_domain: str
    status: str
    score: int
    rejection_reasons: tuple[str, ...]
    attribution_warnings: tuple[str, ...]
    observed_at: datetime
    verified_at: datetime
    signals: tuple[WebsiteVerificationSignal, ...]
    fingerprint: str


@dataclass(frozen=True)
class BraveSearchResult:
    url: str
    title: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    links: tuple[str, ...]
    fetched_at: datetime
