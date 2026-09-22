import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ObservedJobOffer, RecruitmentSignal
from app.services.collection.greenhouse import GreenhouseBoard, GreenhouseJobBoardProvider
from app.services.collection.open_web import (
    CachedSearchClient,
    DiscoveryQueryPlan,
    OpenWebJobDiscoveryProvider,
    source_from_url,
)
from app.services.collection.providers import JobOfferProviderCollector, ProviderPage
from app.services.opportunities.company import aggregate_active_company_opportunities
from app.services.persistence.offers import OfferSnapshot, create_collection_run, upsert_offer


NOW = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'multi-source.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_greenhouse_public_board_normalizes_only_explicit_94_offers_without_salary():
    payload = json.loads((FIXTURES / "greenhouse_board.json").read_text())
    calls = []
    provider = GreenhouseJobBoardProvider(
        GreenhouseBoard("example", "Example SAS", discovery_provider="brave_search"),
        requester=lambda *args, **kwargs: calls.append((args, kwargs)) or Response(payload),
        sleeper=lambda _: None,
    )

    page = tuple(provider.iter_pages())[0]

    assert len(calls) == 1 and page.is_last is True and page.skipped_count == 1
    assert [offer.source_offer_id for offer in page.offers] == [
        "greenhouse:example:101", "greenhouse:example:103"
    ]
    assert all(offer.source == "employer_career_site" for offer in page.offers)
    assert all(offer.department_code == "94" for offer in page.offers)
    assert page.offers[0].commune == "creteil"
    assert page.offers[0].salary is None and page.offers[0].created_at is None
    assert page.offers[0].discovery_provider == "brave_search"
    assert page.offers[0].origin == "greenhouse:example"


class StaticProvider:
    provider_id = "employer_career_site"
    scope_type = "provider_board"
    can_deactivate_unseen = True

    def __init__(self, board, offers):
        self.scope_value = f"greenhouse:{board}"
        self.offers = offers

    def iter_pages(self):
        yield ProviderPage(1, offers=self.offers, is_last=True)


def _snapshot(identifier, *, board="a", title="Technicien", source="employer_career_site"):
    return OfferSnapshot(
        source=source, source_offer_id=identifier, title=title,
        company_name="ACME SAS", location_label="Créteil", commune="94028",
        department_code="94", created_at="2026-09-20T08:00:00Z",
        source_url=f"https://jobs.example/{identifier}", origin=f"greenhouse:{board}",
    )


def test_generic_provider_deactivation_is_complete_and_board_scoped(session):
    JobOfferProviderCollector(StaticProvider("a", (_snapshot("a-1"), _snapshot("a-2")))).collect(session)
    JobOfferProviderCollector(StaticProvider("b", (_snapshot("b-1", board="b"),))).collect(session)

    result = JobOfferProviderCollector(StaticProvider("a", (_snapshot("a-1"),))).collect(session)
    rows = {row.source_offer_id: row for row in session.scalars(select(ObservedJobOffer))}

    assert result.offers_deactivated == 1
    assert rows["a-1"].is_active is True and rows["a-2"].is_active is False
    assert rows["b-1"].is_active is True


class FailingProvider(StaticProvider):
    def iter_pages(self):
        yield ProviderPage(1, offers=(_snapshot("a-1"),), is_last=False)
        raise RuntimeError("simulated provider failure")


def test_failed_provider_refresh_never_deactivates_unseen_board_offers(session):
    JobOfferProviderCollector(StaticProvider("a", (_snapshot("a-1"), _snapshot("a-2")))).collect(session)

    with pytest.raises(RuntimeError, match="simulated"):
        JobOfferProviderCollector(FailingProvider("a", ())).collect(session)

    assert all(row.is_active for row in session.scalars(select(ObservedJobOffer)))


class Result:
    def __init__(self, url, title, description):
        self.url, self.title, self.description = url, title, description


class FakeSearchClient:
    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return (
            Result("https://www.linkedin.com/jobs/view/123", "ACME recrute", "Poste à Créteil"),
            Result("https://www.linkedin.com/jobs/view/123", "duplicate", "duplicate"),
            Result("https://jobs.acme.test/opening", "Nous recrutons", "Val-de-Marne"),
        )


def test_open_web_discovery_keeps_incomplete_results_as_signals_without_scraping(session):
    client = FakeSearchClient()
    provider = OpenWebJobDiscoveryProvider(
        client, DiscoveryQueryPlan(communes=("Créteil",), intents=('"recrute"',), site_domains=(), max_queries=1)
    )

    JobOfferProviderCollector(provider).collect(session)

    signals = list(session.scalars(select(RecruitmentSignal).order_by(RecruitmentSignal.id)))
    assert len(client.calls) == 1 and len(signals) == 2
    assert signals[0].source == "linkedin"
    assert signals[0].discovery_provider == "brave_search"
    assert signals[1].source == "employer_career_site"
    assert session.query(ObservedJobOffer).count() == 0


def test_open_web_search_cache_avoids_a_second_brave_client_call(session):
    delegate = FakeSearchClient()
    cached = CachedSearchClient(session, delegate, now=lambda: NOW)

    first = cached.search('"offre emploi" Créteil', request_index=1)
    second = cached.search('"offre emploi" Créteil', request_index=2)

    assert first == second
    assert len(delegate.calls) == 1


def test_source_classification_does_not_call_linkedin_or_indeed():
    assert source_from_url("https://www.linkedin.com/jobs/view/1") == "linkedin"
    assert source_from_url("https://fr.indeed.com/viewjob?jk=1") == "indeed"
    assert source_from_url("https://company.test/carrieres/1") == "employer_career_site"


def test_discovery_provider_is_distinct_from_offer_source(session):
    run = create_collection_run(session, "brave_search", "department", "94", now=NOW)
    result = upsert_offer(session, run, _snapshot(
        "linkedin-1", source="linkedin"
    ).__class__(**{
        **_snapshot("linkedin-1", source="linkedin").__dict__,
        "discovery_provider": "brave_search",
        "origin": None,
    }), now=NOW)

    assert result.offer.source == "linkedin"
    assert result.offer.discovery_provider == "brave_search"


def _observation(
    session, identifier, *, source, title="Auxiliaire de vie H/F", company="Société X",
    created_at="2026-09-15T08:00:00Z", location="Créteil", contract="CDI",
):
    offer = ObservedJobOffer(
        source=source, source_offer_id=identifier, title=title, company_name=company,
        location_label=location, commune=None, department_code="94", contract_type=contract,
        created_at=created_at, source_url=f"https://{source}.example/{identifier}",
        first_seen_at=NOW, last_seen_at=NOW, last_changed_at=NOW,
        is_active=True, observation_count=1,
    )
    session.add(offer)
    session.flush()
    return offer


def test_cross_source_same_need_is_one_canonical_offer_with_all_evidence(session):
    _observation(session, "ft-1", source="france_travail")
    _observation(session, "hw-1", source="hellowork", title="Auxiliaire de vie")
    session.commit()

    opportunity = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]

    assert opportunity.active_offer_count == 1
    assert opportunity.distinct_source_count == 2
    assert opportunity.offer_ids == ("france_travail:ft-1", "hellowork:hw-1")
    assert opportunity.active_job_offers[0].sources == ("france_travail", "hellowork")
    assert len(opportunity.active_job_offers[0].evidence) == 2


def test_cross_source_dedupe_keeps_different_roles_and_reposts_separate(session):
    _observation(session, "ft-1", source="france_travail", title="Comptable")
    _observation(session, "hw-1", source="hellowork", title="Technicien maintenance")
    _observation(session, "wttj-1", source="welcome_to_the_jungle", title="Comptable", created_at="2026-07-01T08:00:00Z")
    session.commit()

    opportunity = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]

    assert opportunity.active_offer_count == 3
    assert opportunity.distinct_job_title_count == 2


def test_cross_source_dedupe_never_merges_similarly_named_companies(session):
    _observation(session, "one", source="france_travail", company="ACME SAS")
    _observation(session, "two", source="hellowork", company="ACME SARL")
    session.commit()

    result = aggregate_active_company_opportunities(session, now=NOW)

    assert len(result.opportunities) == 2
    assert all(item.active_offer_count == 1 for item in result.opportunities)
