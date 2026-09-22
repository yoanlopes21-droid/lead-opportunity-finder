"""Controlled France Travail department collection orchestration.

This module bridges the France Travail connector and the source-independent
persistence contract.  It deliberately contains no FastAPI or frontend code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol

from sqlalchemy.orm import Session

from app.models import CollectionRun
from app.services.france_travail.offers import (
    MAX_PAGE_SIZE,
    MAX_RESULTS_PER_QUERY,
    VAL_DE_MARNE_DEPARTMENT,
    CreationDateWindow,
    FranceTravailOffersClient,
    FranceTravailOffersError,
    NormalizedJobOffer,
    OfferSearchPage,
)
from app.services.persistence.offers import (
    CollectionRunStatus,
    OfferSnapshot,
    complete_collection_run,
    create_collection_run,
    fail_collection_run,
    record_skipped_offers,
    upsert_offer,
)


FRANCE_TRAVAIL_SOURCE = "france_travail"
DEPARTMENT_SCOPE_TYPE = "department"


class FranceTravailPageSource(Protocol):
    """The small connector contract consumed by this orchestration layer."""

    def iter_department_pages(self, page_size: int = MAX_PAGE_SIZE) -> Iterable[OfferSearchPage]:
        """Yield every page of the remote Val-de-Marne search."""


@dataclass(frozen=True)
class FranceTravailCollectionResult:
    """Non-sensitive summary of a completed or failed local collection."""

    run_id: int
    status: str
    offers_received: int
    offers_new: int
    offers_updated: int
    offers_unchanged: int
    offers_skipped: int
    offers_deactivated: int
    temporal_windows: int = 0
    pages_processed: int = 0


class FranceTravailDepartmentCollector:
    """Manually collect the full official offers scope for department 94.

    Each successfully processed page is committed so useful observations made
    before an intermittent remote failure remain available.  A failure always
    closes the run as ``failed`` and never calls the deactivation path.
    """

    def __init__(self, page_source: FranceTravailPageSource, page_size: int = MAX_PAGE_SIZE):
        self._page_source = page_source
        self._page_size = page_size

    def collect(self, session: Session, run: CollectionRun | None = None) -> FranceTravailCollectionResult:
        run = run or create_collection_run(
            session, source=FRANCE_TRAVAIL_SOURCE, scope_type=DEPARTMENT_SCOPE_TYPE,
            scope_value=VAL_DE_MARNE_DEPARTMENT,
        )
        if run.status == CollectionRunStatus.QUEUED:
            run.status = CollectionRunStatus.RUNNING
        session.commit()
        run_id = run.id

        try:
            expected_offset: int | None = 0
            for page in self._page_source.iter_department_pages(page_size=self._page_size):
                self._validate_page(page, expected_offset)
                record_skipped_offers(run, page.skipped_offers)
                for offer in page.offers:
                    self._validate_offer_scope(offer)
                    upsert_offer(session, run, _to_snapshot(offer))
                run.pages_processed += 1
                # A page is a durable progress checkpoint. It prevents a later
                # remote error from losing valid observations already collected.
                session.commit()
                expected_offset = page.next_offset

            if expected_offset is not None:
                raise ValueError("France Travail pagination ended before the final page")

            complete_collection_run(
                session,
                run,
                full_scope_completed=True,
                deactivate_unseen=True,
            )
            session.commit()
        except Exception as exc:
            # A persistence error can leave the current transaction invalid.
            # Roll it back first, then record the failed lifecycle state in a
            # fresh transaction.  No offer deactivation is attempted here.
            session.rollback()
            failed_run = session.get(CollectionRun, run_id)
            if failed_run is not None and failed_run.status == CollectionRunStatus.RUNNING:
                failed_run.error_summary = _safe_error_summary(exc)
                fail_collection_run(session, failed_run)
                session.commit()
            raise

        completed_run = session.get(CollectionRun, run_id)
        if completed_run is None:  # Defensive: a committed run must be readable.
            raise RuntimeError("completed collection run could not be reloaded")
        return _result_from_run(completed_run)

    @staticmethod
    def _validate_page(page: OfferSearchPage, expected_offset: int | None) -> None:
        """Reject invalid pagination metadata before it can imply completion."""
        if page.offset < 0 or page.limit < 1:
            raise ValueError("invalid France Travail page bounds")
        if expected_offset is None or page.offset != expected_offset:
            raise ValueError("France Travail pagination has a gap or an unexpected page")
        if page.next_offset is not None and page.next_offset <= page.offset:
            raise ValueError("France Travail pagination did not advance")

    @staticmethod
    def _validate_offer_scope(offer: NormalizedJobOffer) -> None:
        """Do not let an unexpected connector result escape the 94 run scope."""
        if offer.source != FRANCE_TRAVAIL_SOURCE:
            raise ValueError("France Travail collection received an offer from another source")
        if offer.department_code != VAL_DE_MARNE_DEPARTMENT:
            raise ValueError("France Travail collection received an offer outside department 94")


class FranceTravailCompleteDepartmentCollector:
    """Collect the complete 94 scope by recursively splitting creation-date windows.

    France Travail limits one query to ``MAX_RESULTS_PER_QUERY`` results.  A
    window is therefore persisted only after its first page confirms that its
    total fits inside that remote window.  Date bounds are inclusive; sibling
    windows overlap at their midpoint and the database upsert absorbs it.
    """

    _MAX_SPLIT_DEPTH = 64

    def __init__(
        self,
        client: FranceTravailOffersClient,
        page_size: int = MAX_PAGE_SIZE,
        now_provider=lambda: datetime.now(timezone.utc),
    ):
        self._client = client
        self._page_size = page_size
        self._now_provider = now_provider
        self._pages_processed = 0
        self._temporal_windows = 0

    def collect(self, session: Session, run: CollectionRun | None = None) -> FranceTravailCollectionResult:
        run = run or create_collection_run(
            session, source=FRANCE_TRAVAIL_SOURCE, scope_type=DEPARTMENT_SCOPE_TYPE,
            scope_value=VAL_DE_MARNE_DEPARTMENT,
        )
        if run.status == CollectionRunStatus.QUEUED:
            run.status = CollectionRunStatus.RUNNING
        session.commit()
        run_id = run.id
        initial_window = CreationDateWindow(
            datetime(1970, 1, 1, tzinfo=timezone.utc), self._now_provider()
        )
        try:
            self._collect_window(session, run, initial_window, depth=0)
            complete_collection_run(
                session,
                run,
                full_scope_completed=True,
                deactivate_unseen=True,
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            failed_run = session.get(CollectionRun, run_id)
            if failed_run is not None and failed_run.status == CollectionRunStatus.RUNNING:
                failed_run.error_summary = _safe_error_summary(exc)
                fail_collection_run(session, failed_run)
                session.commit()
            raise

        completed_run = session.get(CollectionRun, run_id)
        if completed_run is None:
            raise RuntimeError("completed collection run could not be reloaded")
        return _result_from_run(
            completed_run,
            temporal_windows=self._temporal_windows,
            pages_processed=self._pages_processed,
        )

    def _collect_window(
        self, session: Session, run: CollectionRun, window: CreationDateWindow, depth: int
    ) -> None:
        if depth > self._MAX_SPLIT_DEPTH:
            raise FranceTravailOffersError("France Travail creation-date splitting exceeded its safe depth.")
        first_page = self._client.search_department_page(
            offset=0, limit=self._page_size, creation_window=window
        )
        self._pages_processed += 1
        run.pages_processed += 1
        if first_page.total_count is None:
            raise FranceTravailOffersError(
                "France Travail did not provide a total for a creation-date window."
            )
        if first_page.total_count > MAX_RESULTS_PER_QUERY:
            left, right = window.split_inclusively()
            self._collect_window(session, run, left, depth + 1)
            self._collect_window(session, run, right, depth + 1)
            return

        self._temporal_windows += 1
        run.temporal_windows += 1
        for index, page in enumerate(
            self._client.iter_department_pages(
                page_size=self._page_size,
                creation_window=window,
                initial_page=first_page,
            )
        ):
            if index:
                self._pages_processed += 1
                run.pages_processed += 1
            FranceTravailDepartmentCollector._validate_page(page, page.offset)
            record_skipped_offers(run, page.skipped_offers)
            for offer in page.offers:
                FranceTravailDepartmentCollector._validate_offer_scope(offer)
                upsert_offer(session, run, _to_snapshot(offer))
            session.commit()


def _to_snapshot(offer: NormalizedJobOffer) -> OfferSnapshot:
    """Translate a connector model into the generic persistence contract."""
    return OfferSnapshot(
        source=offer.source,
        source_offer_id=offer.source_offer_id,
        title=offer.title,
        description=offer.description,
        company_name=offer.company_name,
        location_label=offer.location_label,
        commune=offer.commune,
        department_code=offer.department_code,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
        contract_type=offer.contract_type,
        salary=offer.salary,
        source_url=offer.source_url,
        origin=offer.origin,
    )


def _result_from_run(
    run: CollectionRun, temporal_windows: int = 0, pages_processed: int = 0
) -> FranceTravailCollectionResult:
    return FranceTravailCollectionResult(
        run_id=run.id,
        status=run.status,
        offers_received=run.offers_received,
        offers_new=run.offers_new,
        offers_updated=run.offers_updated,
        offers_unchanged=run.offers_unchanged,
        offers_skipped=run.offers_skipped,
        offers_deactivated=run.offers_deactivated,
        temporal_windows=temporal_windows,
        pages_processed=pages_processed,
    )


def _safe_error_summary(exc: Exception) -> str:
    """Keep a useful local failure explanation without exposing configuration."""
    message = str(exc).strip() or "France Travail collection failed."
    return message[:1000]
