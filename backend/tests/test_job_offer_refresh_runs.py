from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import BraveUsageEvent, CollectionRun, ObservedJobOffer
from app.services.collection.france_travail import FRANCE_TRAVAIL_SOURCE
from app.api.job_offer_refresh_runs import _response
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.job_offer_refresh_runs import (
    create_or_get_active_refresh_run,
    recover_orphaned_refresh_runs,
    release_refresh_worker,
    run_refresh,
)
from app.services.persistence.offers import (
    CollectionRunStatus,
    complete_collection_run,
    create_collection_run,
    ensure_collection_run_schema,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'refresh-runs.sqlite3'}")
    Base.metadata.create_all(engine)
    ensure_collection_run_schema(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def test_refresh_run_is_persistent_and_second_request_reuses_active_run(session):
    first, first_created = create_or_get_active_refresh_run(session)
    second, second_created = create_or_get_active_refresh_run(session)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert first.status == CollectionRunStatus.QUEUED
    release_refresh_worker(first.id)


def test_running_status_is_lightweight_and_timezone_aware(session):
    run, _ = create_or_get_active_refresh_run(session)
    run.status = CollectionRunStatus.RUNNING
    session.commit()

    response = _response(run)

    assert response.status == CollectionRunStatus.RUNNING
    assert response.active_opportunity_count is None
    assert response.started_at.utcoffset() == timezone.utc.utcoffset(response.started_at)
    release_refresh_worker(run.id)


def test_refresh_uses_existing_official_collector_without_brave_or_contacts(session, monkeypatch):
    run, _ = create_or_get_active_refresh_run(session)
    calls = []

    class FakeCollector:
        def __init__(self, client):
            calls.append(client)

        def collect(self, database_session, *, run):
            assert run.id == run_id
            run.status = CollectionRunStatus.RUNNING
            run.pages_processed = 3
            complete_collection_run(database_session, run, full_scope_completed=True)
            database_session.commit()
            return SimpleNamespace(status=CollectionRunStatus.COMPLETED)

    run_id = run.id
    monkeypatch.setattr("app.services.job_offer_refresh_runs.FranceTravailCompleteDepartmentCollector", FakeCollector)

    run_refresh(session, run.id, Settings())

    stored = session.get(CollectionRun, run.id)
    assert stored.status == CollectionRunStatus.COMPLETED
    assert stored.pages_processed == 3
    assert stored.active_offer_count == 0
    assert stored.active_opportunity_count == 0
    assert len(calls) == 1
    assert session.scalars(select(BraveUsageEvent)).all() == []


def test_failed_refresh_keeps_existing_active_offers(session, monkeypatch):
    offer = ObservedJobOffer(
        source=FRANCE_TRAVAIL_SOURCE, source_offer_id="still-active", title="Existing", department_code="94",
        first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc),
        last_changed_at=datetime.now(timezone.utc), is_active=True, observation_count=1,
    )
    session.add(offer)
    session.commit()
    run, _ = create_or_get_active_refresh_run(session)

    class FailingCollector:
        def __init__(self, client):
            pass

        def collect(self, database_session, *, run):
            run.status = CollectionRunStatus.FAILED
            run.error_summary = "official API unavailable"
            database_session.commit()
            raise RuntimeError("official API unavailable")

    monkeypatch.setattr("app.services.job_offer_refresh_runs.FranceTravailCompleteDepartmentCollector", FailingCollector)
    with pytest.raises(RuntimeError, match="official API unavailable"):
        run_refresh(session, run.id, Settings())

    assert session.get(ObservedJobOffer, offer.id).is_active is True
    assert session.get(CollectionRun, run.id).status == CollectionRunStatus.FAILED


def test_final_summary_uses_commercial_lead_page_total_count(session, monkeypatch):
    run, _ = create_or_get_active_refresh_run(session)

    class CompletingCollector:
        def __init__(self, client):
            pass

        def collect(self, database_session, *, run):
            run.status = CollectionRunStatus.RUNNING
            complete_collection_run(database_session, run, full_scope_completed=True)
            database_session.commit()
            return SimpleNamespace(status=CollectionRunStatus.COMPLETED)

    monkeypatch.setattr(
        "app.services.job_offer_refresh_runs.FranceTravailCompleteDepartmentCollector",
        CompletingCollector,
    )
    monkeypatch.setattr(
        "app.services.job_offer_refresh_runs.list_commercial_leads",
        lambda *_args, **_kwargs: SimpleNamespace(total_count=17),
    )

    run_refresh(session, run.id, Settings())

    stored = session.get(CollectionRun, run.id)
    assert stored.status == CollectionRunStatus.COMPLETED
    assert stored.active_opportunity_count == 17
    assert _response(stored).active_opportunity_count == 17


def test_orphan_recovery_fails_run_without_deactivating_active_stock(session):
    offer = ObservedJobOffer(
        source=FRANCE_TRAVAIL_SOURCE, source_offer_id="protected", title="Existing",
        company_name="Protected", department_code="94", first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc), last_changed_at=datetime.now(timezone.utc),
        is_active=True, observation_count=1,
    )
    session.add(offer)
    run = create_collection_run(session, FRANCE_TRAVAIL_SOURCE, "department", "94")
    session.commit()
    release_refresh_worker(run.id)

    assert recover_orphaned_refresh_runs(session) == 1

    session.refresh(run)
    session.refresh(offer)
    assert run.status == CollectionRunStatus.FAILED
    assert run.finished_at is not None
    assert "interrompue" in run.error_summary
    assert run.offers_deactivated == 0
    assert offer.is_active is True


def test_dashboard_read_and_second_launch_work_while_refresh_is_active(session):
    offer = ObservedJobOffer(
        source=FRANCE_TRAVAIL_SOURCE, source_offer_id="readable", title="Existing",
        company_name="Readable Company", department_code="94",
        first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc),
        last_changed_at=datetime.now(timezone.utc), is_active=True, observation_count=1,
    )
    session.add(offer)
    session.commit()
    first, created = create_or_get_active_refresh_run(session)

    with Session(session.bind) as concurrent_session:
        page = list_commercial_leads(
            concurrent_session,
            CommercialLeadQuery(department_code="94", limit=1),
        )
        same, second_created = create_or_get_active_refresh_run(concurrent_session)

    assert created is True
    assert page.total_count == 1
    assert second_created is False
    assert same.id == first.id
    release_refresh_worker(first.id)
