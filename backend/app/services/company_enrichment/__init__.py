"""Source-independent company identity enrichment services."""

from app.services.company_enrichment.dinum import (
    CompanyEnrichmentResult,
    DinumCompanySearchClient,
    DinumSearchError,
    enrich_company_opportunity,
)

__all__ = [
    "CompanyEnrichmentResult",
    "DinumCompanySearchClient",
    "DinumSearchError",
    "enrich_company_opportunity",
]
