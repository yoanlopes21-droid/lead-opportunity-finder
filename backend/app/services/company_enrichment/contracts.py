"""Provider-neutral contracts for company enrichment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

from app.services.opportunities.company import CompanyOpportunity


class MatchStatus:
    HIGH_CONFIDENCE = "matched_high_confidence"
    REVIEW_NEEDED = "matched_review_needed"
    AMBIGUOUS = "ambiguous"
    GENERIC = "generic_or_intermediary"
    NOT_FOUND = "not_found"
    ERROR = "error"


VALID_MATCH_STATUSES = {
    MatchStatus.HIGH_CONFIDENCE,
    MatchStatus.REVIEW_NEEDED,
    MatchStatus.AMBIGUOUS,
    MatchStatus.GENERIC,
    MatchStatus.NOT_FOUND,
    MatchStatus.ERROR,
}
VALID_SECTOR_TYPES = {"private", "public", "nonprofit", "unknown"}


@dataclass(frozen=True)
class LegalIdentity:
    siren: Optional[str] = None
    siret: Optional[str] = None
    official_name: Optional[str] = None
    address: Optional[str] = None
    postal_code: Optional[str] = None
    commune: Optional[str] = None
    naf_code: Optional[str] = None
    activity_label: Optional[str] = None
    legal_nature: Optional[str] = None
    employee_range: Optional[str] = None
    administrative_status: Optional[str] = None


@dataclass(frozen=True)
class ProviderEnrichmentResult:
    status: str
    confidence_score: Optional[float] = None
    entity_sector_type: str = "unknown"
    confirmed_identity: Optional[LegalIdentity] = None
    suggested_identity: Optional[LegalIdentity] = None
    provider_source: Optional[str] = None


class ProviderCallError(Exception):
    """Sanitised provider failure safe to persist and expose in run metadata."""

    def __init__(self, error_type: str, transient: bool, message: Optional[str] = None):
        del message  # Raw provider messages are intentionally never retained.
        safe_type = re.sub(r"[^a-zA-Z0-9_.-]", "_", error_type)[:100] or "unknown"
        self.error_type = safe_type
        self.transient = transient
        self.safe_message = f"Provider request failed ({safe_type})."
        super().__init__(self.safe_message)


class CompanyEnrichmentProvider(Protocol):
    name: str
    source: str

    def enrich(self, opportunity: CompanyOpportunity) -> ProviderEnrichmentResult:
        ...
