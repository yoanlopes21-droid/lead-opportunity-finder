"""Conservative, in-memory matching against DINUM Recherche d'Entreprises.

The public API is used only to propose an identity.  A result is never written
to the local database here, and uncertain identities deliberately remain
unmatched for human review.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping, Optional, Sequence

import httpx

from app.services.opportunities.company import CompanyOpportunity, normalize_company_key


DINUM_SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"
# The published limit is 7 requests/s/IP. Two requests/s leaves a wide margin.
MAX_REQUESTS_PER_SECOND = 2
_REQUEST_INTERVAL_SECONDS = 1 / MAX_REQUESTS_PER_SECOND


class DinumSearchError(Exception):
    """A controlled failure returned by the public company-search API."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"DINUM company search failed ({kind}).")


@dataclass(frozen=True)
class CompanyCandidate:
    """Optional fields normalised from one DINUM search result."""

    siren: Optional[str]
    siret: Optional[str]
    official_name: Optional[str]
    aliases: tuple[str, ...]
    address: Optional[str]
    postal_code: Optional[str]
    commune_code: Optional[str]
    commune_name: Optional[str]
    naf_code: Optional[str]
    activity_label: Optional[str]
    employee_bracket_raw: Optional[str]
    employee_bracket: str
    legal_category: Optional[str]
    administrative_status: Optional[str]
    is_administration: Optional[bool]
    is_collectivity: Optional[bool]
    is_association: Optional[bool]
    is_ess: Optional[bool]
    entity_sector_type: str


@dataclass(frozen=True)
class CandidateAssessment:
    candidate: CompanyCandidate
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CompanyEnrichmentResult:
    """A non-persistent result suitable for a later review or persistence step."""

    source_company_name: str
    status: str
    matched_candidate: Optional[CompanyCandidate]
    candidate_assessments: tuple[CandidateAssessment, ...]
    reasons: tuple[str, ...]


class DinumCompanySearchClient:
    """Small public, unauthenticated DINUM client with conservative pacing."""

    def __init__(
        self,
        base_url: str = DINUM_SEARCH_URL,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: Optional[float] = None

    def search(
        self,
        query: str,
        department_code: str = "94",
        postal_code: Optional[str] = None,
    ) -> tuple[CompanyCandidate, ...]:
        """Search candidates; the caller decides whether any candidate is a match.

        A lone postal code is safe to forward as a remote filter. Commune values
        from job boards are deliberately evaluated locally so an imprecise board
        locality cannot hide a valid legal entity.
        """
        cleaned_query = query.strip()
        if not cleaned_query:
            return ()
        params: dict[str, str | int] = {
            "q": cleaned_query,
            "departement": department_code,
            "per_page": 10,
        }
        if postal_code:
            params["code_postal"] = postal_code
        self._respect_rate_limit()
        try:
            response = httpx.get(
                self._base_url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Lead-Opportunity-Finder/0.1 (local company enrichment)",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise DinumSearchError("timeout") from exc
        except httpx.RequestError as exc:
            raise DinumSearchError("network_error") from exc

        if response.status_code == 429:
            raise DinumSearchError("rate_limited")
        if response.status_code >= 400:
            raise DinumSearchError(f"http_{response.status_code}")
        if response.status_code == 204:
            return ()
        try:
            payload = response.json()
        except ValueError as exc:
            raise DinumSearchError("invalid_json") from exc
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if results is None:
            return ()
        if not isinstance(results, list):
            raise DinumSearchError("invalid_payload")
        return tuple(normalize_candidate(item) for item in results if isinstance(item, Mapping))

    def _respect_rate_limit(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            delay = _REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if delay > 0:
                self._sleeper(delay)
                now = self._clock()
        self._last_request_at = now


def enrich_company_opportunity(
    opportunity: CompanyOpportunity,
    client: DinumCompanySearchClient,
    postal_code: Optional[str] = None,
) -> CompanyEnrichmentResult:
    """Search then conservatively assess a company opportunity in memory."""
    generic_reason = _generic_or_intermediary_reason(opportunity.company_name)
    if generic_reason:
        return CompanyEnrichmentResult(
            source_company_name=opportunity.company_name,
            status="generic_or_intermediary",
            matched_candidate=None,
            candidate_assessments=(),
            reasons=(generic_reason,),
        )
    candidates = client.search(
        opportunity.company_name, opportunity.department_code, postal_code=postal_code
    )
    return assess_company_candidates(opportunity, candidates, postal_code=postal_code)


def assess_company_candidates(
    opportunity: CompanyOpportunity,
    candidates: Sequence[CompanyCandidate],
    postal_code: Optional[str] = None,
) -> CompanyEnrichmentResult:
    """Apply explainable match rules without assuming API ordering is reliable."""
    generic_reason = _generic_or_intermediary_reason(opportunity.company_name)
    if generic_reason:
        return CompanyEnrichmentResult(
            source_company_name=opportunity.company_name,
            status="generic_or_intermediary",
            matched_candidate=None,
            candidate_assessments=(),
            reasons=(generic_reason,),
        )
    assessments = tuple(sorted(
        (_assess_candidate(opportunity, candidate, postal_code) for candidate in candidates),
        key=lambda item: item.score,
        reverse=True,
    ))
    if not assessments:
        return CompanyEnrichmentResult(opportunity.company_name, "not_found", None, (), ("Aucun candidat DINUM retourné.",))
    best = assessments[0]
    next_score = assessments[1].score if len(assessments) > 1 else None
    explicit_alias_sirens = {
        assessment.candidate.siren
        for assessment in assessments
        if assessment.score >= 70
        and any(reason.startswith("Alias officiel explicite") for reason in assessment.reasons)
        and assessment.candidate.siren
    }
    if len(explicit_alias_sirens) > 1:
        return CompanyEnrichmentResult(
            opportunity.company_name,
            "ambiguous",
            None,
            assessments,
            ("La marque ou l'alias explicite correspond à plusieurs unités légales plausibles.",),
        )
    if best.score >= 90 and (next_score is None or best.score - next_score >= 15):
        return CompanyEnrichmentResult(
            opportunity.company_name,
            "matched_high_confidence",
            best.candidate,
            assessments,
            best.reasons,
        )
    if best.score >= 70 and (next_score is None or best.score - next_score >= 15):
        return CompanyEnrichmentResult(
            opportunity.company_name,
            "matched_review_needed",
            None,
            assessments,
            best.reasons + ("Correspondance plausible à vérifier avant d'attribuer un SIREN.",),
        )
    if best.score >= 50:
        return CompanyEnrichmentResult(
            opportunity.company_name,
            "ambiguous",
            None,
            assessments,
            ("Plusieurs candidats plausibles ou preuves géographiques insuffisantes.",),
        )
    return CompanyEnrichmentResult(
        opportunity.company_name,
        "not_found",
        None,
        assessments,
        ("Aucun candidat ne présente une correspondance suffisamment solide.",),
    )


def normalize_candidate(payload: Mapping[str, Any]) -> CompanyCandidate:
    """Normalise les structures de réponse courantes sans rendre un champ obligatoire."""
    establishment = _select_establishment(payload)
    complements = payload.get("complements")
    complements = complements if isinstance(complements, Mapping) else {}
    postal_code = _string(establishment.get("code_postal")) or _string(payload.get("code_postal"))
    commune_code = _string(establishment.get("commune")) or _string(payload.get("commune"))
    commune_name = _string(establishment.get("libelle_commune")) or _string(payload.get("libelle_commune"))
    legal_category = _string(payload.get("nature_juridique"))
    is_administration = _bool(complements.get("est_administration"))
    is_collectivity = True if isinstance(complements.get("collectivite_territoriale"), Mapping) else None
    is_association = _bool(complements.get("est_association"))
    is_ess = _bool(complements.get("est_ess"))
    association_id = _string(complements.get("identifiant_association"))
    return CompanyCandidate(
        siren=_string(payload.get("siren")),
        siret=_string(establishment.get("siret")) or _string(payload.get("siret")),
        official_name=_first_string(payload, "nom_complet", "nom_raison_sociale", "denomination", "nom"),
        aliases=_candidate_aliases(payload),
        address=_string(establishment.get("adresse")) or _string(payload.get("adresse")),
        postal_code=postal_code,
        commune_code=commune_code,
        commune_name=commune_name,
        naf_code=_string(payload.get("activite_principale")) or _string(establishment.get("activite_principale")),
        activity_label=_string(payload.get("libelle_activite_principale")),
        employee_bracket_raw=_string(payload.get("tranche_effectif_salarie")) or _string(establishment.get("tranche_effectif_salarie")),
        employee_bracket=normalize_employee_bracket(_string(payload.get("tranche_effectif_salarie")) or _string(establishment.get("tranche_effectif_salarie"))),
        legal_category=legal_category,
        administrative_status=_string(payload.get("etat_administratif")),
        is_administration=is_administration,
        is_collectivity=is_collectivity,
        is_association=is_association,
        is_ess=is_ess,
        entity_sector_type=_entity_sector_type(
            is_administration,
            is_collectivity,
            is_association,
            association_id,
            legal_category,
        ),
    )


def normalize_employee_bracket(value: Optional[str]) -> str:
    mapping = {
        "00": "0", "01": "1-2", "02": "3-5", "03": "6-9", "11": "10-19",
        "12": "20-49", "21": "50-99", "22": "100-199", "31": "200-249",
        "32": "250+", "41": "250+", "42": "250+", "51": "250+", "52": "250+", "53": "250+",
    }
    if not value:
        return "unknown"
    return mapping.get(value.strip(), value.strip() if value.strip() in set(mapping.values()) else "unknown")


def _assess_candidate(opportunity: CompanyOpportunity, candidate: CompanyCandidate, postal_code: Optional[str]) -> CandidateAssessment:
    score = 0
    reasons: list[str] = []
    source_key = normalize_company_key(opportunity.company_name)
    candidate_key = normalize_company_key(candidate.official_name)
    alias_keys = {normalize_company_key(alias) for alias in candidate.aliases}
    alias_keys.discard(None)
    if source_key and source_key == candidate_key:
        score += 70; reasons.append("Dénomination normalisée identique (+70).")
    elif source_key and source_key in alias_keys:
        score += 70; reasons.append("Alias officiel explicite identique (sigle, nom commercial ou enseigne) (+70).")
    elif source_key and candidate_key:
        similarity = SequenceMatcher(None, source_key, candidate_key).ratio()
        if similarity >= .90:
            score += 55; reasons.append("Dénomination très similaire (+55).")
        elif similarity >= .75:
            score += 35; reasons.append("Dénomination similaire (+35).")
    if candidate.commune_code and candidate.commune_code in opportunity.communes:
        score += 20; reasons.append("Commune INSEE identique (+20).")
    elif candidate.commune_name and _normal_text(candidate.commune_name) in {_normal_text(v) for v in opportunity.location_labels}:
        score += 15; reasons.append("Libellé de commune cohérent (+15).")
    if postal_code and candidate.postal_code == postal_code:
        score += 20; reasons.append("Code postal identique (+20).")
    if _candidate_in_department(candidate, opportunity.department_code):
        score += 10; reasons.append("Établissement situé dans le département demandé (+10).")
    return CandidateAssessment(candidate, score, tuple(reasons))


def _select_establishment(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    establishments = payload.get("matching_etablissements")
    if isinstance(establishments, list):
        for item in establishments:
            if isinstance(item, Mapping):
                return item
    siege = payload.get("siege")
    return siege if isinstance(siege, Mapping) else {}


def _candidate_aliases(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return only aliases explicitly present in fields documented by DINUM."""
    values: list[str] = []
    sigle = _string(payload.get("sigle"))
    if sigle:
        values.append(sigle)
    establishments: list[Mapping[str, Any]] = []
    matching = payload.get("matching_etablissements")
    if isinstance(matching, list):
        establishments.extend(item for item in matching if isinstance(item, Mapping))
    siege = payload.get("siege")
    if isinstance(siege, Mapping):
        establishments.append(siege)
    for establishment in establishments:
        commercial_name = _string(establishment.get("nom_commercial"))
        if commercial_name:
            values.append(commercial_name)
        signs = establishment.get("liste_enseignes")
        if isinstance(signs, list):
            values.extend(value for item in signs if (value := _string(item)))
    representatives: dict[str, str] = {}
    for value in values:
        key = normalize_company_key(value)
        if key and key not in representatives:
            representatives[key] = value
    return tuple(representatives.values())


def _candidate_in_department(candidate: CompanyCandidate, department: str) -> bool:
    return bool(
        (candidate.postal_code and candidate.postal_code.startswith(department))
        or (candidate.commune_code and candidate.commune_code.startswith(department))
    )


def _entity_sector_type(
    is_admin: Optional[bool],
    is_collectivity: Optional[bool],
    is_association: Optional[bool],
    association_id: Optional[str],
    legal_category: Optional[str],
) -> str:
    if is_admin is True or is_collectivity is True:
        return "public"
    if is_association is True or association_id:
        return "nonprofit"
    # DINUM documents association codes explicitly, while category 7xxx is
    # reserved for public-law legal persons in the INSEE legal-category table.
    if legal_category in {"5195", "9210", "9220", "9221", "9222", "9223", "9224", "9230", "9240", "9260"}:
        return "nonprofit"
    if legal_category and legal_category.startswith("7"):
        return "public"
    # 5xxx and 6xxx are unambiguous company/private-law categories. Other
    # codes remain unknown unless DINUM provides one of the explicit flags.
    if legal_category and legal_category[0] in {"5", "6"}:
        return "private"
    return "unknown"


def _generic_or_intermediary_reason(name: str) -> Optional[str]:
    normalized = _normal_text(name)
    exact_terms = {
        "mairie", "commune", "particulier employeur", "fhf",
        "manpower", "manpower france", "randstad", "crit", "crit interim",
    }
    intermediary_terms = (
        "adecco", "synergie",
        "interim", "intérim", "agence d emploi", "agence emploi", "plateforme",
    )
    if normalized in exact_terms or any(term in normalized for term in intermediary_terms):
        return "Libellé générique, public ou intermédiaire : aucun SIREN n'est attribué automatiquement."
    return None


def _normal_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", value or "").casefold())).strip()


def _string(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_string(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    return next((_string(payload.get(key)) for key in keys if _string(payload.get(key))), None)


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None
