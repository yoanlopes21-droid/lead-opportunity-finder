from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ObservedJobOffer
from app.services.opportunities.company import (
    aggregate_active_company_opportunities,
    normalize_company_key,
)
from app.services.scoring.company import EnrichmentSnapshot, score_company_opportunity


NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'opportunities-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session
    engine.dispose()


def test_aggregates_company_offers_with_metrics_and_descriptive_signals(session):
    _add(session, "1", company="  Acme, SAS ", title="Technicien", contract="CDI", commune="94028", age_days=10, source="source_a")
    _add(session, "2", company="acme sas", title="Commercial", contract="CDD", commune="94029", age_days=50, source="source_b", observations=2)
    _add(session, "3", company="ACME SAS", title="Technicien", contract="MIS", commune="94029", age_days=100)
    session.commit()

    result = aggregate_active_company_opportunities(session, now=NOW)
    opportunity = result.opportunities[0]

    assert result.active_offers_analyzed == 3
    assert opportunity.company_key == "acme sas"
    assert opportunity.company_name == "Acme, SAS"
    assert opportunity.active_offer_count == 3
    assert opportunity.distinct_job_title_count == 2
    assert opportunity.distinct_source_count == 2
    assert opportunity.cdi_offer_count == 1 and opportunity.cdd_offer_count == 1 and opportunity.other_contract_offer_count == 1
    assert (opportunity.offers_over_21_days, opportunity.offers_over_45_days, opportunity.offers_over_90_days) == (2, 2, 1)
    assert {signal.name for signal in opportunity.signals if signal.active} == {
        "hiring_volume_signal", "role_diversity_signal", "persistent_need_signal", "multi_location_signal", "recurrent_observation_signal"
    }
    assert len(opportunity.offer_ids) == 3


def test_similar_names_are_not_aggressively_merged_and_unattributed_are_separate(session):
    _add(session, "a", company="Acme SAS")
    _add(session, "b", company="Acme SARL")
    _add(session, "c", company=None)
    _add(session, "inactive", company="Acme SAS", active=False)
    _add(session, "other-department", company="Other", department="75")
    session.commit()

    result = aggregate_active_company_opportunities(session, now=NOW)

    assert [item.company_key for item in result.opportunities] == ["acme sarl", "acme sas"]
    assert result.active_offers_analyzed == 3
    assert result.unattributed_offer_count == 1
    assert result.unattributed_offer_ids == ("source_a:c",)


def test_missing_dates_and_multiple_sources_do_not_mutate_original_offers(session):
    offer = _add(session, "x", company="No Date", created_at=None, source="indeed", location_label="Vitry")
    _add(session, "y", company="No Date", created_at="invalid-date", source="hellowork", location_label="Vitry")
    session.commit()
    before = (offer.first_seen_at, offer.last_seen_at, offer.observation_count, offer.is_active)

    result = aggregate_active_company_opportunities(session, now=NOW)
    opportunity = result.opportunities[0]

    assert opportunity.oldest_offer_created_at is None
    assert opportunity.average_offer_age_days is None
    assert opportunity.distinct_source_count == 2
    assert (offer.first_seen_at, offer.last_seen_at, offer.observation_count, offer.is_active) == before


def test_aggregates_conservative_intermediary_description_evidence(session):
    _add(session, "1", company="Diffuseur", description="Cabinet de recrutement recrute pour son client.")
    _add(session, "2", company="Diffuseur", description="Relation client et suivi des dossiers.")
    session.commit()

    opportunity = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]

    evidence = opportunity.intermediary_description_evidence
    assert evidence.strong_signal_offer_count == 1
    assert evidence.strong_signal_proportion == 0.5
    assert evidence.marker_types == ("recruitment_firm",)
    assert evidence.examples[0].offer_id == "source_a:1"


def test_company_key_only_normalizes_trivial_spacing_case_and_punctuation():
    assert normalize_company_key(" ACME, SAS ") == normalize_company_key("acme sas")
    assert normalize_company_key("Acme SAS") != normalize_company_key("Acme SARL")
    assert normalize_company_key(None) is None


def test_local_opportunity_aggregates_offers_by_structured_commune_and_preserves_evidence(session):
    _add(session, "1", company="ACME", commune="94028", location_label="Créteil Centre", title="Technicien", source_url="https://example.test/1")
    _add(session, "2", company="ACME", commune="94028", location_label="Créteil Sud", title="Commercial", source_url="https://example.test/2")
    session.commit()

    local = aggregate_active_company_opportunities(session, now=NOW).opportunities[0].local_opportunities[0]

    assert local.local_key == "commune:94:94028"
    assert local.commune == "94028" and local.location_label == "Créteil Centre"
    assert local.active_offer_count == 2 and local.distinct_job_title_count == 2
    assert local.representative_job_titles == ("Commercial", "Technicien")
    assert local.source_offer_ids == ("source_a:1", "source_a:2")
    assert local.source_urls == ("https://example.test/1", "https://example.test/2")


def test_local_opportunities_split_communes_without_duplicate_offers_and_keep_global_score(session):
    _add(session, "1", company="ACME", commune="94028", title="Technicien")
    _add(session, "2", company="ACME", commune="94029", title="Commercial")
    session.commit()

    opportunity = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]
    locals_ = opportunity.local_opportunities

    assert [item.commune for item in locals_] == ["94028", "94029"]
    assert sum(item.active_offer_count for item in locals_) == opportunity.active_offer_count
    assert {offer_id for item in locals_ for offer_id in item.source_offer_ids} == set(opportunity.offer_ids)
    baseline = score_company_opportunity(
        opportunity.__class__(**{**opportunity.__dict__, "local_opportunities": ()}),
        EnrichmentSnapshot(),
    )
    assert score_company_opportunity(opportunity, EnrichmentSnapshot()).total_score == baseline.total_score


def test_local_opportunity_uses_normalized_location_label_when_commune_is_missing(session):
    _add(session, "1", company="ACME", commune=None, location_label="L'Haÿ-les-Roses")
    _add(session, "2", company="ACME", commune=None, location_label="l hay les roses")
    session.commit()

    local = aggregate_active_company_opportunities(session, now=NOW).opportunities[0].local_opportunities[0]

    assert local.local_key == "location:94:l hay les roses"
    assert local.commune is None and local.location_label in {"L'Haÿ-les-Roses", "l hay les roses"}
    assert local.active_offer_count == 2
    assert aggregate_active_company_opportunities(session, now=NOW).opportunities[0].local_opportunities[0] == local


def test_local_opportunity_uses_a_deterministic_unknown_location_bucket(session):
    _add(session, "1", company="ACME", commune=None, location_label=None)
    _add(session, "2", company="ACME", commune=None, location_label=None)
    session.commit()

    local = aggregate_active_company_opportunities(session, now=NOW).opportunities[0].local_opportunities[0]

    assert local.local_key == "unknown_location:94"
    assert local.commune is None and local.location_label is None
    assert local.active_offer_count == 2


def test_single_location_company_has_one_sorted_local_opportunity(session):
    _add(session, "1", company="ACME", commune="94028", title="Technicien", age_days=4)
    _add(session, "2", company="ACME", commune="94028", title="Technicien", age_days=1)
    session.commit()

    local_opportunities = aggregate_active_company_opportunities(session, now=NOW).opportunities[0].local_opportunities

    assert len(local_opportunities) == 1
    assert local_opportunities[0].newest_offer_created_at.endswith("Z")
    assert local_opportunities[0].oldest_offer_created_at.endswith("Z")


def _add(
    session,
    offer_id,
    company="Company",
    title="Title",
    contract="CDI",
    commune="94028",
    location_label="Créteil",
    age_days=1,
    created_at="default",
    source="source_a",
    department="94",
    active=True,
    observations=1,
    description=None,
    source_url=None,
):
    if created_at == "default":
        created_at = (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    offer = ObservedJobOffer(
        source=source,
        source_offer_id=offer_id,
        title=title,
        company_name=company,
        description=description,
        contract_type=contract,
        location_label=location_label,
        commune=commune,
        department_code=department,
        created_at=created_at,
        source_url=source_url,
        first_seen_at=NOW - timedelta(days=age_days),
        last_seen_at=NOW,
        last_changed_at=NOW,
        is_active=active,
        observation_count=observations,
    )
    session.add(offer)
    session.flush()
    return offer
