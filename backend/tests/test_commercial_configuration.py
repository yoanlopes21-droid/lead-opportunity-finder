"""Synthetic configuration fixtures only; no real commercial values."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import CommercialCatalogOfferRecord, CommercialPolicyRecord, CommercialProfileRecord
from app.services.commercial_configuration import (
    CommercialOffer, CommercialPolicy, CommercialProfile, approved_references,
    calculate_private_scenario, personal_specialties, resolve_exclusivity, select_offer,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'configuration.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def client(session):
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as api:
        yield api
    app.dependency_overrides.clear()


def offer(code="starter", **changes):
    payload = {
        "code": code, "display_name": code.title(), "enabled_for_prospecting": True,
        "is_default": code == "starter",
        "features": {
            "launch_minutes_min": 30, "launch_minutes_max": 45,
            "distribution_site_count_min": 10, "hunt_campaign_count": 1,
            "hunted_candidates_per_campaign": 20, "application_processing_hours": 96,
            "phone_screen": True, "interview_screen": False, "candidate_questionnaire": False,
            "consultant_analysis": True, "candidate_dossier": False,
            "reference_checks": False, "post_integration_days": [0, 14],
            "option_codes": ["reference_checks"],
        },
        "guarantee_documentation": {"inclusion_status": "conflicting", "period": "initial_trial"},
        "claim_limitations": ["hunt_target_not_response_or_presentation", "processing_deadline_not_hire_deadline"],
    }
    payload.update(changes)
    return payload


def test_absent_profile_then_update_multiple_specialties_and_territories(client, session):
    assert client.get("/api/v1/commercial-configuration/profile").json() is None
    profile = {
        "consultant_name": "Alex Example", "commercial_title": "Account Lead Example",
        "network_name": "Sample Network", "phone": "0000000000",
        "specialties": [
            {"label": "Logistics", "expertise_scope": "personal", "related_roles": ["Dispatcher"]},
            {"label": "Education", "expertise_scope": "method_only"},
        ],
        "territories": [
            {"department_code": "33", "territorial_familiarity": True},
            {"department_code": "75", "territorial_familiarity": False},
        ],
        "verified_references": [],
    }
    assert client.put("/api/v1/commercial-configuration/profile", json=profile).status_code == 200
    assert len(client.get("/api/v1/commercial-configuration/profile").json()["specialties"]) == 2
    profile["phone"] = "1111111111"
    assert client.put("/api/v1/commercial-configuration/profile", json=profile).json()["phone"] == "1111111111"
    assert session.query(CommercialProfileRecord).count() == 1


def test_catalog_default_manual_other_and_disabled(client, session):
    path = "/api/v1/commercial-configuration/offers"
    assert client.get(path).json() == []
    assert client.put(f"{path}/starter", json=offer()).status_code == 200
    assert client.get(f"{path}/default").json()["code"] == "starter"
    enhanced = offer("enhanced", features={**offer()["features"], "interview_screen": True, "reference_checks": True})
    assert client.put(f"{path}/enhanced", json=enhanced).status_code == 200
    assert select_offer(session, "enhanced").features.reference_checks
    assert select_offer(session).code == "starter"
    disabled = offer("premium_like", enabled_for_prospecting=False, is_default=False)
    assert client.put(f"{path}/premium_like", json=disabled).status_code == 200
    assert not client.get(f"{path}/premium_like").json()["enabled_for_prospecting"]
    assert client.put(f"{path}/premium_like", json={**disabled, "is_default": True}).status_code == 422
    enhanced["is_default"] = True
    assert client.put(f"{path}/enhanced", json=enhanced).status_code == 200
    assert client.get(f"{path}/default").json()["code"] == "enhanced"
    assert session.query(CommercialCatalogOfferRecord).count() == 3


def test_policy_manual_pricing_exclusivity_guarantee_and_fee_trigger(client, session):
    path = "/api/v1/commercial-configuration/policy"
    assert client.get(path).json() is None
    policy = {
        "usual_rate_min_percent": "11", "usual_rate_max_percent": "13",
        "discretionary_cap_eur_ex_vat": "3000", "payment_term_days": 21,
        "guarantee_included": False, "guarantee_complimentary": None,
        "success_fee_trigger": "effective_start", "new_client_exclusive_by_default": False,
        "existing_client_exclusivity_manual": True,
    }
    response = client.put(path, json=policy)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["success_fee_trigger"] == "effective_start"
    assert saved["payment_term_days"] == 21
    assert saved["guarantee_included"] is False
    assert saved["guarantee_complimentary"] is None
    assert calculate_private_scenario(Decimal("40000"), None) is None
    assert calculate_private_scenario(Decimal("40000"), Decimal("12")) == Decimal("4800.00")
    assert calculate_private_scenario(Decimal("40000"), Decimal("12"), manually_selected_cap=Decimal("3000")) == Decimal("3000.00")
    assert calculate_private_scenario(Decimal("40000"), Decimal("12"), manually_selected_discount=Decimal("500")) == Decimal("4300.00")
    assert client.put(path, json={**policy, "new_client_exclusive_by_default": True}).status_code == 422
    assert client.put(path, json={**policy, "guarantee_promises_result": True}).status_code == 422
    policy["guarantee_included"] = True
    policy["guarantee_complimentary"] = False
    assert client.put(path, json=policy).json()["guarantee_complimentary"] is False
    assert session.query(CommercialPolicyRecord).count() == 1


def test_additive_schema_creation_preserves_existing_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE existing_business_data (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO existing_business_data (id, value) VALUES (1, 'unchanged')"))
    before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    after = set(inspect(engine).get_table_names())
    assert before <= after
    assert {"commercial_profiles", "commercial_catalog_offers", "commercial_policies"} <= after
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM existing_business_data WHERE id=1")).scalar_one() == "unchanged"
    engine.dispose()


def test_claim_scopes_and_manual_exclusivity():
    profile = CommercialProfile.model_validate({
        "consultant_name": "Alex Example", "commercial_title": "Consultant",
        "network_name": "Sample Network",
        "specialties": [
            {"label": "Logistics", "expertise_scope": "personal"},
            {"label": "Retail", "expertise_scope": "network"},
        ],
        "verified_references": [
            {"description": "An unapproved case", "evidence_scope": "personal"},
            {"description": "Approved network case", "evidence_scope": "network", "approved_for_claims": True},
        ],
    })
    assert [s.label for s in personal_specialties(profile)] == ["Logistics"]
    assert approved_references(profile, "personal") == []
    assert len(approved_references(profile, "network")) == 1
    policy = CommercialPolicy()
    assert resolve_exclusivity(policy) is False
    assert resolve_exclusivity(policy, manual_choice=True) is True
