"""Source-independent company identity enrichment services."""

from app.services.company_enrichment.dinum import (
    CompanyEnrichmentResult,
    DinumCompanySearchClient,
    DinumSearchError,
    enrich_company_opportunity,
)
from app.services.company_enrichment.dinum_adapter import DinumCompanyEnrichmentProvider

__all__ = [
    "CompanyEnrichmentResult",
    "DinumCompanySearchClient",
    "DinumSearchError",
    "DinumCompanyEnrichmentProvider",
    "enrich_company_opportunity",
]
