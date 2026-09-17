from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ContactEvidence, ContactPoint, PersonContact
from app.services.company_enrichment.contracts import MatchStatus
from app.services.contactability.contracts import (
    ContactConfidence,
    ContactProviderStatus,
    ContactScope,
    ContactTarget,
    ContactType,
    PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.providers.societe_com import (
    SocieteComClient,
    SocieteComClientError,
    SocieteComContactProvider,
    SocieteComPolicy,
    persist_societe_com_result,
    societe_com_target_fingerprint,
)


NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
TOKEN = "test-token-that-must-never-be-exposed"


def target(**overrides):
    values = {
        "company_key": "acme",
        "organization_name_snapshot": "ACME SAS",
        "scope": ContactScope.COMPANY,
        "siren": "123456789",
        "local_key": None,
        "local_commune_snapshot": None,
        "local_location_label_snapshot": None,
        "employer_relationship_status": "direct_employer",
        "identity_match_status": MatchStatus.HIGH_CONFIDENCE,
        "warnings": (),
    }
    values.update(overrides)
    return ContactTarget(**values)


class FakeClient:
    def __init__(self, contact=None, directors=None, error=None):
        self.contact = contact
        self.directors = directors
        self.error = error
        self.calls = []

    def fetch_contact(self, numid):
        self.calls.append(("contact", numid))
        if self.error:
            raise self.error
        return self.contact

    def fetch_directors(self, numid):
        self.calls.append(("directors", numid))
        if self.error:
            raise self.error
        return self.directors

    def logical_url(self, numid, route):
        return f"https://api.societe.com/api/v1/entreprise/{numid}/{route}"


def contact_payload(**overrides):
    values = {
        "siret": "12345678900011",
        "actif": True,
        "diffusible": True,
        "raisonsociale": "ACME SAS",
        "ville": "Créteil",
        "tel": "01 23 45 67 89",
        "email": "Contact@ACME.Test",
        "siteweb": "HTTPS://WWW.ACME.TEST/contact/",
    }
    values.update(overrides)
    return values


def provider(client, token=TOKEN):
    return SocieteComContactProvider(
        token=token,
        client=client,
        now=lambda: NOW,
        policy=SocieteComPolicy(
            contact_ttl=timedelta(days=30), directors_ttl=timedelta(days=90),
        ),
    )


def test_absent_token_is_not_configured_without_network_call(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: pytest.fail("network called"))
    fake = FakeClient(contact=contact_payload())
    result = provider(fake, token=None).discover(target())
    assert result.status == ContactProviderStatus.NOT_CONFIGURED
    assert result.metadata.request_count == 0
    assert fake.calls == []


@pytest.mark.parametrize(
    "ineligible_target",
    [
        target(siren=None),
        target(scope=ContactScope.INTERMEDIARY),
        target(scope=ContactScope.LOCAL, local_key="commune:creteil"),
        target(identity_match_status=MatchStatus.NOT_FOUND, siren=None),
        target(identity_match_status=MatchStatus.AMBIGUOUS, siren=None),
    ],
)
def test_non_confirmed_or_non_company_targets_are_not_applicable(ineligible_target):
    fake = FakeClient(contact=contact_payload())
    result = provider(fake).discover(ineligible_target)
    assert result.status == ContactProviderStatus.NOT_APPLICABLE
    assert result.candidates == () and result.person_candidates == ()
    assert fake.calls == []


def test_contact_route_maps_and_normalizes_three_company_points():
    result = provider(FakeClient(contact=contact_payload())).discover(target())
    assert result.status == ContactProviderStatus.COMPLETED
    assert {item.contact_type for item in result.candidates} == {
        ContactType.PHONE, ContactType.EMAIL, ContactType.WEBSITE,
    }
    by_type = {item.contact_type: item for item in result.candidates}
    assert by_type[ContactType.PHONE].normalized_value == "+33123456789"
    assert by_type[ContactType.EMAIL].normalized_value == "contact@acme.test"
    assert by_type[ContactType.WEBSITE].normalized_value == "https://www.acme.test/contact"
    assert all(item.scope == ContactScope.COMPANY for item in result.candidates)
    assert all(item.siren == "123456789" for item in result.candidates)
    assert all(item.confidence_level == ContactConfidence.HIGH_CONFIDENCE for item in result.candidates)
    assert all(item.verification_status == VerificationStatus.SOURCE_VERIFIED for item in result.candidates)


@pytest.mark.parametrize(
    "payload",
    [
        contact_payload(diffusible=False),
        contact_payload(actif=False),
        contact_payload(tel="", email=None, siteweb=" "),
    ],
)
def test_empty_non_diffusible_or_inactive_contact_values_are_ignored(payload):
    result = provider(FakeClient(contact=payload)).discover(target())
    assert result.candidates == ()


def test_identity_mismatch_is_never_promoted_to_high_confidence():
    name_mismatch = provider(FakeClient(
        contact=contact_payload(raisonsociale="OTHER COMPANY"),
    )).discover(target())
    assert name_mismatch.candidates
    assert all(
        item.confidence_level == ContactConfidence.AMBIGUOUS
        for item in name_mismatch.candidates
    )
    identifier_mismatch = provider(FakeClient(
        contact=contact_payload(siret="98765432100010"),
    )).discover(target())
    assert identifier_mismatch.candidates == ()
    assert "societe_com_identifier_mismatch" in identifier_mismatch.warnings


def test_physical_director_is_mapped_without_private_address():
    result = provider(FakeClient(
        contact=None,
        directors={
            "siren": "123456789",
            "raisonsociale": "ACME SAS",
            "dirigeants": [{
                "type": "personne physique",
                "prenom": "Élodie",
                "nom": "Martin",
                "fonction": "Présidente",
                "adresse_personnelle": "must never be retained",
            }],
        },
    )).discover(target())
    assert len(result.person_candidates) == 1
    person = result.person_candidates[0]
    assert person.full_name == "Élodie Martin"
    assert person.job_title == "Présidente"
    assert person.relevance_role == PersonRelevanceRole.DIRECTOR
    assert "adresse" not in person.__dataclass_fields__
    assert "must never be retained" not in repr(person)


def test_legal_entity_director_and_beneficial_owner_data_are_ignored():
    result = provider(FakeClient(
        directors={"dirigeants": [
            {"type": "personne morale", "denomination": "HOLDING SAS", "fonction": "Président"},
            {"type": "beneficiaire effectif", "nom_complet": "Private Owner"},
        ]},
    )).discover(target())
    assert result.person_candidates == ()


def test_persistence_deduplicates_facts_and_evidence_without_raw_payload(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'societe-contact.sqlite3'}")
    Base.metadata.create_all(engine)
    raw_secret = "private-address-never-persisted"
    result = provider(FakeClient(
        contact=contact_payload(),
        directors={
            "siren": "123456789", "raisonsociale": "ACME SAS",
            "dirigeants": [{
                "type": "personne physique", "prenom": "Élodie", "nom": "Martin",
                "fonction": "Gérante", "adresse": raw_secret,
            }],
        },
    )).discover(target())
    with Session(engine) as session:
        first = persist_societe_com_result(session, result)
        second = persist_societe_com_result(session, result)
        session.commit()
        assert [row.id for row in first.contact_points] == [row.id for row in second.contact_points]
        assert [row.id for row in first.person_contacts] == [row.id for row in second.person_contacts]
        assert len(session.scalars(select(ContactPoint)).all()) == 3
        assert len(session.scalars(select(PersonContact)).all()) == 1
        evidence = session.scalars(select(ContactEvidence)).all()
        assert len(evidence) == 4
        serialized = " ".join(str(row.__dict__) for row in evidence)
        assert TOKEN not in serialized and raw_secret not in serialized
    engine.dispose()


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx.ReadTimeout("late"), "timeout"),
        (429, "rate_limited"),
        (503, "server_error"),
    ],
)
def test_client_returns_sanitized_timeout_429_and_5xx_errors(failure, kind):
    class Response:
        status_code = failure if isinstance(failure, int) else 200

        @staticmethod
        def json():
            return {}

    def requester(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return Response()

    client = SocieteComClient(token=TOKEN, requester=requester)
    with pytest.raises(SocieteComClientError) as caught:
        client.fetch_contact("123456789")
    assert caught.value.kind == kind
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)


def test_provider_converts_client_failure_to_sanitized_error_result():
    result = provider(FakeClient(
        error=SocieteComClientError("rate_limited", "Societe.com rate limit reached"),
    )).discover(target())
    assert result.status == ContactProviderStatus.ERROR
    assert result.metadata.error_type == "rate_limited"
    assert result.metadata.request_count == 1
    assert TOKEN not in repr(result)


def test_mocked_client_uses_expected_header_and_never_places_token_in_url():
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"siret": "12345678900011"}

    def requester(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    client = SocieteComClient(token=TOKEN, requester=requester)
    client.fetch_contact("123456789")
    assert calls[0][1]["headers"] == {"X-Authorization": f"socapi {TOKEN}"}
    assert TOKEN not in calls[0][0]


def test_target_fingerprint_and_ttls_are_stable_and_cost_aware():
    assert societe_com_target_fingerprint(target()) == societe_com_target_fingerprint(target())
    changed = societe_com_target_fingerprint(target(siren="987654321"))
    assert changed != societe_com_target_fingerprint(target())
    policy = SocieteComPolicy()
    assert policy.contact_ttl == timedelta(days=30)
    assert timedelta(days=60) <= policy.directors_ttl <= timedelta(days=90)


def test_resource_discovery_calls_only_the_requested_paid_route():
    fake = FakeClient(contact=contact_payload(), directors={"dirigeants": []})
    selected = provider(fake)
    contact = selected.discover_resource(target(), "contact")
    assert contact.status == ContactProviderStatus.COMPLETED
    assert fake.calls == [("contact", "123456789")]
    directors = selected.discover_resource(target(), "directors")
    assert directors.status == ContactProviderStatus.NOT_FOUND
    assert fake.calls == [("contact", "123456789"), ("directors", "123456789")]
