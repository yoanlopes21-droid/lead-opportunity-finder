from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import CommercialExclusion


NOW = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'commercial-exclusions-api-test.sqlite3'}",
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


def payload(**overrides):
    values = {
        "company_name": "ACME SAS",
        "exclusion_type": "recent_prospect",
        "siren": "123456789",
        "reason": "Contact récent",
        "starts_at": "2020-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    values.update(overrides)
    return values


def test_crud_filters_and_expiration_status(client, session):
    created = client.post("/api/v1/commercial-exclusions", json=payload())
    assert created.status_code == 201
    assert created.json()["matching_basis"] == "siren"

    expired = client.post("/api/v1/commercial-exclusions", json=payload(
        company_name="Ancien Prospect", siren=None,
        starts_at="2025-01-01T00:00:00Z", expires_at="2025-02-01T00:00:00Z",
    ))
    assert expired.status_code == 201 and expired.json()["status"] == "expired"

    body = client.get("/api/v1/commercial-exclusions", params={
        "type": "recent_prospect", "status": "active", "search": "acme",
    }).json()
    assert body["total"] == 1 and body["items"][0]["company_name_snapshot"] == "ACME SAS"

    exclusion_id = created.json()["id"]
    assert client.delete(f"/api/v1/commercial-exclusions/{exclusion_id}").status_code == 204
    assert session.get(CommercialExclusion, exclusion_id) is None
    assert client.delete(f"/api/v1/commercial-exclusions/{exclusion_id}").status_code == 404


@pytest.mark.parametrize("changes, expected", [
    ({"exclusion_type": "unknown"}, "type d’exclusion"),
    ({"siren": "123"}, "9 chiffres"),
    ({"expires_at": "2019-12-31T00:00:00Z"}, "postérieure"),
    ({"exclusion_type": "current_client"}, "prospect récent"),
])
def test_create_reports_clear_validation_errors(client, changes, expected):
    response = client.post("/api/v1/commercial-exclusions", json=payload(**changes))
    assert response.status_code == 422
    assert expected in response.json()["detail"]


def test_strict_duplicate_returns_conflict(client):
    assert client.post("/api/v1/commercial-exclusions", json=payload()).status_code == 201
    duplicate = client.post("/api/v1/commercial-exclusions", json=payload())
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Cette exclusion existe déjà."


def test_csv_preview_then_import_reports_added_duplicates_and_invalid_rows(client, session):
    content = """entreprise;type_exclusion;siren;raison;date_debut;date_expiration
ACME SAS;Prospect récent;123456789;Salon;2026-09-01;2026-12-01
ACME SAS;Prospect récent;123456789;Salon;2026-09-01;2026-12-01
CLIENT SA;Client actuel;987654321;Contrat;2026-09-01;
INVALIDE;Exclusion manuelle;123;SIREN erroné;2026-09-01;
DATES;Prospect récent;;Dates;2026-12-01;2026-11-01
"""
    preview = client.post("/api/v1/commercial-exclusions/import/preview", json={"content": content})
    assert preview.status_code == 200
    assert preview.json()["valid_count"] == 2
    assert preview.json()["duplicate_count"] == 1
    assert preview.json()["invalid_count"] == 2
    assert any("9 chiffres" in error for row in preview.json()["rows"] for error in row["errors"])

    imported = client.post("/api/v1/commercial-exclusions/import", json={"content": content})
    assert imported.status_code == 200
    assert imported.json()["added_count"] == 2
    assert imported.json()["ignored_count"] == 3
    assert session.query(CommercialExclusion).count() == 2

    repeated = client.post("/api/v1/commercial-exclusions/import", json={"content": content}).json()
    assert repeated["added_count"] == 0 and repeated["duplicate_count"] == 3
    assert session.query(CommercialExclusion).count() == 2


def test_invalid_csv_is_rejected_without_inserting(client, session):
    response = client.post("/api/v1/commercial-exclusions/import/preview", json={
        "content": "wrong,headers\nvalue,other",
    })
    assert response.status_code == 422
    assert session.query(CommercialExclusion).count() == 0
