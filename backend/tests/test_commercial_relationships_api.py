from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import CommercialExclusion, CommercialRelationship, ObservedJobOffer
from app.services.commercial_leads.exclusions import ExclusionType


NOW = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'commercial-relationships-api-test.sqlite3'}",
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


def seed_lead(session, company="ACME SAS"):
    session.add(ObservedJobOffer(
        source="france_travail", source_offer_id=f"offer-{company}", title="Technicien",
        company_name=company, location_label="Créteil", commune="94028", department_code="94",
        created_at=(NOW - timedelta(days=2)).isoformat(), source_url="https://source.test/offer",
        first_seen_at=NOW, last_seen_at=NOW, last_changed_at=NOW, is_active=True, observation_count=1,
    ))
    session.commit()


def create_follow_up(client, **overrides):
    body = {
        "company_key": "acme sas",
        "status": "contacted",
        "last_contact_at": NOW.isoformat(),
        "note": "Premier échange",
    }
    body.update(overrides)
    return client.post("/api/v1/commercial-relationships/from-lead", json=body)


def test_create_follow_up_from_real_lead_hides_it_from_new_opportunities(client, session):
    seed_lead(session)
    response = create_follow_up(client)
    assert response.status_code == 201
    assert response.json()["company_name_snapshot"] == "ACME SAS"
    assert response.json()["status"] == "contacted"
    assert client.get("/api/v1/commercial-leads").json()["items"] == []
    audited = client.get("/api/v1/commercial-leads?include_excluded=true").json()["items"][0]
    assert audited["commercial_relationship"]["id"] == response.json()["id"]


def test_update_status_with_optional_follow_up_date(client, session):
    seed_lead(session)
    relationship_id = create_follow_up(client).json()["id"]
    undated = client.patch(f"/api/v1/commercial-relationships/{relationship_id}", json={"status": "follow_up"})
    assert undated.status_code == 200 and undated.json()["next_action_at"] is None
    due = (NOW + timedelta(days=7)).isoformat()
    updated = client.patch(f"/api/v1/commercial-relationships/{relationship_id}", json={
        "status": "follow_up", "next_action_at": due, "note": "Rappeler mardi",
    })
    assert updated.status_code == 200 and updated.json()["next_action_at"].startswith("2026-09-29T10:00:00")
    assert client.get("/api/v1/commercial-relationships?view=follow_up").json()["total"] == 1


@pytest.mark.parametrize("status, exclusion_type", [
    ("client", ExclusionType.CURRENT_CLIENT),
    ("do_not_contact", ExclusionType.MANUAL_EXCLUSION),
])
def test_hard_status_creates_linked_hard_exclusion(client, session, status, exclusion_type):
    seed_lead(session)
    response = create_follow_up(client, status=status, last_contact_at=None)
    assert response.status_code == 201
    row = session.get(CommercialRelationship, response.json()["id"])
    exclusion = session.get(CommercialExclusion, row.hard_exclusion_id)
    assert exclusion.exclusion_type == exclusion_type
    blocked_delete = client.delete(f"/api/v1/commercial-exclusions/{exclusion.id}")
    assert blocked_delete.status_code == 409


def test_wrong_contact_is_followed_but_not_hard_excluded(client, session):
    seed_lead(session)
    response = create_follow_up(client, status="wrong_contact", outcome="Standard non pertinent")
    assert response.status_code == 201
    assert response.json()["hard_exclusion_id"] is None
    assert session.query(CommercialExclusion).count() == 0
    assert client.get("/api/v1/commercial-leads").json()["items"] == []


def test_changing_client_to_wrong_contact_removes_only_generated_hard_exclusion(client, session):
    seed_lead(session)
    relationship_id = create_follow_up(client, status="client", last_contact_at=None).json()["id"]
    assert session.query(CommercialExclusion).count() == 1
    updated = client.patch(f"/api/v1/commercial-relationships/{relationship_id}", json={
        "status": "wrong_contact", "outcome": "Changer d’interlocuteur",
    })
    assert updated.status_code == 200 and updated.json()["hard_exclusion_id"] is None
    assert session.query(CommercialExclusion).count() == 0
    assert client.get("/api/v1/commercial-leads").json()["items"] == []


@pytest.mark.parametrize("status", ["refused", "no_current_need"])
def test_closed_status_keeps_future_recontact_without_hard_exclusion(client, session, status):
    seed_lead(session)
    response = create_follow_up(
        client, status=status, next_action_at=(NOW + timedelta(days=90)).isoformat(), outcome="Pas maintenant",
    )
    assert response.status_code == 201 and response.json()["next_action_at"] is not None
    assert response.json()["hard_exclusion_id"] is None
    assert client.get("/api/v1/commercial-relationships?view=closed").json()["total"] == 1
    assert client.get("/api/v1/commercial-relationships?view=follow_up").json()["total"] == 1


def test_reopen_returns_company_to_opportunities_and_removes_generated_exclusion(client, session):
    seed_lead(session)
    relationship_id = create_follow_up(client, status="client", last_contact_at=None).json()["id"]
    assert session.query(CommercialExclusion).count() == 1
    response = client.post(f"/api/v1/commercial-relationships/{relationship_id}/reopen-opportunity")
    assert response.status_code == 204
    assert session.query(CommercialExclusion).count() == 0
    assert session.get(CommercialRelationship, relationship_id).is_active is False
    assert client.get("/api/v1/commercial-leads").json()["total"] == 1


def test_unknown_company_cannot_be_created_as_a_follow_up(client):
    response = client.post("/api/v1/commercial-relationships/from-lead", json={
        "company_key": "invented company", "status": "contacted",
    })
    assert response.status_code == 404
