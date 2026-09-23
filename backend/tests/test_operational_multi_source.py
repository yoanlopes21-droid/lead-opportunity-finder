from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import CollectionRun, JobSourceBoard, ObservedJobOffer, RecruitmentSignal
from app.services.collection.lever import LeverBoard, LeverJobBoardProvider
from app.services.collection.open_web import (
    BRAVE_DISCOVERY_PROVIDER,
    DiscoveryQueryPlan,
    OpenWebJobDiscoveryProvider,
    RecruitmentPageType,
    classify_page_type,
    extract_offer_geography,
    normalize_index_text,
    reconcile_recruitment_signal_quality,
    source_from_url,
    _signal_from_result,
)
from app.services.collection.providers import ProviderPage
from app.services.job_source_boards import (
    BoardValidationError,
    DuplicateBoardError,
    create_board,
    create_board_refresh_run,
    normalize_board_identifier,
    run_board_refresh,
    update_board,
)
from app.services.open_web_runs import OpenWebRunError, create_open_web_run, run_open_web_discovery
from app.services.opportunities.company import aggregate_active_company_opportunities
from app.services.persistence.offers import (
    OfferSnapshot,
    complete_collection_run,
    create_collection_run,
    ensure_collection_run_schema,
    upsert_recruitment_signal,
    RecruitmentSignalSnapshot,
)
from app.services.recruitment_signals import (
    SignalPromotionError,
    assess_signal_promotion,
    dismiss_signal,
    promote_signal,
)
from app.api.recruitment_signals import _response as signal_response


NOW = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'operational.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def test_board_config_crud_validation_duplicate_and_enabled_state(session):
    assert normalize_board_identifier("greenhouse", "https://boards.greenhouse.io/Acme/jobs/1") == "acme"
    assert normalize_board_identifier("lever", "https://jobs.lever.co/Acme/abc") == "acme"
    with pytest.raises(BoardValidationError):
        normalize_board_identifier("greenhouse", "https://example.com/acme")

    board = create_board(
        session, provider_id="greenhouse", display_name="ACME Carrières",
        board_identifier="https://boards.greenhouse.io/acme", company_name_hint="ACME",
    )
    assert board.enabled is True and board.board_identifier == "acme"
    with pytest.raises(DuplicateBoardError):
        create_board(
            session, provider_id="greenhouse", display_name="Duplicate",
            board_identifier="acme", company_name_hint="ACME",
        )
    update_board(session, board, enabled=False, display_name="ACME Jobs")
    assert board.enabled is False and board.display_name == "ACME Jobs"
    with pytest.raises(BoardValidationError, match="Activez"):
        create_board_refresh_run(session, board)


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_lever_public_provider_normalizes_only_strict_94_offers():
    payload = [
        {
            "id": "one", "text": "Technicien maintenance", "hostedUrl": "https://jobs.lever.co/acme/one",
            "descriptionPlain": "Maintenance", "createdAt": 1_795_000_000_000,
            "categories": {"location": "Créteil, 94000", "commitment": "CDI"},
        },
        {
            "id": "two", "text": "Comptable", "hostedUrl": "https://jobs.lever.co/acme/two",
            "categories": {"location": "Paris et région", "commitment": "CDI"},
        },
    ]
    provider = LeverJobBoardProvider(
        LeverBoard("acme", "ACME", discovery_provider="lever"),
        requester=lambda *args, **kwargs: Response(payload), sleeper=lambda _: None,
    )
    page = tuple(provider.iter_pages())[0]
    assert [offer.source_offer_id for offer in page.offers] == ["lever:acme:one"]
    assert page.offers[0].department_code == "94"
    assert page.offers[0].commune == "creteil"
    assert page.offers[0].origin == "lever:acme"
    assert page.skipped_count == 1


class StaticBoardProvider:
    provider_id = "employer_career_site"
    scope_type = "provider_board"
    can_deactivate_unseen = True

    def __init__(self, scope, offers, *, fail=False):
        self.scope_value = scope
        self.offers = offers
        self.fail = fail

    def iter_pages(self):
        if self.fail:
            raise RuntimeError("board unavailable")
        yield ProviderPage(1, offers=self.offers, is_last=True)


def _board_offer(board, identifier):
    return OfferSnapshot(
        source="employer_career_site", source_offer_id=identifier, title="Technicien",
        company_name="ACME", location_label="Créteil", commune="creteil", department_code="94",
        source_url=f"https://jobs.example/{identifier}", origin=board,
    )


def test_board_refresh_is_scoped_and_failure_keeps_old_offers(session, monkeypatch):
    a = create_board(session, provider_id="greenhouse", display_name="A", board_identifier="a", company_name_hint="A")
    b = create_board(session, provider_id="greenhouse", display_name="B", board_identifier="b", company_name_hint="B")
    providers = {
        a.id: StaticBoardProvider("greenhouse:a", (_board_offer("greenhouse:a", "a-1"), _board_offer("greenhouse:a", "a-2"))),
        b.id: StaticBoardProvider("greenhouse:b", (_board_offer("greenhouse:b", "b-1"),)),
    }
    monkeypatch.setattr("app.services.job_source_boards.build_board_provider", lambda board: providers[board.id])
    for board in (a, b):
        run, _ = create_board_refresh_run(session, board)
        run_board_refresh(session, board.id, run.id)

    providers[a.id] = StaticBoardProvider("greenhouse:a", (_board_offer("greenhouse:a", "a-1"),))
    run, _ = create_board_refresh_run(session, a)
    run_board_refresh(session, a.id, run.id)
    rows = {item.source_offer_id: item for item in session.scalars(select(ObservedJobOffer))}
    assert rows["a-2"].is_active is False and rows["b-1"].is_active is True

    providers[a.id] = StaticBoardProvider("greenhouse:a", (), fail=True)
    run, _ = create_board_refresh_run(session, a)
    run_board_refresh(session, a.id, run.id)
    assert session.get(ObservedJobOffer, rows["a-1"].id).is_active is True
    assert session.get(JobSourceBoard, a.id).last_refresh_status == "failed"


def test_empty_first_board_refresh_completes_but_empty_refresh_cannot_deactivate_stock(session):
    first = create_collection_run(session, "employer_career_site", "provider_board", "lever:empty")
    complete_collection_run(
        session, first, full_scope_completed=True, deactivate_unseen=True,
    )
    assert first.status == "completed" and first.offers_deactivated == 0

    session.add(ObservedJobOffer(
        source="employer_career_site", source_offer_id="protected", title="Existing",
        company_name="ACME", department_code="94", origin="lever:empty",
        first_seen_at=NOW, last_seen_at=NOW, last_changed_at=NOW,
        is_active=True, observation_count=1,
    ))
    session.commit()
    second = create_collection_run(session, "employer_career_site", "provider_board", "lever:empty")
    with pytest.raises(ValueError, match="existing offers"):
        complete_collection_run(
            session, second, full_scope_completed=True, deactivate_unseen=True,
        )


class Result:
    def __init__(self, url, title, description):
        self.url, self.title, self.description = url, title, description


class CountingClient:
    def __init__(self):
        self.calls = 0

    def search(self, query, **kwargs):
        self.calls += 1
        return (
            Result(f"https://www.linkedin.com/jobs/view/{self.calls}", f"ACME recrute Technicien à Créteil", "94000 Créteil"),
            Result(f"https://fr.indeed.com/viewjob?jk={self.calls}", "Offre", "France"),
        )


def test_open_web_stop_early_and_source_classification():
    client = CountingClient()
    provider = OpenWebJobDiscoveryProvider(
        client, DiscoveryQueryPlan(max_queries=8), max_signals=3,
    )
    pages = tuple(provider.iter_pages())
    assert client.calls == 2 and pages[-1].is_last is True
    assert sum(len(page.signals) for page in pages) == 3
    assert source_from_url("https://www.hellowork.com/fr-fr/emplois/1.html") == "hellowork"
    assert source_from_url("https://www.leboncoin.fr/ad/offres_d_emploi/1") == "leboncoin"
    assert source_from_url("https://uk.indeed.co.uk/viewjob?jk=1") == "indeed"
    assert source_from_url("https://fr.jooble.org/emploi") == "jooble"
    assert source_from_url("https://www.glassdoor.fr/Emploi/index.htm") == "glassdoor"
    assert source_from_url("https://careers.acme.test/jobs/1") == "employer_career_site"
    structured = _signal_from_result(Result(
        "https://fr.linkedin.com/jobs/view/1",
        "ACME recrute pour des postes de Technicien H/F à Créteil",
        "Offre située dans le Val-de-Marne",
    ))
    assert structured.company_name == "ACME"
    assert structured.job_title == "Technicien H/F"
    assert structured.department_code == "94"


def test_generic_job_search_pages_and_ats_roots_are_not_individual_offers():
    cases = {
        "https://fr.indeed.com/jobs?q=technicien&l=Creteil": RecruitmentPageType.SEARCH_OR_LISTING,
        "https://www.cadremploi.fr/emploi/liste_offres?ville=vitry-sur-seine-94": RecruitmentPageType.SEARCH_OR_LISTING,
        "https://fr.jooble.org/emploi-mairie-de-vitry-sur+seine": RecruitmentPageType.SEARCH_OR_LISTING,
        "https://jobs.lever.co/ajax": RecruitmentPageType.CAREER_BOARD,
        "https://boards.greenhouse.io/acme": RecruitmentPageType.CAREER_BOARD,
    }
    for url, expected in cases.items():
        assert classify_page_type(url, title="Plus de 600 emplois") == expected

    indeed = _signal_from_result(Result(
        "https://fr.indeed.com/jobs?q=emploi&l=Creteil",
        "Plus de 600 emplois à Créteil", "Offres dans le Val-de-Marne",
    ))
    assert indeed.page_type == RecruitmentPageType.SEARCH_OR_LISTING
    assert indeed.department_code is None
    assert indeed.confidence == 0.25


def test_individual_offer_needs_offer_specific_94_geography():
    url = "https://www.linkedin.com/jobs/view/123"
    assert classify_page_type(url) == RecruitmentPageType.INDIVIDUAL_JOB_OFFER

    outside = _signal_from_result(Result(
        url, "K2GROUP recrute Commercial itinérant H/F à Fontenay-sous-Bois",
        "Poste basé à Frépillon (95)",
    ))
    assert outside.page_type == RecruitmentPageType.INDIVIDUAL_JOB_OFFER
    assert outside.department_code is None
    assert outside.location_label is None

    unknown = _signal_from_result(Result(
        url, "ACME recrute Technicien H/F - CDI", "Rejoignez notre équipe.",
    ))
    assert unknown.department_code is None

    local = _signal_from_result(Result(
        url, "ACME recrute Technicien H/F à Créteil", "Poste basé à Créteil 94000.",
    ))
    assert local.department_code == "94"
    assert local.commune == "creteil"


def test_94_query_never_supplies_missing_result_geography():
    class QueryAwareClient:
        query = None

        def search(self, query, **kwargs):
            self.query = query
            return (Result(
                "https://www.linkedin.com/jobs/view/456",
                "ACME recrute Technicien H/F - CDI", "Localisation non publiée",
            ),)

    client = QueryAwareClient()
    provider = OpenWebJobDiscoveryProvider(
        client,
        DiscoveryQueryPlan(
            communes=("Fontenay-sous-Bois",), intents=('"emploi"',),
            site_domains=(), ats_domains=(), max_queries=1,
        ),
    )
    signal = tuple(provider.iter_pages())[0].signals[0]
    assert "Fontenay-sous-Bois" in client.query
    assert signal.department_code is None


def test_promotability_reports_missing_fields_and_non_individual_page(session):
    listing = _signal(
        session,
        source="indeed",
        source_url="https://fr.indeed.com/jobs?q=technicien&l=creteil",
        title="Plus de 600 emplois à Créteil",
        snippet="Liste d'offres dans le Val-de-Marne",
        company_name=None,
        job_title=None,
        location_label="Créteil",
        commune="creteil",
        department_code="94",
    )
    assessment = assess_signal_promotion(listing)
    assert assessment.is_promotable is False
    assert "Offre individuelle non identifiée" in assessment.blockers
    assert "Entreprise manquante" in assessment.blockers
    assert "Intitulé manquant" in assessment.blockers
    assert "Localisation 94 non prouvée" in assessment.blockers

    response = signal_response(listing)
    assert response.is_promotable is False
    assert response.page_type_label == "Page de résultats"
    assert response.promotion_blockers == list(assessment.blockers)


def test_backend_refuses_non_individual_page_even_with_forged_structured_fields(session):
    signal = _signal(
        session,
        source="cadremploi",
        source_url="https://www.cadremploi.fr/emploi/liste_offres?ville=vitry-sur-seine-94",
        title="ACME recrute Technicien H/F à Vitry-sur-Seine",
        snippet="Poste à Vitry-sur-Seine 94400",
        company_name="ACME", job_title="Technicien H/F",
        location_label="Vitry-sur-Seine", commune="vitry sur seine", department_code="94",
    )
    with pytest.raises(SignalPromotionError, match="Offre individuelle non identifiée"):
        promote_signal(session, signal)
    assert session.query(ObservedJobOffer).count() == 0


def test_html_entities_are_decoded_exactly_once():
    assert normalize_index_text("Recherche d&#x27;un technicien &amp; support") == "Recherche d'un technicien & support"
    assert normalize_index_text("Texte &amp;#39; littéral") == "Texte &#39; littéral"
    snapshot = _signal_from_result(Result(
        "https://www.linkedin.com/jobs/view/789",
        "ACME recrute Technicien H/F à Créteil",
        "Recherche d&#x27;un profil à Créteil 94000",
    ))
    assert snapshot.snippet == "Recherche d'un profil à Créteil 94000"


def test_existing_signal_quality_reconciliation_preserves_source_evidence(session):
    signal = _signal(
        session,
        source="cadremploi",
        source_url="https://www.cadremploi.fr/emploi/liste_offres?ville=vitry-sur-seine-94",
        title="Plus de 1 000 offres à Vitry-sur-Seine",
        snippet="Recherche d&#x27;un emploi dans le 94",
        company_name="Incorrect derived company", job_title="Incorrect derived job",
        location_label="Vitry-sur-Seine", commune="vitry sur seine", department_code="94",
        confidence=0.85,
    )
    original_title, original_snippet = signal.title, signal.snippet
    assert reconcile_recruitment_signal_quality(session) == 1
    session.refresh(signal)
    assert signal.title == original_title and signal.snippet == original_snippet
    assert signal.page_type == RecruitmentPageType.SEARCH_OR_LISTING
    assert signal.company_name is None and signal.job_title is None
    assert signal.department_code is None and signal.confidence == 0.25


class LedgerAwareFakeBrave:
    calls = 0

    def __init__(self, *, usage_service, run_hard_cap, **kwargs):
        self.usage = usage_service
        self.cap = run_hard_cap
        self.run_id = None

    def set_run_id(self, run_id):
        self.run_id = run_id

    def search(self, query, *, count, company_key=None, request_index=None):
        event = self.usage.reserve_request(
            run_id=self.run_id, company_key=company_key, query=query,
            request_index=request_index, run_hard_cap=self.cap,
        )
        self.usage.record_outcome(event.id, "completed")
        type(self).calls += 1
        return (Result(
            f"https://jobs.example/{type(self).calls}",
            f"ACME recrute Technicien {type(self).calls} à Créteil",
            "Offre à Créteil 94000",
        ),)


def test_open_web_run_uses_shared_ledger_hard_cap_and_cache(session, monkeypatch):
    LedgerAwareFakeBrave.calls = 0
    monkeypatch.setattr("app.services.open_web_runs.BraveSearchClient", LedgerAwareFakeBrave)
    settings = Settings(
        brave_search_api_key=SecretStr("test"), brave_search_monthly_request_budget=20,
        brave_search_monthly_reserve=2,
    )
    run, created = create_open_web_run(
        session, settings, target_signal_count=20, brave_max_requests=2,
    )
    assert created is True and run.brave_hard_cap == 2
    run_open_web_discovery(session, run.id, settings)
    session.refresh(run)
    assert run.status == "completed" and run.brave_requests_used == 2
    assert run.signals_found == 2 and LedgerAwareFakeBrave.calls == 2

    second, _ = create_open_web_run(session, settings, target_signal_count=20, brave_max_requests=2)
    run_open_web_discovery(session, second.id, settings)
    session.refresh(second)
    assert second.brave_requests_used == 0
    assert LedgerAwareFakeBrave.calls == 2


def test_open_web_budget_reserve_blocks_a_run(session):
    settings = Settings(
        brave_search_api_key=SecretStr("test"), brave_search_monthly_request_budget=5,
        brave_search_monthly_reserve=5,
    )
    with pytest.raises(OpenWebRunError, match="réserve"):
        create_open_web_run(session, settings, target_signal_count=5, brave_max_requests=1)


def _signal(session, **overrides):
    values = {
        "discovery_provider": BRAVE_DISCOVERY_PROVIDER,
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "domain": "www.linkedin.com",
        "title": "ACME recrute Technicien à Créteil",
        "snippet": "Poste basé à Créteil 94000",
        "company_name": "ACME",
        "job_title": "Technicien",
        "location_label": "Créteil 94000",
        "commune": "creteil",
        "department_code": "94",
        "confidence": 0.9,
        "detection_reason": "test",
        "extraction": {"rule": "test"},
        "status": "new",
    }
    values.update(overrides)
    signal = RecruitmentSignal(
        **values, first_seen_at=NOW, last_seen_at=NOW, observation_count=1,
    )
    session.add(signal)
    session.commit()
    return signal


def test_signal_persistence_promotion_invalid_geography_dismiss_and_cross_source_dedupe(session):
    existing = ObservedJobOffer(
        source="france_travail", source_offer_id="ft-1", title="Technicien", company_name="ACME",
        location_label="Créteil", commune="creteil", department_code="94",
        created_at="2026-09-23T08:00:00Z", first_seen_at=NOW, last_seen_at=NOW,
        last_changed_at=NOW, is_active=True, observation_count=1,
    )
    session.add(existing)
    session.commit()
    valid = _signal(session, published_at="2026-09-23T08:00:00Z")
    assert assess_signal_promotion(valid).is_promotable is True
    offer = promote_signal(session, valid)
    assert offer.discovery_provider == BRAVE_DISCOVERY_PROVIDER
    assert offer.recruitment_signal_id == valid.id
    assert valid.status == "promoted"
    opportunity = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]
    assert opportunity.active_offer_count == 1
    assert set(opportunity.active_job_offers[0].sources) == {"france_travail", "linkedin"}

    invalid = _signal(
        session, source_url="https://fr.indeed.com/viewjob?jk=bad",
        location_label="Île-de-France", commune=None, department_code=None,
    )
    with pytest.raises(SignalPromotionError, match="Localisation 94 non prouvée"):
        promote_signal(session, invalid)
    assert invalid.status == "review_needed"
    dismiss_signal(session, invalid)
    assert invalid.status == "dismissed"


def test_signal_upsert_preserves_dismissed_status(session):
    run = create_collection_run(session, BRAVE_DISCOVERY_PROVIDER, "department", "94")
    snapshot = RecruitmentSignalSnapshot(
        discovery_provider=BRAVE_DISCOVERY_PROVIDER, source="hellowork",
        source_url="https://www.hellowork.com/fr-fr/emplois/1.html", status="new",
    )
    signal = upsert_recruitment_signal(session, run, snapshot)
    signal.status = "dismissed"
    session.commit()
    run2 = create_collection_run(session, BRAVE_DISCOVERY_PROVIDER, "department", "94")
    same = upsert_recruitment_signal(session, run2, snapshot)
    assert same.status == "dismissed"


def test_existing_sqlite_schema_gets_operational_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE collection_runs RENAME TO collection_runs_current")
        connection.exec_driver_sql("ALTER TABLE recruitment_signals RENAME TO recruitment_signals_current")
        connection.exec_driver_sql(
            "CREATE TABLE collection_runs (id INTEGER PRIMARY KEY, source VARCHAR(120), scope_type VARCHAR(50), "
            "scope_value VARCHAR(120), status VARCHAR(20), is_full_scope BOOLEAN, started_at DATETIME, "
            "finished_at DATETIME, offers_received INTEGER, offers_new INTEGER, offers_updated INTEGER, "
            "offers_unchanged INTEGER, offers_skipped INTEGER, offers_deactivated INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE recruitment_signals (id INTEGER PRIMARY KEY, discovery_provider VARCHAR(120), "
            "source VARCHAR(120), source_url VARCHAR(2048), title VARCHAR(500), snippet TEXT, "
            "company_name VARCHAR(500), location_label VARCHAR(500), department_code VARCHAR(10), "
            "first_seen_at DATETIME, last_seen_at DATETIME, observation_count INTEGER, last_seen_run_id INTEGER)"
        )
    ensure_collection_run_schema(engine)
    columns = {item["name"] for item in inspect(engine).get_columns("collection_runs")}
    assert {"signals_found", "brave_requests_used", "stop_requested", "completion_reason"} <= columns
    signal_columns = {item["name"] for item in inspect(engine).get_columns("recruitment_signals")}
    assert {"domain", "page_type", "job_title", "commune", "confidence", "status", "promoted_offer_id"} <= signal_columns
    engine.dispose()
