"""Synthetic contracts for reliable commercial inputs; no real prospect fixtures."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import CommercialExclusion, CommercialRelationship, CompanyEnrichment, ObservedJobOffer
from app.services.commercial_leads.approach import ApproachReadiness, get_commercial_approach_context
from app.services.contactability.contracts import (
    ContactConfidence, ContactEvidenceInput, ContactPointInput, ContactScope, ContactType,
    PersonContactInput, PersonRelevanceRole, VerificationStatus,
)
from app.services.contactability.persistence import (
    add_contact_evidence, upsert_contact_point, upsert_person_contact,
)


NOW = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'approach.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def offer(session, *, company="NOVA", identifier="one", title="Technicien", location="94 - Créteil",
          age_days=2, description=None, source="france_travail", commune="94028"):
    session.add(ObservedJobOffer(
        source=source, source_offer_id=identifier, title=title, company_name=company,
        description=description, location_label=location, commune=commune,
        department_code="94", created_at=(NOW - timedelta(days=age_days)).isoformat(),
        updated_at=None, contract_type="CDI", salary=None,
        source_url=f"https://jobs.example.test/{identifier}",
        first_seen_at=NOW - timedelta(days=age_days), last_seen_at=NOW,
        last_changed_at=NOW, is_active=True, observation_count=3,
    ))
    session.flush()


def point(session, *, value="+33102030405", kind=ContactType.PHONE, scope=ContactScope.COMPANY,
          local_key=None, source_url="https://nova.example.test/contact", excerpt=None,
          confidence=ContactConfidence.HIGH_CONFIDENCE, verification=VerificationStatus.SOURCE_VERIFIED,
          company="nova", person_contact_id=None):
    row = upsert_contact_point(session, ContactPointInput(
        company_key=company, organization_name_snapshot=company.upper(), scope=scope,
        local_key=local_key, contact_type=kind, value=value, confidence_level=confidence,
        verification_status=verification, observed_at=NOW, person_contact_id=person_contact_id,
    ))
    add_contact_evidence(session, ContactEvidenceInput(
        provider="official_web", source_name="Contact", source_url=source_url,
        excerpt=excerpt, observed_at=NOW,
    ), contact_point_id=row.id)
    return row


def context(session, company="nova"):
    return get_commercial_approach_context(session, company, now=NOW)


def test_minimal_lead_without_person_siren_or_channel_remains_preparable(session):
    offer(session)
    result = context(session)
    assert result.readiness == ApproachReadiness.CHANNEL_MISSING
    assert result.approach_preparable and not result.contact_now_possible
    assert result.siren is None and result.recommended_contact_id is None
    assert "named_contact" in result.missing_information
    assert "confirmed_legal_identity" in result.missing_information
    assert result.entry_offer.title == "Technicien"


def test_sourced_switchboard_can_be_used_for_routing(session):
    offer(session)
    phone = point(session)
    result = context(session)
    assert result.readiness == ApproachReadiness.ROUTING_REQUIRED
    assert result.contact_now_possible and result.recommended_contact_id == phone.id
    assert result.recommended_channel == "company_switchboard"
    assert result.contacts[0].scope == ContactScope.COMPANY


def test_general_email_is_routing_not_confirmed_hr(session):
    offer(session)
    email = point(session, value="contact@nova.example.test", kind=ContactType.EMAIL)
    result = context(session)
    assert result.readiness == ApproachReadiness.ROUTING_REQUIRED
    assert result.recommended_contact_id == email.id
    assert result.recommended_channel == "general_email"
    assert result.contacts[0].role == "general_routing"
    assert "confirmed_hr_recipient" in result.prohibited_claims


def test_direct_hr_email_is_ready_to_contact(session):
    offer(session)
    person = upsert_person_contact(session, PersonContactInput(
        company_key="nova", organization_name_snapshot="NOVA", scope=ContactScope.COMPANY,
        full_name="Camille Martin", relevance_role=PersonRelevanceRole.HR,
        confidence_level=ContactConfidence.HIGH_CONFIDENCE,
        verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW,
    ))
    email = point(session, value="camille@nova.example.test", kind=ContactType.EMAIL,
                  person_contact_id=person.id)
    result = context(session)
    assert result.readiness == ApproachReadiness.READY_TO_CONTACT
    assert result.recommended_contact_id == email.id
    assert result.recommended_person_contact_id == person.id


def test_known_hr_without_direct_channel_can_route_via_general_email(session):
    offer(session)
    upsert_person_contact(session, PersonContactInput(
        company_key="nova", organization_name_snapshot="NOVA", scope=ContactScope.COMPANY,
        full_name="Camille Martin", relevance_role=PersonRelevanceRole.HR,
        confidence_level=ContactConfidence.HIGH_CONFIDENCE,
        verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW,
    ))
    email = point(session, value="contact@nova.example.test", kind=ContactType.EMAIL)
    result = context(session)
    assert result.readiness == ApproachReadiness.ROUTING_REQUIRED
    assert result.recommended_contact_id == email.id
    assert result.recommended_person_contact_id is None


def test_unproven_contact_is_visible_but_not_recommended(session):
    offer(session)
    candidate = upsert_contact_point(session, ContactPointInput(
        company_key="nova", organization_name_snapshot="NOVA", scope=ContactScope.COMPANY,
        contact_type=ContactType.EMAIL, value="contact@nova.example.test",
        confidence_level=ContactConfidence.HIGH_CONFIDENCE,
        verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW,
    ))
    result = context(session)
    assert result.readiness == ApproachReadiness.VERIFY_CONTACT
    assert result.recommended_contact_id is None
    assert next(item for item in result.contacts if item.id == candidate.id).reason_codes == ("contact_provenance_missing",)


@pytest.mark.parametrize("address,url,excerpt", [
    ("dpd@nova.example.test", "https://nova.example.test/politique-de-confidentialite", "Délégué à la protection des données"),
    ("presse@nova.example.test", "https://nova.example.test/contact", "Relations médias"),
    ("support@nova.example.test", "https://nova.example.test/contact", "Support clients"),
])
def test_unrelated_functional_mailboxes_are_rejected(session, address, url, excerpt):
    offer(session)
    candidate = point(session, value=address, kind=ContactType.EMAIL, source_url=url, excerpt=excerpt)
    result = context(session)
    assert result.recommended_contact_id is None
    assert next(item for item in result.contacts if item.id == candidate.id).use == "rejected"
    assert result.readiness == ApproachReadiness.CHANNEL_MISSING


def test_foreign_channel_is_not_recommended_for_french_need(session):
    offer(session)
    candidate = point(session, value="recruiting@nova.us", kind=ContactType.EMAIL,
                      source_url="https://nova.us/contact", excerpt="United States recruiting")
    result = context(session)
    assert result.recommended_contact_id is None
    assert next(item for item in result.contacts if item.id == candidate.id).use == "rejected"


def test_ambiguous_attribution_requires_verification_even_with_a_phone(session):
    offer(session, description="Et si c'était Nova Services qu'il vous fallait ? Rejoignez l'équipe.")
    point(session)
    result = context(session)
    assert result.employer_relationship_status == "direct_employer"
    assert result.employer_attribution == "attribution_ambiguous"
    assert result.readiness == ApproachReadiness.VERIFY_EMPLOYER
    assert result.approach_preparable and not result.contact_now_possible


def test_generic_multi_role_publication_is_not_confirmed_employer(session):
    for number, title in enumerate(("Vendeur", "Cuisinier", "Comptable")):
        offer(session, identifier=str(number), title=title,
              description="Offre collectée par La bonne alternance : description générique")
    result = context(session)
    assert result.employer_attribution == "attribution_ambiguous"
    assert "generic_multi_role_publication" in result.employer_reasons


def test_confirmed_intermediary_is_not_treated_as_final_employer(session):
    offer(session, company="NOVA RECRUTEMENT", description="Nous recrutons pour notre client.")
    result = context(session, "nova recrutement")
    assert result.employer_attribution == "intermediary_confirmed"
    assert result.readiness == ApproachReadiness.INTERMEDIARY_NOT_EMPLOYER
    assert not result.approach_preparable and not result.contact_now_possible


def test_job_location_is_from_entry_offer_not_confirmed_legal_address(session):
    offer(session, location="94 - Ivry-sur-Seine")
    session.add(CompanyEnrichment(
        company_key="nova", source_company_name="NOVA", provider="dinum",
        match_status="matched_high_confidence", entity_sector_type="private",
        siren="123456789", siret="12345678900010", official_name="NOVA",
        address="10 rue du Siège, Paris", commune="Paris",
        last_attempt_at=NOW, attempt_count=1, input_fingerprint="nova-identity",
    ))
    result = context(session)
    assert result.entry_offer.location == "94 - Ivry-sur-Seine"
    assert result.official_name == "NOVA" and result.siren == "123456789"
    assert all("Paris" not in fact.value for fact in result.usable_facts)


def test_missing_job_location_stays_unknown(session):
    offer(session, location=None, commune=None)
    result = context(session)
    assert result.entry_offer.location is None
    assert "job_location" in result.missing_information
    assert "job_location_claim" in result.prohibited_claims


def test_stale_last_observation_requires_offer_verification(session):
    offer(session, age_days=20)
    row = session.query(ObservedJobOffer).filter_by(source_offer_id="one").one()
    row.last_seen_at = NOW - timedelta(days=9)
    point(session)
    result = context(session)
    assert result.readiness == ApproachReadiness.VERIFY_OFFER
    assert result.approach_preparable and not result.contact_now_possible
    assert "currently_open_job_claim" in result.prohibited_claims


def test_entry_offer_prefers_attributed_local_need_over_newer_unlocated_offer(session):
    offer(session, identifier="located", title="Technicien atelier", age_days=4,
          location="94 - Créteil")
    offer(session, identifier="newer", title="Technicien", age_days=1,
          location=None, commune=None)
    result = context(session)
    assert result.entry_offer.offer_id == "located"
    assert "quality_preferred_over_newest" in result.entry_offer.selection_reasons


def test_canonical_needs_and_source_listings_are_not_position_count(session):
    offer(session, source="france_travail", identifier="ft-one")
    offer(session, source="greenhouse", identifier="gh-one")
    result = context(session)
    assert result.canonical_need_count == 1
    assert result.source_listing_count == 2
    assert result.entry_offer.source_observation_count == 2
    assert "unproven_vacancy_count" in result.prohibited_claims


@pytest.mark.parametrize("kind", ["current_client", "manual_exclusion"])
def test_active_exclusions_suspend_approach(session, kind):
    offer(session)
    session.add(CommercialExclusion(
        company_key="nova", company_name_snapshot="NOVA", exclusion_type=kind,
        starts_at=NOW - timedelta(days=1),
    ))
    result = context(session)
    assert result.readiness == ApproachReadiness.SUSPENDED
    assert not result.approach_preparable and not result.contact_now_possible
    assert result.exclusion_type == kind


def test_active_do_not_contact_relationship_suspends(session):
    offer(session)
    session.add(CommercialRelationship(
        company_key="nova", company_name_snapshot="NOVA", status="do_not_contact",
        is_active=True, created_at=NOW, updated_at=NOW,
    ))
    result = context(session)
    assert result.readiness == ApproachReadiness.SUSPENDED


def test_name_only_manual_exclusion_remains_effective_after_siren_confirmation(session):
    offer(session)
    session.add(CompanyEnrichment(
        company_key="nova", source_company_name="NOVA", provider="dinum",
        match_status="matched_high_confidence", entity_sector_type="private",
        siren="123456789", official_name="NOVA", last_attempt_at=NOW,
        attempt_count=1, input_fingerprint="nova-confirmed",
    ))
    session.add(CommercialExclusion(
        company_key="nova", siren=None, company_name_snapshot="NOVA",
        exclusion_type="manual_exclusion", starts_at=NOW - timedelta(days=1),
    ))
    result = context(session)
    assert result.siren == "123456789"
    assert result.readiness == ApproachReadiness.SUSPENDED


def test_inactive_relationship_is_preserved_without_first_contact_claim(session):
    offer(session)
    session.add(CommercialRelationship(
        company_key="nova", company_name_snapshot="NOVA", status="contacted",
        is_active=False, last_contact_at=NOW - timedelta(days=3),
        created_at=NOW - timedelta(days=4), updated_at=NOW,
    ))
    result = context(session)
    assert result.readiness == ApproachReadiness.CHANNEL_MISSING
    assert result.active_relationship_status is None
    assert len(result.relationship_history) == 1
    assert "first_contact_claim_without_review" in result.prohibited_claims


def test_specific_site_number_is_not_silently_used_as_company_switchboard(session):
    offer(session, location="94 - Créteil")
    candidate = point(session, source_url="https://nova.example.test/residence/vitry")
    result = context(session)
    assert result.recommended_contact_id is None
    assert next(item for item in result.contacts if item.id == candidate.id).use == "verify"
    assert result.readiness == ApproachReadiness.VERIFY_CONTACT


def test_contact_with_another_local_key_is_not_used(session):
    offer(session)
    candidate = point(session, scope=ContactScope.LOCAL, local_key="commune:94:94081")
    result = context(session)
    assert result.recommended_contact_id is None
    assert next(item for item in result.contacts if item.id == candidate.id).use == "rejected"


def test_contact_at_exact_local_key_is_usable_only_for_that_need(session):
    offer(session)
    local = point(session, scope=ContactScope.LOCAL, local_key="commune:94:94028",
                  source_url="https://nova.example.test/agence/creteil")
    result = context(session)
    assert result.readiness == ApproachReadiness.ROUTING_REQUIRED
    assert result.recommended_contact_id == local.id
    assert result.contacts[0].scope == ContactScope.LOCAL
    assert result.contacts[0].reach == ContactScope.LOCAL


def test_read_only_api_exposes_context_and_returns_404_for_unknown_lead(session):
    offer(session)
    app.dependency_overrides[get_db] = lambda: (yield session)
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/commercial-leads/nova/approach-context")
            missing = client.get("/api/v1/commercial-leads/unknown/approach-context")
        assert response.status_code == 200
        assert response.json()["readiness"] == ApproachReadiness.CHANNEL_MISSING
        assert response.json()["entry_offer"]["location"] == "94 - Créteil"
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()
