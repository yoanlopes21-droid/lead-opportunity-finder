from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CommercialExclusion, CompanyEnrichment, ObservedJobOffer
from app.services.commercial_leads.exclusions import (
    CommercialExclusionInput,
    ExclusionType,
    create_commercial_exclusion,
    parse_exclusion_rows,
)
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.company_enrichment.contracts import MatchStatus
from app.services.scoring.company import ScoreCategory


NOW = datetime(2026, 9, 17, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'commercial-leads-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_offer(session, identifier, company="ACME SAS", age_days=2, title="Technicien", source="source", url="https://source.test/offer", description=None):
    row = ObservedJobOffer(
        source=source, source_offer_id=identifier, title=title, company_name=company, description=description,
        location_label="Créteil", commune="94028", department_code="94",
        created_at=(NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z"),
        source_url=url, first_seen_at=NOW - timedelta(days=age_days), last_seen_at=NOW,
        last_changed_at=NOW, is_active=True, observation_count=2,
    )
    session.add(row)
    session.flush()
    return row


def add_enrichment(session, company_key="acme sas", name="ACME SAS", status=MatchStatus.HIGH_CONFIDENCE, siren="123456789", sector="private", employee_range="20-49"):
    if status != MatchStatus.HIGH_CONFIDENCE:
        siren = None
    row = CompanyEnrichment(
        company_key=company_key, source_company_name=name, provider="dinum",
        match_status=status, entity_sector_type=sector, siren=siren,
        siret="12345678900010" if siren else None,
        official_name=name if siren else None, commune="Créteil" if siren else None,
        employee_range=employee_range if siren else None, last_attempt_at=NOW,
        attempt_count=1, input_fingerprint=f"fingerprint-{company_key}",
    )
    session.add(row)
    session.flush()
    return row


def add_exclusion(session, company_key="acme sas", exclusion_type=ExclusionType.MANUAL_EXCLUSION, siren=None, starts_at=None, expires_at=None):
    row = CommercialExclusion(
        company_key=company_key, siren=siren, company_name_snapshot="ACME SAS",
        exclusion_type=exclusion_type, starts_at=starts_at or NOW - timedelta(days=1),
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return row


def test_current_client_is_excluded_and_visible_for_audit(session):
    add_offer(session, "1")
    add_enrichment(session)
    add_exclusion(session, exclusion_type=ExclusionType.CURRENT_CLIENT, siren="123456789")
    assert list_commercial_leads(session, now=NOW).items == ()
    audit = list_commercial_leads(session, CommercialLeadQuery(include_excluded=True), now=NOW).items[0]
    assert not audit.is_eligible and audit.exclusion.exclusion_type == ExclusionType.CURRENT_CLIENT


def test_recent_prospect_is_excluded_until_expiry_then_eligible(session):
    add_offer(session, "1")
    add_enrichment(session)
    add_exclusion(session, exclusion_type=ExclusionType.RECENT_PROSPECT, siren="123456789", expires_at=NOW + timedelta(days=1))
    assert not list_commercial_leads(session, CommercialLeadQuery(include_excluded=True), now=NOW).items[0].is_eligible
    assert list_commercial_leads(session, now=NOW + timedelta(days=2)).items[0].is_eligible


def test_manual_exclusion_without_expiry_remains_active(session):
    add_offer(session, "1")
    add_enrichment(session)
    add_exclusion(session, siren="123456789")
    assert not list_commercial_leads(session, CommercialLeadQuery(include_excluded=True), now=NOW + timedelta(days=365)).items[0].is_eligible


def test_siren_match_is_prioritized_over_company_key(session):
    add_offer(session, "1")
    add_enrichment(session)
    add_exclusion(session, siren="999999999")
    assert list_commercial_leads(session, now=NOW).items[0].is_eligible
    add_exclusion(session, company_key="other", siren="123456789")
    assert list_commercial_leads(session, now=NOW).items == ()


def test_name_only_exclusion_still_applies_after_siren_is_confirmed(session):
    add_offer(session, "1")
    add_enrichment(session)
    add_exclusion(session, company_key="acme sas", siren=None)
    assert list_commercial_leads(session, now=NOW).items == ()


def test_exact_company_key_fallback_never_fuzzy_matches(session):
    add_offer(session, "1")
    add_enrichment(session, status=MatchStatus.NOT_FOUND)
    add_exclusion(session, company_key="acme", exclusion_type=ExclusionType.MANUAL_EXCLUSION)
    assert list_commercial_leads(session, now=NOW).items[0].is_eligible
    add_exclusion(session, company_key="acme sas", exclusion_type=ExclusionType.MANUAL_EXCLUSION)
    assert list_commercial_leads(session, now=NOW).items == ()


def test_non_excluded_lead_keeps_scoring_identity_and_evidence(session):
    add_offer(session, "1", url="https://source.test/one")
    add_offer(session, "2", title="Commercial", source="other", url="https://source.test/two")
    add_enrichment(session)
    lead = list_commercial_leads(session, now=NOW).items[0]
    assert lead.is_eligible and lead.scoring.total_score > 0
    assert lead.siren == "123456789" and lead.official_name == "ACME SAS"
    assert len(lead.evidence) == 2 and lead.recommended_channel is None


def test_sorting_is_score_descending_and_deterministic(session):
    add_offer(session, "a", company="BETA", age_days=2)
    add_offer(session, "b", company="ALPHA", age_days=2)
    add_offer(session, "c", company="HIGH", age_days=2, title="One")
    add_offer(session, "d", company="HIGH", age_days=2, title="Two")
    page = list_commercial_leads(session, now=NOW)
    assert page.items[0].company_name == "HIGH"
    tied = [item.company_name for item in page.items if item.scoring.total_score == page.items[-1].scoring.total_score]
    assert tied == sorted(tied, key=str.casefold)


def test_category_and_minimum_score_filters(session):
    add_offer(session, "1")
    add_enrichment(session)
    lead = list_commercial_leads(session, now=NOW).items[0]
    by_category = list_commercial_leads(session, CommercialLeadQuery(categories=frozenset({lead.scoring.category})), now=NOW)
    assert [item.company_key for item in by_category.items] == [lead.company_key]
    assert list_commercial_leads(session, CommercialLeadQuery(minimum_score=lead.scoring.total_score + 1), now=NOW).items == ()


def test_missing_enrichment_does_not_remove_lead(session):
    add_offer(session, "1")
    lead = list_commercial_leads(session, now=NOW).items[0]
    assert lead.official_name is None and lead.employee_range is None and lead.is_eligible


def test_not_found_enrichment_remains_a_supported_lead(session):
    add_offer(session, "1")
    add_enrichment(session, status=MatchStatus.NOT_FOUND)
    lead = list_commercial_leads(session, now=NOW).items[0]
    assert lead.entity_sector_type == "private" and lead.siren is None and lead.is_eligible


def test_suspected_intermediary_remains_eligible_with_a_moderate_penalty(session):
    add_offer(session, "1", description="Nous recrutons pour notre client.")
    lead = list_commercial_leads(session, now=NOW).items[0]
    assert lead.is_eligible
    assert lead.scoring.employer_relationship_status == "intermediary_suspected"
    assert any(item.code == "intermediary_suspected_penalty" for item in lead.scoring.penalties)


def test_building_leads_does_not_mutate_source_rows(session):
    offer = add_offer(session, "1")
    enrichment = add_enrichment(session)
    before = (offer.last_seen_at, offer.observation_count, enrichment.attempt_count, enrichment.match_status)
    list_commercial_leads(session, now=NOW)
    assert before == (offer.last_seen_at, offer.observation_count, enrichment.attempt_count, enrichment.match_status)


def test_sector_filter_and_pagination(session):
    add_offer(session, "1", company="PRIVATE")
    add_enrichment(session, company_key="private", name="PRIVATE")
    add_offer(session, "2", company="PUBLIC")
    add_enrichment(session, company_key="public", name="PUBLIC", sector="public")
    filtered = list_commercial_leads(session, CommercialLeadQuery(entity_sector_types=frozenset({"public"}), limit=1), now=NOW)
    assert filtered.total_count == 1 and len(filtered.items) == 1 and filtered.items[0].entity_sector_type == "public"


def test_future_import_contract_and_creation_are_exact_and_provider_neutral(session):
    parsed = parse_exclusion_rows(({
        "company_name": " ACME, SAS ", "exclusion_type": "recent_prospect",
        "expires_at": "2026-10-01T00:00:00Z", "reason": "Manual CRM export",
    },), starts_at=NOW)
    row = create_commercial_exclusion(session, parsed[0])
    assert row.company_key == "acme sas" and row.siren is None
    with pytest.raises(ValueError):
        create_commercial_exclusion(session, CommercialExclusionInput(
            company_key="different", company_name_snapshot="ACME SAS",
            exclusion_type=ExclusionType.MANUAL_EXCLUSION,
        ))
