from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import BraveUsageEvent, CollectionRun, ObservedJobOffer
from app.services.collection.france_travail import FRANCE_TRAVAIL_SOURCE
from app.services.job_offer_refresh_runs import create_or_get_active_refresh_run, run_refresh
from app.services.persistence.offers import CollectionRunStatus, complete_collection_run, ensure_collection_run_schema


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

    run_id = run.id
    monkeypatch.setattr("app.services.job_offer_refresh_runs.FranceTravailCompleteDepartmentCollector", FakeCollector)

    run_refresh(session, run.id, Settings())

    stored = session.get(CollectionRun, run.id)
    assert stored.status == CollectionRunStatus.COMPLETED
    assert stored.pages_processed == 3
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
