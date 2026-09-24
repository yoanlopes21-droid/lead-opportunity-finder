"""Synthetic copy and disclosure contracts; no private prospect data."""

from dataclasses import replace
from datetime import timedelta

import pytest

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import CommercialExclusion
from app.services.commercial_leads.approach_pack import (
    build_commercial_approach_pack, get_commercial_approach_pack,
)
from app.services.commercial_leads.angle import build_commercial_angle
from app.services.commercial_leads.approach import get_commercial_approach_context
from app.services.commercial_configuration import CommercialPolicy
from app.services.contactability.contracts import (
    ContactConfidence, ContactScope, ContactType, PersonContactInput, PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.persistence import upsert_person_contact
from test_commercial_angle import catalog, configured, profile
from test_commercial_approach import NOW, offer, point, session  # noqa: F401


def pack(session):
    return get_commercial_approach_pack(session, "nova", now=NOW)


def draft(pack, kind):
    return next(item for item in pack.email if item.type == kind)


def phone_draft(pack, kind):
    return next(item for item in pack.phone if item.type == kind)


def test_minimal_prospect_prepares_copy_without_channel(session):
    configured(session)
    offer(session, title="Technicien de maintenance", location="94 - Créteil")
    result = pack(session)
    assert result.status == "prepared_channel_missing"
    assert result.communication_status == "prepared_no_channel"
    assert "Technicien de maintenance" in result.phone[0].first_30_seconds
    assert "à Créteil" in draft(result, "cold_email").subject
    assert not draft(result, "cold_email").ready_to_copy
    assert len(result.priority_objections) == 3
    assert len(result.objections) == 13
    assert result.evidence.claims_used
    assert "unproven_urgency" in result.evidence.do_not_claim
    assert draft(result, "email_requested_after_call").body is None


def test_routing_email_and_standard_do_not_impersonate_decision_maker(session):
    configured(session)
    offer(session, title="Mécanicien automobile")
    point(session)
    result = pack(session)
    assert result.status == "routing_required"
    assert draft(result, "routing_email").ready_to_copy
    assert not draft(result, "cold_email").ready_to_copy
    assert "transmettre" in draft(result, "routing_email").body
    assert "personne ou le service" in phone_draft(result, "phone_gatekeeper").first_30_seconds
    assert "confirmé" not in phone_draft(result, "phone_gatekeeper").first_30_seconds


def test_verified_hr_channel_uses_cold_email_and_role_branches(session):
    configured(session)
    offer(session)
    person = upsert_person_contact(session, PersonContactInput(
        company_key="nova", organization_name_snapshot="NOVA", scope=ContactScope.COMPANY,
        full_name="Camille Martin", relevance_role=PersonRelevanceRole.HR,
        confidence_level=ContactConfidence.HIGH_CONFIDENCE,
        verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW,
    ))
    point(session, value="camille@nova.example.test", kind=ContactType.EMAIL, person_contact_id=person.id)
    result = pack(session)
    assert result.status == "ready_for_call"
    assert draft(result, "cold_email").ready_to_copy
    assert not draft(result, "routing_email").ready_to_copy
    assert "complément de votre démarche" in phone_draft(result, "phone_hr").first_30_seconds
    assert phone_draft(result, "phone_refusal").continuation is None


def test_cold_email_with_direct_contact_and_no_invented_claims(session):
    configured(session, specialty="Technologies numériques", scopes={
        "features.phone_screen": "client_communicable",
        "features.consultant_analysis": "manual_approval_required",
        "display_name": "internal_only",
    })
    offer(session, title="Data Scientist junior")
    point(session, value="contact@nova.example.test", kind=ContactType.EMAIL)
    # A generic email must be routed, even when the occupation matches a specialty.
    result = pack(session)
    body = draft(result, "cold_email").body
    assert "qualification téléphonique" in body
    assert "Data Scientist junior" in body
    assert "Starter" not in body
    assert "garantie" not in body.lower()
    assert "candidats disponibles" not in body.lower()
    assert "oui, j'ai déjà recruté" not in " ".join(o.response.lower() for o in result.objections)
    assert "recrutement identique" in next(o.response for o in result.objections if o.code == "prior_experience")
    assert all("features.consultant_analysis" not in c.source_reference for c in result.evidence.claims_used)


def test_follow_up_email_requires_actual_call_event(session):
    configured(session)
    offer(session)
    point(session)
    context = get_commercial_approach_context(session, "nova", now=NOW)
    angle = build_commercial_angle(context, profile(), (catalog(),), CommercialPolicy())
    before = build_commercial_approach_pack(context, angle, profile())
    after = build_commercial_approach_pack(context, angle, profile(), call_request_kind="email")
    assert draft(before, "email_requested_after_call").body is None
    assert "Comme demandé lors de notre échange" in draft(after, "email_requested_after_call").body
    presentation = build_commercial_approach_pack(context, angle, profile(), call_request_kind="presentation")
    assert draft(presentation, "email_requested_after_call").attachment_recommendation == "approved_client_presentation"


@pytest.mark.parametrize("readiness,expected", [
    ("verify_offer", "verify_offer"),
    ("verify_employer", "verify_employer"),
    ("intermediary_not_employer", "intermediary_not_employer"),
    ("suspended", "suspended"),
])
def test_blocked_readiness_never_exposes_copy(session, readiness, expected):
    configured(session)
    offer(session)
    context = replace(get_commercial_approach_context(session, "nova", now=NOW), readiness=readiness)
    angle = build_commercial_angle(context, profile(), (catalog(),), CommercialPolicy())
    result = build_commercial_approach_pack(context, angle, profile())
    assert result.status == expected
    assert result.communication_status == "blocked"
    assert all(item.first_30_seconds is None for item in result.phone)
    assert all(item.body is None for item in result.email)
    assert not result.objections


def test_offer_without_approved_catalog_blocks_client_copy(session):
    offer(session)
    context = get_commercial_approach_context(session, "nova", now=NOW)
    angle = build_commercial_angle(context, profile(), (), CommercialPolicy())
    result = build_commercial_approach_pack(context, angle, profile())
    assert result.status == "verify_offer"
    assert draft(result, "cold_email").body is None


def test_verify_contact_keeps_drafts_but_requires_verification(session):
    configured(session)
    offer(session)
    context = replace(get_commercial_approach_context(session, "nova", now=NOW), readiness="verify_contact")
    angle = build_commercial_angle(context, profile(), (catalog(),), CommercialPolicy())
    result = build_commercial_approach_pack(context, angle, profile())
    assert result.communication_status == "verify_contact"
    assert result.phone[0].first_30_seconds
    assert not result.phone[0].ready_to_copy


def test_multiple_adverts_never_become_a_vacancy_count(session):
    configured(session)
    offer(session, identifier="one", title="Technicien")
    offer(session, identifier="two", title="Comptable", location="94 - Vincennes", commune="94080")
    result = pack(session)
    text = " ".join(filter(None, [*(p.first_30_seconds for p in result.phone),
                                  *(e.body for e in result.email)]))
    assert result.commercial_angle.suggested_offer_code is None
    assert "deux postes" not in text and "2 postes" not in text
    assert "urgent" not in text.lower()


def test_objection_library_is_short_and_never_promises_price_or_reference(session):
    configured(session, specialty="Transport")
    offer(session, title="Comptable")
    result = pack(session)
    codes = {item.code for item in result.objections}
    assert len(codes) == 13
    assert {"too_expensive", "no_budget", "no_exclusivity", "prior_experience"} <= codes
    assert "Je n'ai pas de recrutement identique" in next(o.response for o in result.objections if o.code == "prior_experience")
    assert "Transport" not in draft(result, "cold_email").body
    assert all("%" not in item.response and "€" not in item.response for item in result.objections)
    assert all(item.follow_up_question is None or item.follow_up_question.count("?") == 1 for item in result.objections)


def test_verify_employer_and_suspension_block_outreach(session):
    configured(session)
    offer(session, description="Et si c'était Autre Société qu'il vous fallait ?")
    uncertain = pack(session)
    assert uncertain.status == "verify_employer"
    assert uncertain.phone[0].first_30_seconds is None
    assert draft(uncertain, "cold_email").body is None
    assert not uncertain.objections
    assert uncertain.internal.advice
    session.add(CommercialExclusion(
        company_key="nova", company_name_snapshot="NOVA", exclusion_type="do_not_contact",
        starts_at=NOW - timedelta(days=1),
    ))
    session.flush()
    suspended = pack(session)
    assert suspended.status == "suspended"
    assert not suspended.objections
    assert all(item.body is None for item in suspended.email)


def test_read_only_api_exposes_separate_drafts(session):
    configured(session)
    offer(session)
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/commercial-leads/nova/approach-pack?department=94")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["email"][0]["type"] == "cold_email"
        assert any(item["type"] == "phone_gatekeeper" for item in result["phone"])
        assert result["evidence"]["sources"]
    finally:
        app.dependency_overrides.clear()
