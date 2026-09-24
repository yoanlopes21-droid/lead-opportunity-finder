"""Synthetic decision and disclosure contracts for the commercial angle."""

from datetime import timedelta
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import CommercialExclusion
from app.services.commercial_configuration import (
    CommercialOffer, CommercialPolicy, CommercialProfile, save_offer, save_policy, save_profile,
)
from app.services.commercial_leads.angle import get_commercial_angle
from app.services.commercial_leads.angle import build_commercial_angle
from app.services.commercial_leads.approach import get_commercial_approach_context
from test_commercial_approach import NOW, offer, point, session  # noqa: F401
from app.services.contactability.contracts import ContactType


def profile(*, specialty="Mobilité véhicule", related=(), territory="94"):
    return CommercialProfile.model_validate({
        "consultant_name": "Alex Example", "commercial_title": "Consultant",
        "network_name": "Example Network",
        "specialties": [{"label": specialty, "related_roles": list(related), "expertise_scope": "personal"}],
        "territories": [{"department_code": territory, "territorial_familiarity": True}],
    })


def catalog(code="starter", *, default=True, enabled=True, scopes=None):
    return CommercialOffer.model_validate({
        "code": code, "display_name": code.title(), "enabled_for_prospecting": enabled,
        "is_default": default,
        "features": {
            "launch_minutes_min": 30, "launch_minutes_max": 45,
            "distribution_site_count_min": 10, "application_processing_hours": 96,
            "phone_screen": True, "interview_screen": False,
            "candidate_questionnaire": False, "consultant_analysis": True,
            "candidate_dossier": False, "reference_checks": False,
        },
        "guarantee_documentation": {"inclusion_status": "conflicting", "period": "not_documented"},
        "communication_scopes": scopes or {},
    })


def configured(session, *, specialty="Mobilité véhicule", related=(), territory="94", scopes=None):
    save_profile(session, profile(specialty=specialty, related=related, territory=territory))
    save_offer(session, catalog(scopes=scopes))
    save_policy(session, CommercialPolicy())


def angle(session):
    return get_commercial_angle(session, "nova", now=NOW)


def test_minimal_prospect_has_angle_without_channel_and_starter(session):
    configured(session)
    offer(session, title="Mécanicien automobile")
    result = angle(session)
    assert result.readiness == "channel_missing"
    assert result.pack_status == "prepared_channel_missing"
    assert result.active_angle and result.primary_angle
    assert result.target_status == "function_to_request"
    assert result.selected_offer_code == "starter"
    assert len(result.qualification_questions) == 3


def test_exact_personal_role_match_does_not_become_past_mission(session):
    configured(session, specialty="Services à domicile", related=("auxiliaire de vie",))
    offer(session, title="Auxiliaire de vie à domicile")
    result = angle(session)
    assert result.specialty_match == "strong_specialty_match"
    assert result.territorial_relevance == "useful"
    assert any(item.claim_type == "consultant_profile_fact" for item in result.client_safe_facts)
    assert "prior_exact_role_recruitment_without_reference" in result.do_not_claim
    assert "périmètre géographique" in result.qualification_questions[2].question


@pytest.mark.parametrize("title,specialty", [
    ("Juriste", "Services à domicile"),
    ("Comptable", "Mobilité véhicule"),
    ("Technicien de maintenance bois", "Technologies numériques"),
])
def test_sector_or_employer_does_not_create_occupation_expertise(session, title, specialty):
    configured(session, specialty=specialty)
    offer(session, title=title)
    result = angle(session)
    assert result.specialty_match == "no_specialty_claim"
    assert result.primary_value_proposition == "profile_criteria_clarification"
    assert "personal_occupation_specialty_claim" in result.do_not_claim


def test_locality_is_secondary_without_specific_local_need_and_irrelevant_outside(session):
    configured(session, specialty="Technologies numériques")
    offer(session, title="Data Scientist junior", location="94 - Créteil")
    result = angle(session)
    assert result.specialty_match == "strong_specialty_match"
    assert result.territorial_relevance == "secondary"
    assert result.primary_value_proposition == "profile_criteria_clarification"
    assert "autonomie" in result.qualification_questions[2].question
    save_profile(session, profile(specialty="Technologies numériques", territory="33"))
    assert angle(session).territorial_relevance == "irrelevant"


def test_automotive_specialty_does_not_claim_aircraft_engine_expertise(session):
    configured(session, specialty="Mobilité véhicule")
    offer(session, title="Mécanicien moteur aéronautique")
    result = angle(session)
    assert result.specialty_match == "no_specialty_claim"
    assert result.territorial_relevance == "secondary"


def test_national_role_does_not_force_territorial_claim(session):
    configured(session)
    offer(session, title="Responsable national des ventes", location="94 - Créteil")
    result = angle(session)
    assert result.territorial_relevance == "irrelevant"
    assert not any(claim.source_reference.startswith("territories:") for claim in result.client_safe_facts)


def test_small_confirmed_size_allows_generic_director_function(session):
    configured(session)
    offer(session, title="Mécanicien automobile")
    context = replace(get_commercial_approach_context(session, "nova", now=NOW), employee_range="1-9")
    result = build_commercial_angle(context, profile(), (catalog(),), CommercialPolicy())
    assert result.target_role == "dirigeant ou responsable du recrutement"
    assert result.target_status == "function_to_request"


def test_specialty_prefers_comparable_evidence_but_not_stale_offer(session):
    configured(session, specialty="Services à domicile", related=("auxiliaire de vie",))
    offer(session, identifier="business", title="Business developer", age_days=1)
    offer(session, identifier="care", title="Auxiliaire de vie", age_days=4)
    result = angle(session)
    assert result.entry_offer.title == "Auxiliaire de vie"
    assert "personal_specialty_preferred_at_comparable_evidence_quality" in result.entry_offer_reasons
    assert result.entry_offer.source_urls


def test_old_specialty_offer_does_not_beat_recent_other_role(session):
    configured(session, specialty="Services à domicile", related=("auxiliaire de vie",))
    offer(session, identifier="business", title="Business developer", age_days=1)
    offer(session, identifier="care", title="Auxiliaire de vie", age_days=80)
    assert angle(session).entry_offer.title == "Business developer"


def test_old_or_uncertain_publication_does_not_assert_current_opening(session):
    configured(session)
    offer(session, title="Mécanicien", age_days=50)
    result = angle(session)
    assert result.pack_status == "verify_offer"
    assert result.client_safe_facts == ()
    assert "current_opening_without_reverification" in result.do_not_claim
    assert "verify_old_publication_before_current_opening_claim" in result.internal_advice
    assert any("observed_only" in claim.limitations[0] for claim in result.claims if claim.claim_type == "observed_job_fact" and claim.limitations)


@pytest.mark.parametrize("kind,value,expected", [
    (ContactType.PHONE, "+33102030405", "company_switchboard:routing"),
    (ContactType.EMAIL, "contact@nova.example.test", "general_email:routing"),
])
def test_generic_channel_is_routing_not_confirmed_hr(session, kind, value, expected):
    configured(session)
    offer(session)
    point(session, kind=kind, value=value)
    result = angle(session)
    assert result.pack_status == "routing_required"
    assert result.channel_strategy == expected
    assert result.target_status == "function_to_request"
    assert "confirmed_hr_recipient" in result.do_not_claim


def test_verify_employer_allows_preparation_but_blocks_client_facts(session):
    configured(session)
    offer(session, description="Et si c'était Autre Société qu'il vous fallait ?")
    result = angle(session)
    assert result.pack_status == "verify_employer"
    assert result.active_angle and result.primary_angle
    assert result.client_safe_facts == ()
    assert "client_outreach_before_readiness_cleared" in result.do_not_claim


def test_suspension_has_no_active_prospecting_angle(session):
    configured(session)
    offer(session)
    session.add(CommercialExclusion(
        company_key="nova", company_name_snapshot="NOVA", exclusion_type="do_not_contact",
        starts_at=NOW - timedelta(days=1),
    ))
    session.flush()
    result = angle(session)
    assert result.pack_status == "suspended"
    assert not result.active_angle and result.primary_angle is None
    assert result.target_role is None and result.client_safe_facts == ()


def test_multiple_locations_alone_never_suggest_enhanced(session):
    configured(session)
    save_offer(session, catalog("enhanced", default=False))
    save_offer(session, catalog("premium", default=False, enabled=False))
    offer(session, identifier="first", title="Technicien", location="94 - Créteil", commune="94028")
    assert angle(session).suggested_offer_code is None
    offer(session, identifier="second", title="Technicien", location="94 - Vincennes", commune="94080")
    result = angle(session)
    assert result.selected_offer_code == "starter"
    assert result.suggested_offer_code is None


def test_internal_and_client_document_scopes_and_manual_levers(session):
    configured(session, scopes={
        "display_name": "internal_only",
        "features.phone_screen": "client_communicable",
        "features.consultant_analysis": "manual_approval_required",
    })
    offer(session)
    result = angle(session)
    refs = {claim.source_reference for claim in result.client_safe_facts}
    assert "starter.features.phone_screen" in refs
    assert "starter.display_name" not in refs
    assert "starter.features.consultant_analysis" not in refs
    assert any("manual_approval_required" in advice for advice in result.internal_advice)
    assert "pricing_manual" in result.commercial_levers_available
    assert not any(claim.claim_type == "commercial_policy_fact" for claim in result.client_safe_facts)
    assert "guarantee_included_or_complimentary_without_manual_decision" in result.do_not_claim
    assert "exclusivity_without_manual_decision" in result.do_not_claim
    assert "job_listing_count_as_vacancy_count" in result.do_not_claim


def test_read_only_api_returns_traceable_angle_and_no_mutation(session):
    configured(session)
    offer(session)
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/commercial-leads/nova/commercial-angle?department=94")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["entry_offer"]["title"] == "Technicien"
        assert body["claims"][0]["source_reference"]
        assert body["selected_offer_code"] == "starter"
    finally:
        app.dependency_overrides.clear()
