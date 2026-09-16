from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CollectionRun, ObservedJobOffer
from app.services.collection.france_travail import FranceTravailDepartmentCollector
from app.services.france_travail.offers import NormalizedJobOffer, OfferSearchPage
from app.services.persistence.offers import OfferSnapshot, create_collection_run, upsert_offer


class FakePageSource:
    def __init__(self, pages: list[OfferSearchPage], error_after: int | None = None):
        self.pages = pages
        self.error_after = error_after
        self.requested_page_size: int | None = None

    def iter_department_pages(self, page_size: int) -> Iterable[OfferSearchPage]:
        self.requested_page_size = page_size
        for index, page in enumerate(self.pages):
            if self.error_after == index:
                raise RuntimeError("simulated remote failure")
            yield page
        if self.error_after == len(self.pages):
            raise RuntimeError("simulated remote failure")


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'collection-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session
    engine.dispose()


def test_single_page_collection_persists_offers_and_counts_skips(session):
    source = FakePageSource([_page(0, [_offer("ft-1"), _offer("ft-2")], skipped=1)])

    result = FranceTravailDepartmentCollector(source, page_size=25).collect(session)

    assert source.requested_page_size == 25
    assert result.status == "completed"
    assert (result.offers_received, result.offers_new, result.offers_skipped) == (2, 2, 1)
    assert result.offers_deactivated == 0
    assert session.scalars(select(ObservedJobOffer)).all()[0].source == "france_travail"
    run = session.get(CollectionRun, result.run_id)
    assert run is not None and run.is_full_scope is True


def test_multi_page_collection_has_no_duplicates_and_uses_all_pages(session):
    source = FakePageSource([
        _page(0, [_offer("ft-1"), _offer("ft-2")], next_offset=2),
        _page(2, [_offer("ft-3")]),
    ])

    result = FranceTravailDepartmentCollector(source).collect(session)

    assert result.offers_received == result.offers_new == 3
    assert {item.source_offer_id for item in session.scalars(select(ObservedJobOffer)).all()} == {"ft-1", "ft-2", "ft-3"}


def test_reobservation_tracks_unchanged_changed_and_reactivated_offers(session):
    first = FranceTravailDepartmentCollector(FakePageSource([_page(0, [_offer("same"), _offer("changed"), _offer("inactive")])]))
    first.collect(session)
    inactive = _stored_offer(session, "inactive")
    inactive.is_active = False
    session.commit()

    later = _offer("changed", title="Updated title")
    result = FranceTravailDepartmentCollector(FakePageSource([_page(0, [_offer("same"), later, _offer("inactive")])])).collect(session)

    assert (result.offers_new, result.offers_updated, result.offers_unchanged) == (0, 2, 1)
    assert _stored_offer(session, "same").observation_count == 2
    assert _stored_offer(session, "changed").title == "Updated title"
    assert _stored_offer(session, "inactive").is_active is True


def test_error_mid_pagination_marks_failed_and_never_deactivates(session):
    _persist_existing(session, "missing", source="france_travail", department="94")
    source = FakePageSource([_page(0, [_offer("seen")]), _page(1, [_offer("never")])], error_after=1)

    with pytest.raises(RuntimeError, match="simulated remote failure"):
        FranceTravailDepartmentCollector(source).collect(session)

    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert run.status == "failed"
    assert _stored_offer(session, "missing").is_active is True
    assert _stored_offer(session, "seen").is_active is True
    assert session.scalar(select(ObservedJobOffer).where(ObservedJobOffer.source_offer_id == "never")) is None


def test_later_completed_scope_deactivates_only_unseen_matching_source_and_department(session):
    _persist_existing(session, "gone", source="france_travail", department="94")
    _persist_existing(session, "other-source", source="indeed", department="94")
    _persist_existing(session, "other-department", source="france_travail", department="75")

    result = FranceTravailDepartmentCollector(FakePageSource([_page(0, [_offer("current")])])).collect(session)

    assert result.offers_deactivated == 1
    assert _stored_offer(session, "gone").is_active is False
    assert _stored_offer(session, "other-source", source="indeed").is_active is True
    assert _stored_offer(session, "other-department").is_active is True


def test_invalid_pagination_is_failed_without_deactivation(session):
    _persist_existing(session, "protected", source="france_travail", department="94")
    invalid_page = _page(0, [_offer("seen")], next_offset=0)

    with pytest.raises(ValueError, match="did not advance"):
        FranceTravailDepartmentCollector(FakePageSource([invalid_page])).collect(session)

    assert session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc())).status == "failed"
    assert _stored_offer(session, "protected").is_active is True


def test_pagination_that_ends_before_its_next_page_is_failed(session):
    _persist_existing(session, "protected", source="france_travail", department="94")

    with pytest.raises(ValueError, match="ended before the final page"):
        FranceTravailDepartmentCollector(FakePageSource([_page(0, [_offer("seen")], next_offset=1)])).collect(session)

    assert session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc())).status == "failed"
    assert _stored_offer(session, "protected").is_active is True


def test_offer_outside_the_run_scope_fails_without_deactivation(session):
    _persist_existing(session, "protected", source="france_travail", department="94")
    outside_scope = replace(_offer("wrong-scope"), department_code="75")

    with pytest.raises(ValueError, match="outside department 94"):
        FranceTravailDepartmentCollector(FakePageSource([_page(0, [outside_scope])])).collect(session)

    assert session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc())).status == "failed"
    assert _stored_offer(session, "protected").is_active is True


def _page(offset: int, offers: list[NormalizedJobOffer], skipped: int = 0, next_offset: int | None = None) -> OfferSearchPage:
    return OfferSearchPage(
        offers=offers,
        http_status=206,
        offset=offset,
        limit=150,
        total_count=offset + len(offers) if next_offset is None else None,
        next_offset=next_offset,
        skipped_offers=skipped,
    )


def _offer(offer_id: str, title: str = "Analyste") -> NormalizedJobOffer:
    return NormalizedJobOffer(
        source="france_travail",
        source_offer_id=offer_id,
        title=title,
        description=None,
        company_name=None,
        location_label="Créteil",
        commune="Créteil",
        department_code="94",
        created_at="2026-09-01T00:00:00+00:00",
        updated_at=None,
        contract_type=None,
        salary=None,
        source_url=None,
        origin=None,
    )


def _persist_existing(session, offer_id: str, source: str, department: str) -> None:
    run = create_collection_run(session, source, "department", department)
    upsert_offer(
        session,
        run,
        OfferSnapshot(source=source, source_offer_id=offer_id, title="Existing", department_code=department),
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    session.commit()


def _stored_offer(session, offer_id: str, source: str = "france_travail") -> ObservedJobOffer:
    offer = session.scalar(select(ObservedJobOffer).where(ObservedJobOffer.source == source, ObservedJobOffer.source_offer_id == offer_id))
    assert offer is not None
    return offer
