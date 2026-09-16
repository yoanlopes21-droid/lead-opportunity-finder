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


def test_company_key_only_normalizes_trivial_spacing_case_and_punctuation():
    assert normalize_company_key(" ACME, SAS ") == normalize_company_key("acme sas")
    assert normalize_company_key("Acme SAS") != normalize_company_key("Acme SARL")
    assert normalize_company_key(None) is None


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
):
    if created_at == "default":
        created_at = (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    offer = ObservedJobOffer(
        source=source,
        source_offer_id=offer_id,
        title=title,
        company_name=company,
        contract_type=contract,
        location_label=location_label,
        commune=commune,
        department_code=department,
        created_at=created_at,
        first_seen_at=NOW - timedelta(days=age_days),
        last_seen_at=NOW,
        last_changed_at=NOW,
        is_active=active,
        observation_count=observations,
    )
    session.add(offer)
    session.flush()
    return offer
