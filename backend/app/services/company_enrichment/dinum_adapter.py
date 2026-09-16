"""Adapter from the DINUM-specific matcher to generic enrichment contracts."""

from __future__ import annotations

from typing import Optional

from app.services.company_enrichment.contracts import (
    LegalIdentity,
    MatchStatus,
    ProviderCallError,
    ProviderEnrichmentResult,
)
from app.services.company_enrichment.dinum import (
    DINUM_SEARCH_URL,
    CompanyCandidate,
    DinumCompanySearchClient,
    DinumSearchError,
    enrich_company_opportunity,
)
from app.services.opportunities.company import CompanyOpportunity


class DinumCompanyEnrichmentProvider:
    name = "dinum"
    source = DINUM_SEARCH_URL

    def __init__(self, client: DinumCompanySearchClient):
        self._client = client

    def enrich(self, opportunity: CompanyOpportunity) -> ProviderEnrichmentResult:
        try:
            result = enrich_company_opportunity(opportunity, self._client)
        except DinumSearchError as error:
            raise ProviderCallError(
                error_type=error.kind,
                transient=_is_transient(error.kind),
            ) from error

        best_assessment = result.candidate_assessments[0] if result.candidate_assessments else None
        score = float(best_assessment.score) if best_assessment else None
        confirmed = (
            _identity(result.matched_candidate)
            if result.status == MatchStatus.HIGH_CONFIDENCE
            else None
        )
        suggested = (
            _identity(best_assessment.candidate)
            if result.status in {MatchStatus.REVIEW_NEEDED, MatchStatus.AMBIGUOUS}
            and best_assessment
            else None
        )
        sector_candidate = result.matched_candidate or (
            best_assessment.candidate if best_assessment else None
        )
        return ProviderEnrichmentResult(
            status=result.status,
            confidence_score=score,
            entity_sector_type=(
                sector_candidate.entity_sector_type if sector_candidate else "unknown"
            ),
            confirmed_identity=confirmed,
            suggested_identity=suggested,
            provider_source=self.source,
        )


def _identity(candidate: Optional[CompanyCandidate]) -> Optional[LegalIdentity]:
    if candidate is None:
        return None
    return LegalIdentity(
        siren=candidate.siren,
        siret=candidate.siret,
        official_name=candidate.official_name,
        address=candidate.address,
        postal_code=candidate.postal_code,
        commune=candidate.commune_name,
        naf_code=candidate.naf_code,
        activity_label=candidate.activity_label,
        legal_nature=candidate.legal_category,
        employee_range=candidate.employee_bracket,
        administrative_status=candidate.administrative_status,
    )


def _is_transient(error_type: str) -> bool:
    return error_type in {"timeout", "network_error", "rate_limited"} or (
        error_type.startswith("http_5")
    )
