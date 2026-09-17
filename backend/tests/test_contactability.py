from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CompanyEnrichment, ObservedJobOffer
from app.services.commercial_leads.service import CommercialLead
from app.services.company_enrichment.contracts import MatchStatus
from app.services.contactability.contracts import (
    ContactConfidence,
    ContactEvidenceInput,
    ContactPointInput,
    ContactScope,
    ContactType,
    PersonContactInput,
    PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.normalization import (
    contact_point_fingerprint,
    normalize_email,
    normalize_phone,
    normalize_url,
)
from app.services.contactability.persistence import (
    add_contact_evidence,
    list_contact_points,
    mark_contact_point_stale,
    upsert_contact_point,
    upsert_person_contact,
)
from app.services.contactability.targets import (
    ContactIdentityContext,
    build_contact_targets,
    is_vague_local_target,
)
from app.services.opportunities.company import LocalOpportunity
from app.services.opportunities.intermediary import IntermediaryDescriptionEvidence
from app.services.scoring.company import (
    CompanyScoringResult,
    EmployerRelationshipStatus,
    ScoreSubscores,
)


NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contactability-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def point_input(**overrides):
    values = {
        "company_key": "acme", "organization_name_snapshot": "ACME SAS",
        "scope": ContactScope.COMPANY, "contact_type": ContactType.EMAIL,
        "value": "contact@acme.test", "confidence_level": ContactConfidence.HIGH_CONFIDENCE,
        "verification_status": VerificationStatus.SOURCE_VERIFIED, "observed_at": NOW,
    }
    values.update(overrides)
    return ContactPointInput(**values)


def person_input(**overrides):
    values = {
        "company_key": "acme", "organization_name_snapshot": "ACME SAS",
        "scope": ContactScope.COMPANY, "full_name": "Élodie Martin",
        "relevance_role": PersonRelevanceRole.RECRUITMENT,
        "confidence_level": ContactConfidence.HIGH_CONFIDENCE,
        "verification_status": VerificationStatus.SOURCE_VERIFIED, "observed_at": NOW,
    }
    values.update(overrides)
    return PersonContactInput(**values)


def local(key="commune:94:94028", commune="94028", label="Créteil"):
    return LocalOpportunity(
        local_key=key, commune=commune, location_label=label, department_code="94",
        active_offer_count=2, distinct_job_title_count=1,
        representative_job_titles=("Technicien",), oldest_offer_created_at=None,
        newest_offer_created_at=None, source_offer_ids=("france_travail:1",),
        source_urls=("https://example.test/offer",), signals=(),
    )


def lead(relationship=EmployerRelationshipStatus.DIRECT_EMPLOYER, siren="123456789", locals_=(local(),)):
    scoring = CompanyScoringResult(
        company_key="acme", company_name="ACME SAS", department_code="94", total_score=50,
        category="medium", subscores=ScoreSubscores(0, 0, 0, 0, 0),
        positive_reasons=(), commercial_adjustments=(), penalties=(), signals_used=(),
        employer_relationship_status=relationship, employer_relationship_reasons=(),
        intermediary_description_evidence=IntermediaryDescriptionEvidence(),
    )
    return CommercialLead(
        company_key="acme", company_name="ACME SAS", official_name="ACME SAS" if siren else None,
        siren=siren, siret="12345678900010" if siren else None, entity_sector_type="private",
        employee_range="20-49", principal_location="Créteil", active_offer_count=2,
        distinct_job_title_count=1, representative_job_titles=("Technicien",),
        newest_offer_created_at=None, oldest_offer_created_at=None, local_opportunities=locals_,
        latent_signals=(), scoring=scoring, evidence=(), is_eligible=True,
        exclusion=None,
    )


def test_company_contact_point_is_valid_without_siren_and_can_be_staled(session):
    point = upsert_contact_point(session, point_input(siren=None))
    assert point.id and point.siren is None and point.normalized_value == "contact@acme.test"
    assert mark_contact_point_stale(session, point.id).verification_status == VerificationStatus.STALE
    assert list_contact_points(session, "acme") == ()
    assert list_contact_points(session, "acme", include_inactive=True) == (point,)


def test_local_contact_requires_local_key_and_company_and_local_values_are_distinct(session):
    with pytest.raises(ValueError, match="local scope requires local key"):
        upsert_contact_point(session, point_input(scope=ContactScope.LOCAL))
    company = upsert_contact_point(session, point_input())
    local_point = upsert_contact_point(session, point_input(
        scope=ContactScope.LOCAL, local_key="commune:94:94028", local_commune_snapshot="94028",
    ))
    assert company.id != local_point.id


def test_normalisation_and_contact_fingerprints_are_deterministic():
    assert normalize_email(" Contact@ACME.Test ") == "contact@acme.test"
    assert normalize_phone("01 23 45 67 89") == "+33123456789"
    assert normalize_phone("+33 (0)1 23 45 67 89") == "+33123456789"
    assert normalize_url("HTTPS://Example.TEST:443/contact/#team") == "https://example.test/contact"
    first = contact_point_fingerprint(
        company_key="ACME", scope=ContactScope.COMPANY, local_key=None,
        contact_type=ContactType.EMAIL, normalized_value="contact@acme.test", person_contact_id=None,
    )
    second = contact_point_fingerprint(
        company_key=" acme ", scope=ContactScope.COMPANY, local_key=None,
        contact_type=ContactType.EMAIL, normalized_value="contact@acme.test", person_contact_id=None,
    )
    assert first == second


def test_upsert_and_multiple_evidence_do_not_duplicate_the_contact(session):
    point = upsert_contact_point(session, point_input())
    refreshed = upsert_contact_point(session, point_input(observed_at=NOW + timedelta(days=1)))
    assert refreshed.id == point.id and refreshed.last_observed_at == NOW + timedelta(days=1)
    first = add_contact_evidence(session, ContactEvidenceInput(
        provider="official_site", source_name="Page contact", source_url="https://acme.test/contact",
        observed_at=NOW,
    ), contact_point_id=point.id)
    second = add_contact_evidence(session, ContactEvidenceInput(
        provider="official_site", source_name="Mentions légales", source_url="https://acme.test/legal",
        observed_at=NOW,
    ), contact_point_id=point.id)
    assert first.id != second.id
    assert len(list_contact_points(session, "acme")) == 1


def test_person_contact_uses_linked_contact_point_for_email(session):
    person = upsert_person_contact(session, person_input())
    email = upsert_contact_point(session, point_input(person_contact_id=person.id))
    evidence = add_contact_evidence(session, ContactEvidenceInput(
        provider="official_site", source_name="Équipe", observed_at=NOW,
    ), person_contact_id=person.id)
    assert person.normalized_name == "elodie martin"
    assert email.person_contact_id == person.id and evidence.person_contact_id == person.id


def test_evidence_requires_exactly_one_existing_target(session):
    evidence = ContactEvidenceInput(provider="test", source_name="source", observed_at=NOW)
    with pytest.raises(ValueError, match="exactly one"):
        add_contact_evidence(session, evidence)
    point = upsert_contact_point(session, point_input())
    with pytest.raises(ValueError, match="exactly one"):
        add_contact_evidence(session, evidence, contact_point_id=point.id, person_contact_id=1)


def test_direct_employer_builds_company_and_local_targets_without_local_siren():
    targets = build_contact_targets(lead(locals_=(local(), local("location:94:vitry", None, "Vitry-sur-Seine"))))
    assert [target.scope for target in targets] == [ContactScope.COMPANY, ContactScope.LOCAL, ContactScope.LOCAL]
    assert targets[0].siren == "123456789"
    assert all(target.siren is None for target in targets[1:])
    assert is_vague_local_target(targets[2])
    assert "non structurée" in " ".join(targets[2].warnings)


def test_not_found_identity_keeps_company_target_without_siren():
    target = build_contact_targets(
        lead(siren=None, locals_=()), ContactIdentityContext(match_status=MatchStatus.NOT_FOUND)
    )[0]
    assert target.scope == ContactScope.COMPANY and target.siren is None
    assert target.identity_match_status == MatchStatus.NOT_FOUND


def test_intermediary_and_suspected_targets_remain_distinct():
    intermediary = build_contact_targets(lead(EmployerRelationshipStatus.INTERMEDIARY))[0]
    suspected = build_contact_targets(lead(EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED))[0]
    assert intermediary.scope == ContactScope.INTERMEDIARY
    assert suspected.scope == ContactScope.COMPANY
    assert "possible" in " ".join(suspected.warnings)


def test_contactability_does_not_mutate_offer_or_enrichment(session):
    offer = ObservedJobOffer(
        source="test", source_offer_id="1", title="Technicien", company_name="ACME SAS",
        department_code="94", first_seen_at=NOW, last_seen_at=NOW, last_changed_at=NOW,
        is_active=True, observation_count=1,
    )
    enrichment = CompanyEnrichment(
        company_key="acme", source_company_name="ACME SAS", provider="dinum",
        match_status=MatchStatus.NOT_FOUND, entity_sector_type="unknown", last_attempt_at=NOW,
        attempt_count=1, input_fingerprint="a" * 64,
    )
    session.add_all((offer, enrichment))
    session.flush()
    before = (offer.last_seen_at, offer.observation_count, enrichment.match_status, enrichment.attempt_count)
    upsert_contact_point(session, point_input())
    session.flush()
    assert before == (offer.last_seen_at, offer.observation_count, enrichment.match_status, enrichment.attempt_count)
