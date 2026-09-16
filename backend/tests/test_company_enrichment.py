from datetime import datetime, timezone

import httpx
import pytest

from app.services.company_enrichment.dinum import (
    CompanyCandidate,
    DinumCompanySearchClient,
    DinumSearchError,
    assess_company_candidates,
    enrich_company_opportunity,
    normalize_candidate,
)
from app.services.opportunities.company import CompanyOpportunity


def opportunity(name="ACME SAS", communes=("94028",), labels=("Créteil",)):
    return CompanyOpportunity(
        company_key=name.casefold(), company_name=name, department_code="94",
        active_offer_count=1, distinct_job_title_count=1, distinct_source_count=1,
        sources=("test",), oldest_offer_created_at=None, newest_offer_created_at=None,
        oldest_offer_age_days=None, newest_offer_age_days=None, contract_types=(),
        communes=communes, location_labels=labels, offer_ids=("test:1",), job_titles=("Test",),
        cdi_offer_count=0, cdd_offer_count=0, other_contract_offer_count=1,
        offers_over_21_days=0, offers_over_45_days=0, offers_over_90_days=0,
        average_offer_age_days=None, median_offer_age_days=None, signals=(),
    )


def candidate(name="ACME SAS", commune="94028", postal="94000", **extra):
    payload = {
        "siren": "123456789", "nom_complet": name, "activite_principale": "62.01Z",
        "tranche_effectif_salarie": "12", "nature_juridique": "5710",
        "etat_administratif": "A",
        "complements": {"est_administration": False, "est_association": False, "est_ess": False},
        "matching_etablissements": [{"siret": "12345678900010", "commune": commune, "libelle_commune": "Créteil", "code_postal": postal, "adresse": "1 rue Exemple"}],
    }
    payload.update(extra)
    return normalize_candidate(payload)


def response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://example.test/search"))


def test_exact_name_and_commune_is_high_confidence():
    result = assess_company_candidates(opportunity(), [candidate()])
    assert result.status == "matched_high_confidence"
    assert result.matched_candidate.siren == "123456789"
    assert result.matched_candidate.entity_sector_type == "private"
    assert result.matched_candidate.employee_bracket == "20-49"


def test_exact_name_and_postal_is_high_confidence():
    result = assess_company_candidates(opportunity(communes=()), [candidate(commune="75056", postal="94000")], postal_code="94000")
    assert result.status == "matched_high_confidence"


def test_geographically_coherent_candidate_wins_over_same_name_elsewhere():
    result = assess_company_candidates(opportunity(), [candidate(commune="75056", postal="75001"), candidate()])
    assert result.status == "matched_high_confidence"
    assert result.matched_candidate.commune_code == "94028"


def test_two_plausible_candidates_are_ambiguous():
    result = assess_company_candidates(opportunity(communes=()), [candidate(commune="75056", postal="75001"), candidate(commune="69000", postal="69000")])
    assert result.status == "ambiguous"
    assert result.matched_candidate is None


@pytest.mark.parametrize(
    "name",
    ["MAIRIE", "Particulier Employeur", "Adecco Medical", "SYNERGIE", "FHF", "MANPOWER", "RANDSTAD", "CRIT"],
)
def test_generic_or_intermediary_never_forces_siren(name):
    result = assess_company_candidates(opportunity(name), [candidate(name=name)])
    assert result.status == "generic_or_intermediary"
    assert result.matched_candidate is None


def test_public_and_nonprofit_classification_is_evidence_based():
    public = candidate(name="VILLE DE TEST", complements={"est_administration": True})
    nonprofit = candidate(name="ASSOCIATION TEST", complements={"est_association": True})
    assert public.entity_sector_type == "public"
    assert nonprofit.entity_sector_type == "nonprofit"


def test_explicit_trade_name_can_establish_a_high_confidence_alias_match():
    branded = candidate(
        name="EVANCIA (BABILOU)",
        matching_etablissements=[{
            "siret": "12345678900010", "commune": "94028", "code_postal": "94000",
            "nom_commercial": "BABILOU", "liste_enseignes": ["BABILOU"],
        }],
    )
    result = assess_company_candidates(opportunity("BABILOU"), [branded])
    assert result.status == "matched_high_confidence"
    assert result.matched_candidate.siren == "123456789"
    assert "BABILOU" in result.matched_candidate.aliases


def test_explicit_alias_shared_by_multiple_legal_entities_is_ambiguous():
    first = candidate(
        name="EVANCIA (BABILOU)",
        matching_etablissements=[{"commune": "94028", "code_postal": "94000", "liste_enseignes": ["BABILOU"]}],
    )
    second = candidate(
        name="BABILOU SAINT-MAURICE",
        siren="987654321",
        matching_etablissements=[{"commune": "94069", "code_postal": "94410", "nom_commercial": "BABILOU"}],
    )
    result = assess_company_candidates(
        opportunity("BABILOU", communes=("94028", "94069")), [first, second]
    )
    assert result.status == "ambiguous"
    assert result.matched_candidate is None


def test_brand_without_explicit_alias_proof_does_not_force_siren():
    result = assess_company_candidates(
        opportunity("BABILOU", communes=()), [candidate(name="EVANCIA", commune="75056", postal="75001")]
    )
    assert result.status == "not_found"
    assert result.matched_candidate is None


def test_public_legal_category_and_missing_legal_evidence():
    public = candidate(name="HOPITAL TEST", nature_juridique="7364", complements={})
    unknown = candidate(name="STRUCTURE TEST", nature_juridique=None, complements={})
    assert public.entity_sector_type == "public"
    assert unknown.entity_sector_type == "unknown"


def test_optional_fields_and_employee_bracket_absence_are_safe():
    normalized = normalize_candidate({"siren": "123", "nom_complet": "Test", "matching_etablissements": [{}]})
    assert normalized.employee_bracket == "unknown"
    assert normalized.postal_code is None
    assert normalized.entity_sector_type == "unknown"


def test_client_normalizes_empty_result(monkeypatch):
    monkeypatch.setattr("app.services.company_enrichment.dinum.httpx.get", lambda *args, **kwargs: response({"results": []}))
    assert DinumCompanySearchClient(base_url="https://example.test/search").search("ACME") == ()


def test_client_treats_no_content_as_an_empty_search(monkeypatch):
    monkeypatch.setattr(
        "app.services.company_enrichment.dinum.httpx.get",
        lambda *args, **kwargs: response({}, status=204),
    )
    assert DinumCompanySearchClient(base_url="https://example.test/search").search("ACME") == ()


def test_client_sends_public_search_parameters(monkeypatch):
    captured = {}
    def fake_get(*args, **kwargs):
        captured.update(kwargs)
        return response({"results": []})
    monkeypatch.setattr("app.services.company_enrichment.dinum.httpx.get", fake_get)
    DinumCompanySearchClient(base_url="https://example.test/search").search("ACME", "94", "94000")
    assert captured["params"] == {"q": "ACME", "departement": "94", "per_page": 10, "code_postal": "94000"}
    assert "Authorization" not in captured["headers"]


@pytest.mark.parametrize("status,kind", [(429, "rate_limited"), (500, "http_500")])
def test_client_handles_http_errors(monkeypatch, status, kind):
    monkeypatch.setattr("app.services.company_enrichment.dinum.httpx.get", lambda *args, **kwargs: response({}, status))
    with pytest.raises(DinumSearchError, match=kind):
        DinumCompanySearchClient(base_url="https://example.test/search").search("ACME")


def test_client_handles_timeout_and_invalid_json(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("timeout")
    monkeypatch.setattr("app.services.company_enrichment.dinum.httpx.get", timeout)
    with pytest.raises(DinumSearchError, match="timeout"):
        DinumCompanySearchClient().search("ACME")
    invalid = httpx.Response(200, content=b"not-json", request=httpx.Request("GET", "https://example.test"))
    monkeypatch.setattr("app.services.company_enrichment.dinum.httpx.get", lambda *args, **kwargs: invalid)
    with pytest.raises(DinumSearchError, match="invalid_json"):
        DinumCompanySearchClient().search("ACME")


def test_enrichment_does_not_use_candidate_order(monkeypatch):
    client = DinumCompanySearchClient(base_url="https://example.test/search")
    monkeypatch.setattr(client, "search", lambda *args, **kwargs: (candidate(commune="75056", postal="75001"), candidate()))
    result = enrich_company_opportunity(opportunity(), client)
    assert result.matched_candidate.commune_code == "94028"
