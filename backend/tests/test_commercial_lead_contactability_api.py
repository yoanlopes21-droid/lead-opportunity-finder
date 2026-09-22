"""Additive contactability representation on the read-only commercial-leads API."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import ObservedJobOffer, VerifiedWebsiteRecord
from app.services.contactability.contracts import (
    ContactConfidence, ContactEvidenceInput, ContactPointInput, ContactScope,
    ContactType, PersonContactInput, PersonRelevanceRole, VerificationStatus,
)
from app.services.contactability.persistence import (
    add_contact_evidence, mark_contact_point_stale, upsert_contact_point,
    upsert_person_contact,
)
from app.services.contactability.providers.official_web.contracts import WebsiteVerificationStatus
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.search_runs import is_actionable_lead


NOW = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lead-contactability-api.sqlite3'}")
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


def offer(session, identifier, company, *, description=None, commune="94028", title="Technicien"):
    session.add(ObservedJobOffer(
        source="france_travail", source_offer_id=identifier, title=title,
        company_name=company, description=description, location_label="Créteil",
        commune=commune, department_code="94", created_at=(NOW - timedelta(days=2)).isoformat(),
        source_url=f"https://offers.test/{identifier}", first_seen_at=NOW - timedelta(days=2),
        last_seen_at=NOW, last_changed_at=NOW, is_active=True, observation_count=1,
    ))


def person(session, key, name, role, *, scope=ContactScope.COMPANY, local_key=None):
    return upsert_person_contact(session, PersonContactInput(
        company_key=key, organization_name_snapshot=key.upper(), scope=scope, local_key=local_key,
        full_name=name, relevance_role=role, confidence_level=ContactConfidence.CONFIRMED,
        verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW,
    ))


def point(session, key, kind, value, *, scope=ContactScope.COMPANY, local_key=None,
          person_contact_id=None, confidence=ContactConfidence.CONFIRMED,
          verification=VerificationStatus.SOURCE_VERIFIED):
    return upsert_contact_point(session, ContactPointInput(
        company_key=key, organization_name_snapshot=key.upper(), scope=scope, local_key=local_key,
        contact_type=kind, value=value, person_contact_id=person_contact_id,
        confidence_level=confidence, verification_status=verification, observed_at=NOW,
    ))


def evidence(session, *, contact_point_id=None, person_contact_id=None, provider="official_web", url="https://acme.test/contact", reason="page_contact"):
    return add_contact_evidence(session, ContactEvidenceInput(
        provider=provider, source_name="page officielle", source_url=url,
        observed_at=NOW, evidence_reason=reason, excerpt="Extrait public court.",
    ), contact_point_id=contact_point_id, person_contact_id=person_contact_id)


def item_for(client, name):
    return next(item for item in client.get("/api/v1/commercial-leads?limit=200").json()["items"] if item["company_name"] == name)


def test_exposes_people_contacts_provenance_and_ranked_strategy(client, session):
    offer(session, "acme", "ACME", title="Développeur")
    hr = person(session, "acme", "Camille Martin", PersonRelevanceRole.HR)
    direct = point(session, "acme", ContactType.EMAIL, "camille.martin@acme.test", person_contact_id=hr.id)
    phone = point(session, "acme", ContactType.PHONE, "+33102030405")
    evidence(session, person_contact_id=hr.id, url="https://acme.test/equipe", reason="role_rh")
    evidence(session, contact_point_id=direct.id)
    evidence(session, contact_point_id=direct.id, provider="offer_description", url="https://offers.test/acme", reason="email_public")
    evidence(session, contact_point_id=phone.id)
    session.commit()

    lead = item_for(client, "ACME")
    assert lead["contact_strategy"]["target_type"] == "hr"
    assert lead["contact_strategy"]["preferred_channel"] == "direct_email"
    assert lead["contact_strategy"]["preferred_contact_point_id"] == direct.id
    assert lead["contact_strategy"]["preferred_person_contact_id"] == hr.id
    assert lead["people"][0]["contact_point_ids"] == [direct.id]
    contact = next(item for item in lead["contacts"] if item["id"] == direct.id)
    assert contact["value"] == "camille.martin@acme.test" and contact["scope"] == "company"
    assert {item["provider"] for item in contact["evidence"]} == {"official_web", "offer_description"}
    assert all(len(item["short_excerpt"] or "") <= 400 for item in contact["evidence"])
    assert lead["contactability_summary"]["recruitment_context"]["active_offer_count"] == 1


def test_hr_without_direct_email_uses_switchboard_and_review_needed_is_not_recommended(client, session):
    offer(session, "switch", "SWITCH")
    hr = person(session, "switch", "Responsable RH", PersonRelevanceRole.HR)
    review = point(session, "switch", ContactType.EMAIL, "maybe@switch.test", confidence=ContactConfidence.REVIEW_NEEDED)
    standard = point(session, "switch", ContactType.PHONE, "+33111111111")
    evidence(session, person_contact_id=hr.id)
    evidence(session, contact_point_id=review.id)
    evidence(session, contact_point_id=standard.id)
    session.commit()

    lead = item_for(client, "SWITCH")
    strategy = lead["contact_strategy"]
    assert strategy["target_type"] == "hr" and strategy["preferred_channel"] == "company_switchboard"
    assert strategy["preferred_contact_point_id"] == standard.id
    assert "demander le service Ressources Humaines" in strategy["short_context"]
    review_json = next(item for item in lead["contacts"] if item["id"] == review.id)
    assert review_json["warnings"] and review.id != strategy["preferred_contact_point_id"]


def test_functional_service_director_unresolved_and_scope_rules(client, session):
    offer(session, "service", "SERVICE")
    functional = point(session, "service", ContactType.EMAIL, "recrutement@service.test")
    evidence(session, contact_point_id=functional.id)
    offer(session, "director", "DIRECTOR")
    boss = person(session, "director", "Alex Dirigeant", PersonRelevanceRole.DIRECTOR)
    evidence(session, person_contact_id=boss.id)
    offer(session, "empty", "EMPTY")
    offer(session, "local", "LOCAL", commune="94029")
    company_phone = point(session, "local", ContactType.PHONE, "+33122222222")
    local_phone = point(session, "local", ContactType.PHONE, "+33133333333", scope=ContactScope.LOCAL, local_key="commune:94:94029")
    evidence(session, contact_point_id=company_phone.id)
    evidence(session, contact_point_id=local_phone.id)
    stale = point(session, "local", ContactType.EMAIL, "old@local.test")
    evidence(session, contact_point_id=stale.id)
    mark_contact_point_stale(session, stale.id)
    session.commit()

    service = item_for(client, "SERVICE")
    assert service["contact_strategy"]["target_type"] == "recruitment"
    assert service["contact_strategy"]["preferred_channel"] == "functional_email"
    director = item_for(client, "DIRECTOR")
    assert director["contact_strategy"]["target_type"] == "director"
    empty = item_for(client, "EMPTY")
    assert empty["contacts"] == [] and empty["people"] == []
    assert empty["contact_strategy"]["target_type"] == "unresolved"
    assert empty["contact_strategy"]["preferred_channel"] == "none"
    assert "canal professionnel public" in empty["contact_strategy"]["missing_information"]
    local = item_for(client, "LOCAL")
    bucket = local["local_opportunities"][0]
    assert local_phone.id in bucket["contact_point_ids"] and company_phone.id not in bucket["contact_point_ids"]
    stale_json = next(item for item in local["contacts"] if item["id"] == stale.id)
    assert stale_json["stale"] is True and stale.id != local["contact_strategy"]["preferred_contact_point_id"]


def test_intermediary_stays_intermediary_and_official_web_rejection_is_safe(client, session):
    offer(session, "cabinet", "APPEL MEDICAL", description="Nous recrutons pour notre client.")
    email = point(session, "appel medical", ContactType.EMAIL, "contact@cabinet.test", scope=ContactScope.INTERMEDIARY)
    evidence(session, contact_point_id=email.id)
    session.add(VerifiedWebsiteRecord(
        company_key="appel medical", target_scope=ContactScope.INTERMEDIARY, local_key=None,
        target_fingerprint="target", candidate_fingerprint="candidate", candidate_set_fingerprint="set",
        provider="official_web", canonical_url="https://tier.test", registrable_domain="tier.test",
        status=WebsiteVerificationStatus.REJECTED, score=99, rejection_reasons=["tiers"],
        attribution_warnings=[], observed_at=NOW, verified_at=NOW, fresh_until=NOW,
        fingerprint="verified-cabinet",
    ))
    session.commit()

    lead = item_for(client, "APPEL MEDICAL")
    assert lead["employer_relationship_status"] == "intermediary"
    assert lead["contact_strategy"]["scope"] == "intermediary"
    assert lead["contact_strategy"]["target_type"] == "intermediary"
    assert lead["contact_strategy"]["preferred_contact_point_id"] == email.id
    website = lead["contactability_summary"]["official_web"]
    assert website["verified_site_status"] == "rejected"
    assert website["verification_score"] == 0 and website["verified_domain"] is None


def test_site_summary_hides_stale_and_obvious_third_party_hypotheses(client, session):
    offer(session, "press", "PRESS")
    offer(session, "structured", "STRUCTURED")
    offer(session, "plausible", "PLAUSIBLE")
    records = [
        ("press", "lefigaro.fr", WebsiteVerificationStatus.AMBIGUOUS, NOW + timedelta(days=30), ["competing_domains"]),
        ("structured", "wikidata.org", WebsiteVerificationStatus.REVIEW_NEEDED, NOW + timedelta(days=30), []),
        ("plausible", "plausible.test", WebsiteVerificationStatus.AMBIGUOUS, NOW + timedelta(days=30), []),
    ]
    for key, domain, status, fresh_until, warnings in records:
        session.add(VerifiedWebsiteRecord(
            company_key=key, target_scope=ContactScope.COMPANY, local_key=None,
            target_fingerprint=f"target-{key}", candidate_fingerprint=f"candidate-{key}", candidate_set_fingerprint=f"set-{key}",
            provider="official_web", canonical_url=f"https://{domain}", registrable_domain=domain,
            status=status, score=70, rejection_reasons=[], attribution_warnings=warnings,
            observed_at=NOW, verified_at=NOW, fresh_until=fresh_until, fingerprint=f"site-{key}",
        ))
    session.commit()

    for name in ("PRESS", "STRUCTURED"):
        website = item_for(client, name)["contactability_summary"]["official_web"]
        assert website["verified_site_status"] == "rejected"
        assert website["verified_domain"] is None and website["verification_score"] == 0
    visible = item_for(client, "PLAUSIBLE")["contactability_summary"]["official_web"]
    assert visible["verified_site_status"] == "ambiguous"
    assert visible["verified_domain"] == "plausible.test"


def test_verified_site_summary_does_not_also_claim_no_site_was_verified(client, session):
    offer(session, "coherent", "COHERENT")
    for suffix, domain, status in (
        ("good", "coherent.fr", WebsiteVerificationStatus.HIGH_CONFIDENCE),
        ("hidden", "directory.example", WebsiteVerificationStatus.REJECTED),
    ):
        session.add(VerifiedWebsiteRecord(
            company_key="coherent", target_scope=ContactScope.COMPANY, local_key=None,
            target_fingerprint="target-coherent", candidate_fingerprint=f"candidate-{suffix}",
            candidate_set_fingerprint="set-coherent", provider="official_web",
            canonical_url=f"https://{domain}", registrable_domain=domain, status=status,
            score=95 if status == WebsiteVerificationStatus.HIGH_CONFIDENCE else 0,
            rejection_reasons=[] if status == WebsiteVerificationStatus.HIGH_CONFIDENCE else ["tiers"],
            attribution_warnings=[], observed_at=NOW, verified_at=NOW,
            fresh_until=NOW + timedelta(days=30), fingerprint=f"site-coherent-{suffix}",
        ))
    session.commit()

    website = item_for(client, "COHERENT")["contactability_summary"]["official_web"]
    assert website["verified_site_status"] == "high_confidence"
    assert website["verified_domain"] == "coherent.fr"
    assert not any("Aucun site officiel" in warning for warning in website["warnings"])


def test_foreign_group_channels_are_kept_but_not_recommended_for_france(client, session):
    offer(session, "lidl", "LIDL")
    email = point(session, "lidl", ContactType.EMAIL, "recruiting@lidl.us")
    evidence(session, contact_point_id=email.id, url="https://careers.lidl.co.uk/contact", reason="recruitment contact")
    session.add(VerifiedWebsiteRecord(
        company_key="lidl", target_scope=ContactScope.COMPANY, local_key=None,
        target_fingerprint="target-lidl", candidate_fingerprint="candidate-lidl",
        candidate_set_fingerprint="set-lidl", provider="official_web",
        canonical_url="https://lidl.co.uk", registrable_domain="lidl.co.uk",
        status=WebsiteVerificationStatus.HIGH_CONFIDENCE, score=95, rejection_reasons=[],
        attribution_warnings=[], observed_at=NOW, verified_at=NOW,
        fresh_until=NOW + timedelta(days=30), fingerprint="site-lidl",
    ))
    session.commit()

    lead = item_for(client, "LIDL")
    contact = next(item for item in lead["contacts"] if item["id"] == email.id)
    assert contact["commercial_relevance"] == "irrelevant_foreign"
    assert contact["verification_status"] == "source_verified"
    assert lead["contact_strategy"]["preferred_channel"] == "none"
    assert any("entité étrangère" in warning for warning in lead["contact_strategy"]["warnings"])
    composed = next(item for item in list_commercial_leads(
        session, CommercialLeadQuery(limit=200), now=NOW,
    ).items if item.company_name == "LIDL")
    assert not is_actionable_lead(composed)


def test_explicit_french_national_recruitment_channel_remains_recommended(client, session):
    offer(session, "apef", "APEF")
    phone = point(session, "apef", ContactType.PHONE, "+33467153143")
    evidence(
        session, contact_point_id=phone.id, url="https://apefrecrute.fr/contact",
        reason="contact recrutement France",
    )
    session.commit()

    lead = item_for(client, "APEF")
    assert lead["contact_strategy"]["preferred_contact_point_id"] == phone.id
    assert lead["contact_strategy"]["channel_relevance"] == "national_france"
    assert any("national ou groupe" in warning for warning in lead["contact_strategy"]["warnings"])
    composed = next(item for item in list_commercial_leads(
        session, CommercialLeadQuery(limit=200), now=NOW,
    ).items if item.company_name == "APEF")
    assert is_actionable_lead(composed)


def test_contactability_loader_uses_bounded_selects_for_many_leads(client, session):
    for index in range(24):
        key = f"many-{index}"
        offer(session, key, key.upper())
        email = point(session, key, ContactType.EMAIL, f"contact{index}@many.test")
        evidence(session, contact_point_id=email.id)
    session.commit()
    selects = []

    @event.listens_for(session.bind, "before_cursor_execute")
    def count_selects(_, __, statement, ___, ____, _____):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    try:
        response = client.get("/api/v1/commercial-leads?limit=24")
    finally:
        event.remove(session.bind, "before_cursor_execute", count_selects)
    assert response.status_code == 200
    # Aggregation and enrichment have their own reads; contactability adds four
    # bounded set queries (points, people, evidence, verified sites), not N per lead.
    assert len(selects) <= 10
