from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CompanyEnrichment, ContactEvidence, ObservedJobOffer, PersonContact
from app.services.company_enrichment.contracts import MatchStatus
from app.services.contactability.contracts import (
    ContactConfidence,
    ContactProviderStatus,
    ContactScope,
    ContactTarget,
    ContactType,
)
from app.services.contactability.offer_description import (
    OfferDescriptionContactProvider,
    merge_offer_description_results,
    persist_offer_description_candidates,
)
from app.services.contactability.persistence import list_contact_points
from app.services.scoring.company import EmployerRelationshipStatus


NOW = datetime(2026, 9, 17, 10, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Offer:
    source_offer_id: str
    description: Optional[str]
    source_url: Optional[str] = "https://source.test/offer"
    company_name: Optional[str] = "ACME SAS"
    commune: Optional[str] = "94028"
    location_label: Optional[str] = "Créteil"
    source: str = "france_travail"
    last_seen_at: datetime = NOW


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'offer-description-contact-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def target(
    *, scope=ContactScope.COMPANY,
    relationship=EmployerRelationshipStatus.DIRECT_EMPLOYER,
    siren="123456789", local_key=None, commune=None, label=None,
):
    return ContactTarget(
        company_key="acme", organization_name_snapshot="ACME SAS", scope=scope,
        siren=siren, local_key=local_key, local_commune_snapshot=commune,
        local_location_label_snapshot=label,
        employer_relationship_status=relationship,
        identity_match_status=MatchStatus.HIGH_CONFIDENCE if siren else MatchStatus.NOT_FOUND,
    )


def provider_result(contact_target, *offers):
    return OfferDescriptionContactProvider().discover(contact_target, offers)


def test_extracts_and_normalizes_a_plausible_email_with_short_sourced_evidence():
    result = provider_result(target(), Offer(
        "1", "ACME SAS recrute. Contactez recrutement@ACME.test pour candidater. " + "x" * 500,
    ))

    candidate = result.candidates[0]
    evidence = candidate.evidence[0]
    assert result.status == ContactProviderStatus.COMPLETED
    assert candidate.contact_type == ContactType.EMAIL
    assert candidate.normalized_value == "recrutement@acme.test"
    assert candidate.scope == ContactScope.COMPANY
    assert candidate.confidence_level == ContactConfidence.REVIEW_NEEDED
    assert evidence.source_identifier == "1" and evidence.source_url == "https://source.test/offer"
    assert len(evidence.excerpt) <= 240 and len(evidence.excerpt) < 500


def test_ignores_invalid_and_reserved_example_emails():
    result = provider_result(target(), Offer(
        "1", "Ecrivez à name@example.com, faux@@acme.test ou contact@invalide.",
    ))
    assert result.status == ContactProviderStatus.NOT_FOUND and result.candidates == ()


def test_extracts_french_phone_but_ignores_postal_codes_and_siren_like_numbers():
    result = provider_result(target(), Offer(
        "1", "ACME SAS : téléphone 01 23 45 67 89. Code postal 94000, SIREN 123456789.",
    ))
    assert len(result.candidates) == 1
    assert result.candidates[0].contact_type == ContactType.PHONE
    assert result.candidates[0].normalized_value == "+33123456789"


def test_extracts_http_urls_but_ignores_offer_source_url_duplicates():
    result = provider_result(target(), Offer(
        "1", "ACME SAS : https://source.test/offer et https://careers.acme.test/jobs.",
    ))
    assert len(result.candidates) == 1
    assert result.candidates[0].contact_type == ContactType.WEBSITE
    assert result.candidates[0].normalized_value == "https://careers.acme.test/jobs"


def test_same_value_in_three_offers_creates_one_candidate_and_three_evidence(session):
    result = provider_result(target(), *(
        Offer(str(index), "ACME SAS : recrutement@acme.test") for index in range(1, 4)
    ))
    assert len(result.candidates) == 1 and len(result.candidates[0].evidence) == 3
    persisted = persist_offer_description_candidates(session, result)
    assert len(persisted) == 1 and len(list_contact_points(session, "acme")) == 1
    assert session.query(ContactEvidence).count() == 3


def test_merge_keeps_one_logical_candidate_across_target_level_results():
    first = provider_result(target(), Offer("1", "ACME SAS : recrutement@acme.test"))
    second = provider_result(target(), Offer("2", "ACME SAS : recrutement@acme.test"))
    merged = merge_offer_description_results((first, second))
    assert len(merged.candidates) == 1
    assert len(merged.candidates[0].evidence) == 2


def test_intermediary_is_never_promoted_to_client_company_scope():
    result = provider_result(target(relationship=EmployerRelationshipStatus.INTERMEDIARY), Offer(
        "1", "Pour notre client, contactez agence@interim.test",
    ))
    candidate = result.candidates[0]
    assert candidate.scope == ContactScope.INTERMEDIARY
    assert candidate.confidence_level == ContactConfidence.REVIEW_NEEDED


def test_suspected_intermediary_stays_unknown_and_ambiguous():
    result = provider_result(target(relationship=EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED), Offer(
        "1", "ACME SAS : recrutement@acme.test",
    ))
    candidate = result.candidates[0]
    assert candidate.scope == ContactScope.UNKNOWN
    assert candidate.confidence_level == ContactConfidence.AMBIGUOUS
    assert candidate.siren is None


def test_commune_alone_does_not_make_a_contact_local():
    local_target = target(
        scope=ContactScope.LOCAL, local_key="commune:94:94028", commune="94028", label="Créteil",
    )
    result = provider_result(local_target, Offer(
        "1", "Le poste est situé à Créteil. Contactez recrutement@acme.test",
    ))
    candidate = result.candidates[0]
    assert candidate.scope == ContactScope.UNKNOWN and candidate.local_key is None


def test_explicit_local_contact_context_can_remain_review_needed_not_confirmed():
    local_target = target(
        scope=ContactScope.LOCAL, local_key="commune:94:94028", commune="94028", label="Créteil",
    )
    result = provider_result(local_target, Offer(
        "1", "Pour contacter notre agence de Créteil : recrutement@acme.test",
    ))
    candidate = result.candidates[0]
    assert candidate.scope == ContactScope.LOCAL
    assert candidate.confidence_level == ContactConfidence.REVIEW_NEEDED
    assert candidate.siren is None


def test_provider_never_creates_person_candidates_or_confirmed_contacts(session):
    result = provider_result(target(), Offer("1", "ACME SAS : recrutement@acme.test"))
    persisted = persist_offer_description_candidates(session, result)
    assert persisted[0].confidence_level != ContactConfidence.CONFIRMED
    assert session.query(PersonContact).count() == 0


def test_discovery_and_temp_persistence_do_not_mutate_offers_or_enrichments(session):
    offer = ObservedJobOffer(
        source="france_travail", source_offer_id="1", title="Technicien", description="ACME SAS : recrutement@acme.test",
        company_name="ACME SAS", department_code="94", first_seen_at=NOW, last_seen_at=NOW,
        last_changed_at=NOW, is_active=True, observation_count=1,
    )
    enrichment = CompanyEnrichment(
        company_key="acme", source_company_name="ACME SAS", provider="dinum",
        match_status=MatchStatus.NOT_FOUND, entity_sector_type="unknown", last_attempt_at=NOW,
        attempt_count=1, input_fingerprint="a" * 64,
    )
    session.add_all((offer, enrichment))
    session.flush()
    before = (offer.description, offer.last_seen_at, enrichment.match_status, enrichment.attempt_count)
    result = provider_result(target(), offer)
    persist_offer_description_candidates(session, result)
    assert before == (offer.description, offer.last_seen_at, enrichment.match_status, enrichment.attempt_count)
