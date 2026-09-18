"""Business rules for the pure contact-strategy layer."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.services.contactability.contracts import (
    ContactConfidence, ContactEvidenceInput, ContactPointInput, ContactScope,
    ContactTarget, ContactType, PersonContactInput, PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.persistence import (
    add_contact_evidence, upsert_contact_point, upsert_person_contact,
)
from app.services.contactability.strategy import (
    PreferredChannel, StrategyTargetType, recommend_contact_strategy,
)


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'strategy.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def target(**changes):
    values = dict(company_key="acme", organization_name_snapshot="ACME", scope=ContactScope.COMPANY,
                  siren="123456789", local_key=None, local_commune_snapshot=None,
                  local_location_label_snapshot=None, employer_relationship_status="direct_employer",
                  identity_match_status="matched_high_confidence")
    values.update(changes)
    return ContactTarget(**values)


def person(session, role, **changes):
    values = dict(company_key="acme", organization_name_snapshot="ACME", scope=ContactScope.COMPANY,
                  full_name="Camille Martin", relevance_role=role,
                  confidence_level=ContactConfidence.CONFIRMED,
                  verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW)
    values.update(changes)
    return upsert_person_contact(session, PersonContactInput(**values))


def point(session, kind=ContactType.EMAIL, value="contact@acme.test", **changes):
    values = dict(company_key="acme", organization_name_snapshot="ACME", scope=ContactScope.COMPANY,
                  contact_type=kind, value=value, confidence_level=ContactConfidence.CONFIRMED,
                  verification_status=VerificationStatus.SOURCE_VERIFIED, observed_at=NOW)
    values.update(changes)
    return upsert_contact_point(session, ContactPointInput(**values))


def test_small_structure_without_hr_selects_known_director_without_size_deciding(session):
    director = person(session, PersonRelevanceRole.DIRECTOR)
    result = recommend_contact_strategy(session, target(), employee_range="0-2")
    assert result.target_type == StrategyTargetType.DIRECTOR
    assert result.person_contact_id == director.id
    assert "employee_range_context_only" in result.rationale_codes
    assert result.preferred_channel == PreferredChannel.NONE


def test_small_structure_without_director_remains_unresolved(session):
    assert recommend_contact_strategy(session, target(), employee_range="0-2").target_type == StrategyTargetType.UNRESOLVED


def test_nominative_hr_with_direct_email_wins_over_known_director(session):
    director = person(session, PersonRelevanceRole.DIRECTOR, full_name="Dirigeant")
    hr = person(session, PersonRelevanceRole.HR, full_name="Responsable RH")
    email = point(session, value="rh.personne@acme.test", person_contact_id=hr.id)
    result = recommend_contact_strategy(session, target())
    assert result.target_type == StrategyTargetType.HR
    assert result.person_contact_id == hr.id and result.person_contact_id != director.id
    assert result.contact_point_id == email.id and result.preferred_channel == PreferredChannel.DIRECT_EMAIL


def test_nominative_hr_without_direct_email_uses_switchboard_with_context(session):
    person(session, PersonRelevanceRole.HR)
    switchboard = point(session, ContactType.PHONE, "+33102030405")
    result = recommend_contact_strategy(session, target())
    assert result.contact_point_id == switchboard.id
    assert result.preferred_channel == PreferredChannel.COMPANY_SWITCHBOARD
    assert "demander le service Ressources Humaines" in result.short_context


def test_hr_service_functional_email_is_useful_without_person(session):
    email = point(session, value="recrutement@acme.test")
    result = recommend_contact_strategy(session, target())
    assert result.target_type == StrategyTargetType.RECRUITMENT
    assert result.person_contact_id is None and result.contact_point_id == email.id
    assert result.preferred_channel == PreferredChannel.FUNCTIONAL_EMAIL


def test_local_manager_must_be_explicitly_attached_to_that_local_key(session):
    company_manager = person(session, PersonRelevanceRole.MANAGER)
    local_manager = person(session, PersonRelevanceRole.MANAGER, scope=ContactScope.LOCAL,
                           local_key="commune:94:94028", local_commune_snapshot="94028")
    local_target = target(scope=ContactScope.LOCAL, local_key="commune:94:94028", siren=None,
                         local_commune_snapshot="94028")
    result = recommend_contact_strategy(session, local_target)
    assert result.target_type == StrategyTargetType.MANAGER
    assert result.person_contact_id == local_manager.id and result.person_contact_id != company_manager.id


def test_company_manager_is_not_propagated_to_local_target(session):
    person(session, PersonRelevanceRole.MANAGER)
    local_target = target(scope=ContactScope.LOCAL, local_key="commune:94:94028", siren=None,
                         local_commune_snapshot="94028")
    result = recommend_contact_strategy(session, local_target)
    assert result.person_contact_id is None and result.target_type == StrategyTargetType.UNRESOLVED


def test_intermediary_never_targets_assumed_final_client(session):
    email = point(session, value="contact@cabinet.test", scope=ContactScope.INTERMEDIARY)
    result = recommend_contact_strategy(session, target(scope=ContactScope.INTERMEDIARY))
    assert result.target_type == StrategyTargetType.INTERMEDIARY
    assert result.contact_point_id == email.id
    assert "jamais un client final" in result.short_context


def test_person_without_channel_and_channel_without_person_are_distinct(session):
    hr = person(session, PersonRelevanceRole.HR)
    first = recommend_contact_strategy(session, target())
    assert first.person_contact_id == hr.id and first.preferred_channel == PreferredChannel.NONE
    point(session, value="bonjour@acme.test")
    second = recommend_contact_strategy(session, target())
    assert second.person_contact_id == hr.id
    assert second.preferred_channel == PreferredChannel.NONE


def test_generic_company_channel_is_usable_without_person(session):
    email = point(session, value="bonjour@acme.test")
    result = recommend_contact_strategy(session, target())
    assert result.target_type == StrategyTargetType.COMPANY_GENERAL
    assert result.person_contact_id is None and result.contact_point_id == email.id
    assert result.preferred_channel == PreferredChannel.FUNCTIONAL_EMAIL


def test_no_email_is_ever_generated_and_provenance_and_warnings_are_preserved(session):
    hr = person(session, PersonRelevanceRole.HR, verification_status=VerificationStatus.UNVERIFIED)
    add_contact_evidence(session, ContactEvidenceInput(provider="official_web", source_name="Equipe", source_url="https://acme.test/equipe", observed_at=NOW), person_contact_id=hr.id)
    result = recommend_contact_strategy(session, target(warnings=("Identité à vérifier.",)))
    assert result.contact_point_id is None
    assert "Identité à vérifier." in result.warnings
    assert "rôle identifié n'est pas vérifié" in " ".join(result.warnings)
    assert [(item.provider, item.source_url) for item in result.evidence_references] == [("official_web", "https://acme.test/equipe")]


def test_strategy_is_deterministic(session):
    hr = person(session, PersonRelevanceRole.HR)
    point(session, value="rh@acme.test", person_contact_id=hr.id)
    first = recommend_contact_strategy(session, target())
    second = recommend_contact_strategy(session, target())
    assert first == second
