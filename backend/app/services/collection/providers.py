"""Generic, source-isolated collection contracts for job-offer providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from sqlalchemy.orm import Session

from app.models import CollectionRun
from app.services.persistence.offers import (
    CollectionRunStatus,
    OfferSnapshot,
    RecruitmentSignalSnapshot,
    complete_collection_run,
    create_collection_run,
    fail_collection_run,
    record_skipped_offers,
    upsert_offer,
    upsert_recruitment_signal,
)


@dataclass(frozen=True)
class ProviderPage:
    page_number: int
    offers: Sequence[OfferSnapshot] = ()
    signals: Sequence[RecruitmentSignalSnapshot] = ()
    skipped_count: int = 0
    is_last: bool = True


class JobOfferProvider(Protocol):
    """A provider is a collection mechanism, not necessarily the offer source."""

    provider_id: str
    scope_type: str
    scope_value: str
    can_deactivate_unseen: bool

    def iter_pages(self) -> Iterable[ProviderPage]: ...


@dataclass(frozen=True)
class ProviderCollectionResult:
    run_id: int
    status: str
    offers_received: int
    offers_new: int
    offers_updated: int
    offers_unchanged: int
    offers_skipped: int
    offers_deactivated: int
    pages_processed: int


class JobOfferProviderCollector:
    """Persist one provider independently and deactivate only after full success."""

    def __init__(self, provider: JobOfferProvider):
        self.provider = provider

    def collect(
        self, session: Session, run: CollectionRun | None = None
    ) -> ProviderCollectionResult:
        run = run or create_collection_run(
            session,
            source=self.provider.provider_id,
            scope_type=self.provider.scope_type,
            scope_value=self.provider.scope_value,
        )
        if run.status == CollectionRunStatus.QUEUED:
            run.status = CollectionRunStatus.RUNNING
        if (run.source, run.scope_type, run.scope_value) != (
            self.provider.provider_id, self.provider.scope_type, self.provider.scope_value
        ):
            raise ValueError("collection run does not match provider scope")
        run_id = run.id
        session.commit()
        try:
            expected_page = 1
            saw_last = False
            for page in self.provider.iter_pages():
                if saw_last or page.page_number != expected_page:
                    raise ValueError("provider pagination is incomplete or out of order")
                if page.skipped_count < 0:
                    raise ValueError("provider skipped count must not be negative")
                record_skipped_offers(run, page.skipped_count)
                for snapshot in page.offers:
                    if snapshot.department_code != "94":
                        raise ValueError("provider returned an offer outside department 94")
                    upsert_offer(session, run, snapshot)
                for signal in page.signals:
                    upsert_recruitment_signal(session, run, signal)
                run.pages_processed += 1
                session.commit()
                expected_page += 1
                saw_last = page.is_last
            if not saw_last:
                raise ValueError("provider pagination ended before the final page")
            complete_collection_run(
                session, run, full_scope_completed=True,
                deactivate_unseen=self.provider.can_deactivate_unseen,
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            failed = session.get(CollectionRun, run_id)
            if failed is not None and failed.status == CollectionRunStatus.RUNNING:
                failed.error_summary = (str(exc).strip() or "Provider collection failed.")[:1000]
                fail_collection_run(session, failed)
                session.commit()
            raise
        completed = session.get(CollectionRun, run_id)
        if completed is None:
            raise RuntimeError("completed provider run could not be reloaded")
        return ProviderCollectionResult(
            run_id=completed.id, status=completed.status,
            offers_received=completed.offers_received, offers_new=completed.offers_new,
            offers_updated=completed.offers_updated,
            offers_unchanged=completed.offers_unchanged,
            offers_skipped=completed.offers_skipped,
            offers_deactivated=completed.offers_deactivated,
            pages_processed=completed.pages_processed,
        )
