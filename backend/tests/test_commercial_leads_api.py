from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import CommercialExclusion, CompanyEnrichment, ObservedJobOffer
from app.services.company_enrichment.contracts import MatchStatus
from app.services.commercial_leads.exclusions import ExclusionType


NOW = datetime(2026, 9, 17, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'commercial-leads-api-test.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def client(session):
    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def add_offer(session, identifier, company, *, department="94", title="Technicien", age_days=2, url=None):
    session.add(ObservedJobOffer(
        source="france_travail",
        source_offer_id=identifier,
        title=title,
        company_name=company,
        location_label="Créteil",
        commune="94028",
        department_code=department,
        created_at=(NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z"),
        source_url=url,
        first_seen_at=NOW - timedelta(days=age_days),
        last_seen_at=NOW,
        last_changed_at=NOW,
        is_active=True,
        observation_count=2,
    ))


def add_enrichment(
    session, company_key, company_name, *, status=MatchStatus.HIGH_CONFIDENCE,
    sector="private", employee_range="20-49", siren="123456789",
):
    confirmed = status == MatchStatus.HIGH_CONFIDENCE
    session.add(CompanyEnrichment(
        company_key=company_key,
        source_company_name=company_name,
        provider="dinum",
        match_status=status,
        entity_sector_type=sector,
        siren=siren if confirmed else None,
        siret="12345678900010" if confirmed else None,
        official_name=company_name if confirmed else None,
        commune="Créteil" if confirmed else None,
        employee_range=employee_range if confirmed else None,
        last_attempt_at=NOW,
        attempt_count=1,
        input_fingerprint=f"fingerprint-{company_key}",
    ))


def add_exclusion(session, company_key, *, siren="123456789"):
    session.add(CommercialExclusion(
        company_key=company_key,
        siren=siren,
        company_name_snapshot=company_key.upper(),
        exclusion_type=ExclusionType.CURRENT_CLIENT,
        starts_at=NOW - timedelta(days=1),
    ))


def seed_leads(session):
    add_offer(session, "alpha-1", "ALPHA", title="Commercial", url="https://example.test/alpha")
    add_offer(session, "alpha-2", "ALPHA", title="Consultant", url="https://example.test/alpha")
    add_enrichment(session, "alpha", "ALPHA", siren="111111111")

    add_offer(session, "public-1", "VILLE EXEMPLE")
    add_enrichment(session, "ville exemple", "VILLE EXEMPLE", sector="public", siren="222222222")

    add_offer(session, "missing-1", "SANS IDENTITE", url=None)
    add_enrichment(session, "sans identite", "SANS IDENTITE", status=MatchStatus.NOT_FOUND)

    add_offer(session, "excluded-1", "EXCLU")
    add_enrichment(session, "exclu", "EXCLU")
    add_exclusion(session, "exclu")

    add_offer(session, "other-department", "HORS SCOPE", department="93")
    add_enrichment(session, "hors scope", "HORS SCOPE", siren="333333333")
    session.commit()


def names(response):
    return [item["company_name"] for item in response.json()["items"]]


def test_get_without_parameters_returns_200(client, session):
    seed_leads(session)
    assert client.get("/api/v1/commercial-leads").status_code == 200


def test_list_contract_has_items_total_limit_and_offset(client, session):
    seed_leads(session)
    body = client.get("/api/v1/commercial-leads").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    assert {"company_key", "subscores", "evidence", "recommended_channel"} <= set(body["items"][0])


def test_default_list_excludes_ineligible_companies(client, session):
    seed_leads(session)
    assert "EXCLU" not in names(client.get("/api/v1/commercial-leads"))


def test_audit_includes_excluded_company_and_exclusion(client, session):
    seed_leads(session)
    excluded = next(item for item in client.get("/api/v1/commercial-leads?include_excluded=true").json()["items"] if item["company_name"] == "EXCLU")
    assert excluded["is_eligible"] is False


def test_audit_represents_the_matching_exclusion(client, session):
    seed_leads(session)
    excluded = next(item for item in client.get("/api/v1/commercial-leads?include_excluded=true").json()["items"] if item["company_name"] == "EXCLU")
    assert excluded["exclusion"]["exclusion_type"] == ExclusionType.CURRENT_CLIENT


def test_category_filter(client, session):
    seed_leads(session)
    initial = client.get("/api/v1/commercial-leads").json()
    category = initial["items"][0]["category"]
    body = client.get("/api/v1/commercial-leads", params={"category": category}).json()
    assert body["items"] and all(item["category"] == category for item in body["items"])


def test_entity_sector_type_filter(client, session):
    seed_leads(session)
    assert names(client.get("/api/v1/commercial-leads?entity_sector_type=public")) == ["VILLE EXEMPLE"]


def test_minimum_score_filter(client, session):
    seed_leads(session)
    body = client.get("/api/v1/commercial-leads?minimum_score=100").json()
    assert all(item["total_score"] >= 100 for item in body["items"])


def test_department_filter(client, session):
    seed_leads(session)
    assert names(client.get("/api/v1/commercial-leads?department=93")) == ["HORS SCOPE"]


def test_pagination_uses_existing_limit_and_offset(client, session):
    seed_leads(session)
    first = client.get("/api/v1/commercial-leads?limit=1").json()
    second = client.get("/api/v1/commercial-leads?limit=1&offset=1").json()
    assert first["total"] == 3 and len(first["items"]) == 1
    assert first["items"][0]["company_key"] != second["items"][0]["company_key"]


def test_sorting_is_score_descending_and_deterministic(client, session):
    seed_leads(session)
    first = client.get("/api/v1/commercial-leads").json()["items"]
    second = client.get("/api/v1/commercial-leads").json()["items"]
    assert [item["company_key"] for item in first] == [item["company_key"] for item in second]
    assert [item["total_score"] for item in first] == sorted((item["total_score"] for item in first), reverse=True)


def test_missing_optional_values_are_null_or_empty(client, session):
    seed_leads(session)
    item = next(item for item in client.get("/api/v1/commercial-leads").json()["items"] if item["company_name"] == "SANS IDENTITE")
    assert item["official_name"] is None and item["siren"] is None
    assert item["employee_range"] is None and item["evidence"][0]["source_url"] is None
    assert item["recommended_channel"] is None


def test_not_found_dinum_company_is_returned(client, session):
    seed_leads(session)
    assert "SANS IDENTITE" in names(client.get("/api/v1/commercial-leads"))


def test_invalid_parameters_return_422(client, session):
    seed_leads(session)
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"minimum_score": 101}, {"category": "invalid"}):
        assert client.get("/api/v1/commercial-leads", params=params).status_code == 422


def test_get_does_not_mutate_database(client, session):
    seed_leads(session)
    offer = session.query(ObservedJobOffer).filter_by(source_offer_id="alpha-1").one()
    enrichment = session.query(CompanyEnrichment).filter_by(company_key="alpha").one()
    exclusion = session.query(CommercialExclusion).filter_by(company_key="exclu").one()
    before = (
        offer.last_seen_at, offer.observation_count, enrichment.attempt_count,
        enrichment.match_status, exclusion.starts_at,
    )
    assert client.get("/api/v1/commercial-leads?include_excluded=true").status_code == 200
    session.expire_all()
    offer = session.query(ObservedJobOffer).filter_by(source_offer_id="alpha-1").one()
    enrichment = session.query(CompanyEnrichment).filter_by(company_key="alpha").one()
    exclusion = session.query(CommercialExclusion).filter_by(company_key="exclu").one()
    assert before == (
        offer.last_seen_at, offer.observation_count, enrichment.attempt_count,
        enrichment.match_status, exclusion.starts_at,
    )
