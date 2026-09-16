from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ObservedJobOffer
from app.services.persistence.offers import (
    CollectionRunStatus,
    OfferSnapshot,
    UpsertOutcome,
    complete_collection_run,
    create_collection_run,
    fail_collection_run,
    record_skipped_offers,
    upsert_offer,
)


def at(hour: int) -> datetime:
    return datetime(2026, 9, 16, hour, tzinfo=timezone.utc)


def snapshot(**overrides) -> OfferSnapshot:
    values = {
        "source": "france_travail",
        "source_offer_id": "FT-123",
        "title": "Technicien maintenance",
        "description": None,
        "company_name": None,
        "location_label": "Créteil",
        "commune": "94028",
        "department_code": "94",
        "created_at": "2026-09-01T08:00:00Z",
        "updated_at": None,
        "contract_type": "CDI",
        "salary": None,
        "source_url": None,
        "origin": None,
    }
    values.update(overrides)
    return OfferSnapshot(**values)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'offers-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def run(session: Session, now: datetime = at(8)):
    return create_collection_run(session, "france_travail", "department", "94", now=now)


def test_new_offer_is_inserted_with_optional_null_fields(session):
    collection_run = run(session)

    result = upsert_offer(session, collection_run, snapshot(), now=at(9))

    assert result.outcome == UpsertOutcome.NEW
    assert result.offer.description is None
    assert result.offer.company_name is None
    assert result.offer.first_seen_at == at(9)
    assert result.offer.last_seen_at == at(9)
    assert result.offer.last_changed_at == at(9)
    assert result.offer.observation_count == 1
    assert result.offer.is_active is True
    assert collection_run.offers_received == 1
    assert collection_run.offers_new == 1


def test_same_offer_is_reobserved_without_duplicate_or_significant_change(session):
    first_run = run(session)
    first = upsert_offer(session, first_run, snapshot(), now=at(9)).offer
    second_run = run(session, now=at(10))

    result = upsert_offer(session, second_run, snapshot(), now=at(11))

    assert result.outcome == UpsertOutcome.UNCHANGED
    assert result.offer.id == first.id
    assert result.offer.first_seen_at == at(9)
    assert result.offer.last_seen_at == at(11)
    assert result.offer.last_changed_at == at(9)
    assert result.offer.observation_count == 2
    assert second_run.offers_unchanged == 1
    assert session.scalar(select(ObservedJobOffer).where(ObservedJobOffer.source_offer_id == "FT-123"))
    assert session.query(ObservedJobOffer).count() == 1


def test_same_offer_is_idempotent_within_one_collection_run(session):
    collection_run = run(session)
    first = upsert_offer(session, collection_run, snapshot(), now=at(9))
    duplicate = upsert_offer(session, collection_run, snapshot(), now=at(10))

    assert first.outcome == UpsertOutcome.NEW
    assert duplicate.outcome == UpsertOutcome.DUPLICATE_IN_RUN
    assert duplicate.offer.first_seen_at == at(9)
    assert duplicate.offer.last_seen_at == at(9)
    assert duplicate.offer.last_seen_run_id == collection_run.id
    assert duplicate.offer.observation_count == 1
    assert (collection_run.offers_received, collection_run.offers_new) == (1, 1)
    assert (collection_run.offers_updated, collection_run.offers_unchanged) == (0, 0)
    assert session.query(ObservedJobOffer).count() == 1


def test_significant_change_updates_last_changed_and_run_counter(session):
    initial_run = run(session)
    upsert_offer(session, initial_run, snapshot(), now=at(9))
    changed_run = run(session, now=at(10))

    result = upsert_offer(
        session,
        changed_run,
        snapshot(title="Technicien maintenance itinérant", salary="2 400 € brut"),
        now=at(11),
    )

    assert result.outcome == UpsertOutcome.UPDATED
    assert result.offer.title == "Technicien maintenance itinérant"
    assert result.offer.salary == "2 400 € brut"
    assert result.offer.last_changed_at == at(11)
    assert changed_run.offers_updated == 1


def test_inactive_offer_is_reactivated_without_changing_content_timestamp(session):
    initial_run = run(session)
    offer = upsert_offer(session, initial_run, snapshot(), now=at(9)).offer
    offer.is_active = False
    reactivation_run = run(session, now=at(10))

    result = upsert_offer(session, reactivation_run, snapshot(), now=at(11))

    assert result.outcome == UpsertOutcome.UPDATED
    assert result.offer.is_active is True
    assert result.offer.last_seen_at == at(11)
    assert result.offer.last_changed_at == at(9)
    assert reactivation_run.offers_updated == 1


def test_database_enforces_source_and_source_offer_id_uniqueness(session):
    collection_run = run(session)
    offer = upsert_offer(session, collection_run, snapshot(), now=at(9)).offer
    duplicate = ObservedJobOffer(
        source=offer.source,
        source_offer_id=offer.source_offer_id,
        title="Duplicate",
        first_seen_at=at(10),
        last_seen_at=at(10),
        last_changed_at=at(10),
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.flush()


def test_completed_run_tracks_counters_and_can_safely_deactivate_unseen(session):
    seed_run = run(session)
    upsert_offer(session, seed_run, snapshot(source_offer_id="FT-SEEN"), now=at(9))
    upsert_offer(session, seed_run, snapshot(source_offer_id="FT-MISSING"), now=at(9))
    complete_collection_run(session, seed_run, now=at(10), full_scope_completed=True)

    full_run = run(session, now=at(11))
    upsert_offer(session, full_run, snapshot(source_offer_id="FT-SEEN"), now=at(12))
    record_skipped_offers(full_run, count=2)
    complete_collection_run(
        session,
        full_run,
        now=at(13),
        full_scope_completed=True,
        deactivate_unseen=True,
    )
    missing_offer = session.scalar(
        select(ObservedJobOffer).where(ObservedJobOffer.source_offer_id == "FT-MISSING")
    )

    assert full_run.status == CollectionRunStatus.COMPLETED
    assert full_run.finished_at == at(13)
    assert full_run.is_full_scope is True
    assert full_run.offers_received == 1
    assert full_run.offers_unchanged == 1
    assert full_run.offers_skipped == 2
    assert full_run.offers_deactivated == 1
    assert missing_offer.is_active is False


def test_failed_or_incomplete_run_never_deactivates_absent_offers(session):
    seed_run = run(session)
    offer = upsert_offer(session, seed_run, snapshot(), now=at(9)).offer
    complete_collection_run(session, seed_run, now=at(10), full_scope_completed=True)

    failed_run = run(session, now=at(11))
    fail_collection_run(session, failed_run, now=at(12))
    assert failed_run.status == CollectionRunStatus.FAILED
    assert offer.is_active is True

    incomplete_run = run(session, now=at(13))
    with pytest.raises(ValueError):
        complete_collection_run(
            session,
            incomplete_run,
            now=at(14),
            full_scope_completed=False,
            deactivate_unseen=True,
        )
    assert incomplete_run.status == CollectionRunStatus.RUNNING
    assert offer.is_active is True
