from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CollectionRun, ObservedJobOffer
from app.services.collection.france_travail import FranceTravailCompleteDepartmentCollector
from app.services.france_travail.offers import (
    MAX_RESULTS_PER_QUERY,
    CreationDateWindow,
    FranceTravailOffersError,
    NormalizedJobOffer,
    OfferSearchPage,
)
from app.services.persistence.offers import OfferSnapshot, create_collection_run, upsert_offer


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'temporal-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session
    engine.dispose()


class FakeTemporalClient:
    def __init__(self, totals, offers=None, failing_windows=()):
        self.totals = totals
        self.offers = offers or {}
        self.failing_windows = set(failing_windows)
        self.searches = []

    def search_department_page(self, offset, limit, creation_window):
        key = _key(creation_window)
        self.searches.append(key)
        if key in self.failing_windows:
            raise FranceTravailOffersError("simulated temporal request failure")
        return _page(offset, self.totals[key], self.offers.get(key, ()))

    def iter_department_pages(self, page_size, creation_window, initial_page):
        yield initial_page


def test_single_window_completes_and_keeps_department_filter_scope(session):
    initial = _window(0, 16)
    client = FakeTemporalClient({_key(initial): 1}, {_key(initial): (_offer("one"),)})

    result = _collector(client).collect(session)

    assert result.status == "completed"
    assert (result.temporal_windows, result.pages_processed, result.offers_new) == (1, 1, 1)
    run = session.get(CollectionRun, result.run_id)
    assert (run.source, run.scope_type, run.scope_value, run.is_full_scope) == ("france_travail", "department", "94", True)


def test_oversized_window_splits_and_boundary_duplicate_is_absorbed(session):
    initial, left, right = _split_windows()
    totals = {_key(initial): MAX_RESULTS_PER_QUERY + 1, _key(left): 1, _key(right): 1}
    boundary_offer = _offer("at-boundary")
    client = FakeTemporalClient(totals, {_key(left): (boundary_offer,), _key(right): (boundary_offer,)})

    result = _collector(client).collect(session)

    assert result.status == "completed"
    assert result.temporal_windows == 2
    assert result.offers_received == 1
    assert (result.offers_new, result.offers_unchanged) == (1, 0)
    assert len(session.scalars(select(ObservedJobOffer)).all()) == 1
    assert session.scalar(select(ObservedJobOffer)).observation_count == 1


def test_oversized_subwindow_is_split_recursively(session):
    initial, left, right = _split_windows()
    left_left, left_right = left.split_inclusively()
    totals = {
        _key(initial): MAX_RESULTS_PER_QUERY + 1,
        _key(left): MAX_RESULTS_PER_QUERY + 1,
        _key(right): 1,
        _key(left_left): 1,
        _key(left_right): 1,
    }
    offers = {
        _key(left_left): (_offer("a"),),
        _key(left_right): (_offer("b"),),
        _key(right): (_offer("c"),),
    }

    result = _collector(FakeTemporalClient(totals, offers)).collect(session)

    assert result.status == "completed"
    assert result.temporal_windows == 3
    assert result.pages_processed == 5
    assert result.offers_new == 3


def test_no_database_transaction_is_held_during_temporal_network_calls(session):
    initial, left, right = _split_windows()
    totals = {_key(initial): MAX_RESULTS_PER_QUERY + 1, _key(left): 0, _key(right): 1}

    class TransactionCheckingClient(FakeTemporalClient):
        def search_department_page(self, offset, limit, creation_window):
            assert session.in_transaction() is False
            return super().search_department_page(offset, limit, creation_window)

    result = _collector(TransactionCheckingClient(
        totals, {_key(right): (_offer("current"),)},
    )).collect(session)

    assert result.status == "completed"


def test_empty_subwindow_is_valid_and_does_not_block_complete_scope(session):
    initial, left, right = _split_windows()
    totals = {
        _key(initial): MAX_RESULTS_PER_QUERY + 1,
        _key(left): 0,
        _key(right): 1,
    }
    client = FakeTemporalClient(totals, {_key(right): (_offer("recent"),)})

    result = _collector(client).collect(session)

    assert result.status == "completed"
    assert result.temporal_windows == 2
    assert result.offers_received == 1
    assert result.offers_new == 1


def test_error_in_one_subwindow_fails_run_without_deactivation(session):
    _persist_existing(session, "protected", source="france_travail", department="94")
    initial, left, right = _split_windows()
    totals = {_key(initial): MAX_RESULTS_PER_QUERY + 1, _key(left): 1, _key(right): 1}
    client = FakeTemporalClient(totals, {_key(left): (_offer("seen"),)}, failing_windows={_key(right)})

    with pytest.raises(FranceTravailOffersError):
        _collector(client).collect(session)

    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert run.status == "failed" and run.offers_deactivated == 0
    assert _stored(session, "protected").is_active is True
    assert _stored(session, "seen").is_active is True


def test_only_all_successful_windows_can_deactivate_matching_scope(session):
    _persist_existing(session, "gone", source="france_travail", department="94")
    _persist_existing(session, "other-source", source="indeed", department="94")
    _persist_existing(session, "other-department", source="france_travail", department="75")
    initial = _window(0, 16)
    result = _collector(FakeTemporalClient({_key(initial): 1}, {_key(initial): (_offer("current"),)})).collect(session)

    assert result.status == "completed" and result.offers_deactivated == 1
    assert _stored(session, "gone").is_active is False
    assert _stored(session, "other-source", "indeed").is_active is True
    assert _stored(session, "other-department").is_active is True


def test_unsplittable_oversized_window_fails_instead_of_false_completion(session):
    single_second = _window(0, 0)
    client = FakeTemporalClient({_key(single_second): MAX_RESULTS_PER_QUERY + 1})

    with pytest.raises(FranceTravailOffersError, match="cannot be split"):
        FranceTravailCompleteDepartmentCollector(
            client, now_provider=lambda: single_second.end
        ).collect(session)

    assert session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc())).status == "failed"


def _collector(client):
    return FranceTravailCompleteDepartmentCollector(client, now_provider=lambda: _window(0, 16).end)


def _window(start_second, end_second):
    return CreationDateWindow(
        datetime(1970, 1, 1, 0, 0, start_second, tzinfo=timezone.utc),
        datetime(1970, 1, 1, 0, 0, end_second, tzinfo=timezone.utc),
    )


def _split_windows():
    initial = _window(0, 16)
    left, right = initial.split_inclusively()
    return initial, left, right


def _key(window):
    return window.start, window.end


def _page(offset, total, offers):
    return OfferSearchPage(tuple(offers), 206, offset, 150, total, None, 0)


def _offer(offer_id):
    return NormalizedJobOffer("france_travail", offer_id, "Title", None, None, None, None, "94", None, None, None, None, None, None)


def _persist_existing(session, offer_id, source, department):
    run = create_collection_run(session, source, "department", department)
    upsert_offer(session, run, OfferSnapshot(source=source, source_offer_id=offer_id, title="Old", department_code=department))
    session.commit()


def _stored(session, offer_id, source="france_travail"):
    result = session.scalar(select(ObservedJobOffer).where(ObservedJobOffer.source == source, ObservedJobOffer.source_offer_id == offer_id))
    assert result is not None
    return result
