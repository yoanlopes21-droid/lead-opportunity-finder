"""Durable, resumable, provider-neutral company enrichment batches."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompanyEnrichment, EnrichmentRun, EnrichmentRunItem
from app.services.company_enrichment.contracts import (
    CompanyEnrichmentProvider,
    MatchStatus,
    ProviderCallError,
    ProviderEnrichmentResult,
)
from app.services.company_enrichment.persistence import (
    EnrichmentRunStatus,
    compute_input_fingerprint,
    create_enrichment_run,
    find_company_enrichment,
    finish_enrichment_run,
    is_fresh_reusable_high_confidence,
    upsert_company_enrichment,
)
from app.services.opportunities.company import CompanyOpportunity, OpportunitySignal


class RunItemStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CACHED = "cached"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class BatchPolicy:
    confirmed_ttl: timedelta = timedelta(days=30)
    max_retries: int = 1
    initial_backoff_seconds: float = 1.0
    systemic_error_threshold: int = 3
    commit_each_result: bool = True

    def __post_init__(self) -> None:
        if self.confirmed_ttl.total_seconds() < 0:
            raise ValueError("confirmed_ttl must not be negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must not be negative")
        if self.systemic_error_threshold < 1:
            raise ValueError("systemic_error_threshold must be positive")


ProgressCallback = Callable[[EnrichmentRun], None]


class CompanyEnrichmentBatchOrchestrator:
    """Persist selection first, then process or resume it item by item."""

    def __init__(
        self,
        provider: CompanyEnrichmentProvider,
        policy: BatchPolicy = BatchPolicy(),
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self._provider = provider
        self._policy = policy
        self._sleeper = sleeper
        self._clock = clock
        self._progress_callback = progress_callback

    def run(
        self, session: Session, opportunities: Iterable[CompanyOpportunity]
    ) -> EnrichmentRun:
        run = self.create_run(session, opportunities)
        return self.resume(session, run.id)

    def create_run(
        self, session: Session, opportunities: Iterable[CompanyOpportunity]
    ) -> EnrichmentRun:
        selected = _unique_opportunities(opportunities)
        run = create_enrichment_run(
            session, self._provider.name, selected_count=len(selected), now=self._clock()
        )
        for position, opportunity in enumerate(selected):
            session.add(
                EnrichmentRunItem(
                    run_id=run.id,
                    company_key=opportunity.company_key,
                    source_company_name=opportunity.company_name,
                    selection_position=position,
                    input_snapshot=_serialize_opportunity(opportunity),
                    input_fingerprint=compute_input_fingerprint(opportunity),
                    status=RunItemStatus.PENDING,
                    attempt_count=0,
                )
            )
        self._durable_flush(session)
        return run

    def resume(self, session: Session, run_id: int) -> EnrichmentRun:
        run = session.get(EnrichmentRun, run_id)
        if run is None:
            raise ValueError("enrichment run does not exist")
        if run.provider != self._provider.name:
            raise ValueError("enrichment run provider does not match")
        if run.status in {
            EnrichmentRunStatus.COMPLETED,
            EnrichmentRunStatus.COMPLETED_WITH_ERRORS,
        }:
            return run

        items = self._items(session, run.id)
        if run.selected_count and not items:
            raise ValueError(
                "enrichment run predates durable run items and cannot be resumed safely"
            )
        if len(items) != run.selected_count:
            raise ValueError("enrichment run selection is incomplete")
        for item in items:
            if item.status == RunItemStatus.PROCESSING:
                item.status = RunItemStatus.PENDING
                item.started_at = None
        run.status = EnrichmentRunStatus.RUNNING
        run.finished_at = None
        self._durable_flush(session)
        consecutive_systemic_errors = 0

        try:
            for item in items:
                if item.status != RunItemStatus.PENDING:
                    continue
                opportunity = _deserialize_opportunity(item.input_snapshot)
                existing = find_company_enrichment(
                    session, opportunity.company_key, self._provider.name
                )
                now = self._clock()
                if is_fresh_reusable_high_confidence(
                    existing,
                    item.input_fingerprint,
                    now,
                    self._policy.confirmed_ttl,
                ):
                    self._finish_item(
                        session, run, item, RunItemStatus.CACHED,
                        MatchStatus.HIGH_CONFIDENCE, existing,
                    )
                    consecutive_systemic_errors = 0
                    continue

                item.status = RunItemStatus.PROCESSING
                item.started_at = now
                item.attempt_count += 1
                self._durable_flush(session)

                result, attempts, error = self._call_with_limited_retry(opportunity)
                item.attempt_count += attempts - 1
                enrichment = upsert_company_enrichment(
                    session=session,
                    run=run,
                    opportunity=opportunity,
                    result=result,
                    input_fingerprint=item.input_fingerprint,
                    attempts=attempts,
                    now=self._clock(),
                    error_type=error.error_type if error else None,
                    error_message=error.safe_message if error else None,
                )
                self._finish_item(
                    session, run, item,
                    RunItemStatus.ERROR if error else RunItemStatus.COMPLETED,
                    result.status, enrichment, error,
                )

                if error and error.transient:
                    consecutive_systemic_errors += 1
                    if consecutive_systemic_errors >= self._policy.systemic_error_threshold:
                        finish_enrichment_run(
                            session, run, EnrichmentRunStatus.FAILED, now=self._clock()
                        )
                        self._durable_flush(session)
                        return run
                else:
                    consecutive_systemic_errors = 0

            terminal_status = (
                EnrichmentRunStatus.COMPLETED_WITH_ERRORS
                if run.error_count else EnrichmentRunStatus.COMPLETED
            )
            finish_enrichment_run(session, run, terminal_status, now=self._clock())
            self._durable_flush(session)
            self._notify(run)
            return run
        except BaseException:
            if run.status == EnrichmentRunStatus.RUNNING:
                finish_enrichment_run(
                    session, run, EnrichmentRunStatus.FAILED, now=self._clock()
                )
                self._durable_flush(session)
            raise

    def _finish_item(
        self,
        session: Session,
        run: EnrichmentRun,
        item: EnrichmentRunItem,
        item_status: str,
        match_status: str,
        enrichment: Optional[CompanyEnrichment],
        error: Optional[ProviderCallError] = None,
    ) -> None:
        item.status = item_status
        item.match_status = match_status
        item.finished_at = self._clock()
        item.enrichment_id = enrichment.id if enrichment else None
        item.last_error_type = error.error_type if error else None
        item.last_error_message = error.safe_message if error else None
        run.processed_count += 1
        _increment_status_counter(run, match_status)
        self._durable_flush(session)
        self._notify(run)

    def _call_with_limited_retry(
        self, opportunity: CompanyOpportunity
    ) -> tuple[ProviderEnrichmentResult, int, Optional[ProviderCallError]]:
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._provider.enrich(opportunity), attempts, None
            except ProviderCallError as error:
                if not error.transient or attempts > self._policy.max_retries:
                    return (
                        ProviderEnrichmentResult(
                            status=MatchStatus.ERROR,
                            entity_sector_type="unknown",
                            provider_source=self._provider.source,
                        ),
                        attempts,
                        error,
                    )
                delay = self._policy.initial_backoff_seconds * (2 ** (attempts - 1))
                self._sleeper(delay)

    def _items(self, session: Session, run_id: int) -> tuple[EnrichmentRunItem, ...]:
        return tuple(
            session.scalars(
                select(EnrichmentRunItem)
                .where(EnrichmentRunItem.run_id == run_id)
                .order_by(EnrichmentRunItem.selection_position)
            )
        )

    def _durable_flush(self, session: Session) -> None:
        if self._policy.commit_each_result:
            session.commit()
        else:
            session.flush()

    def _notify(self, run: EnrichmentRun) -> None:
        if self._progress_callback:
            self._progress_callback(run)


def _serialize_opportunity(opportunity: CompanyOpportunity) -> dict:
    return asdict(opportunity)


def _deserialize_opportunity(payload: dict) -> CompanyOpportunity:
    tuple_fields = {
        "sources", "contract_types", "communes", "location_labels", "offer_ids", "job_titles"
    }
    values = dict(payload)
    for field_name in tuple_fields:
        values[field_name] = tuple(values.get(field_name) or ())
    values["signals"] = tuple(
        OpportunitySignal(**signal) for signal in values.get("signals") or ()
    )
    return CompanyOpportunity(**values)


def _unique_opportunities(
    opportunities: Iterable[CompanyOpportunity],
) -> tuple[CompanyOpportunity, ...]:
    selected: dict[str, CompanyOpportunity] = {}
    for opportunity in opportunities:
        selected.setdefault(opportunity.company_key, opportunity)
    return tuple(selected.values())


def _increment_status_counter(run: EnrichmentRun, status: str) -> None:
    field_by_status = {
        MatchStatus.HIGH_CONFIDENCE: "high_confidence_count",
        MatchStatus.REVIEW_NEEDED: "review_needed_count",
        MatchStatus.AMBIGUOUS: "ambiguous_count",
        MatchStatus.GENERIC: "generic_count",
        MatchStatus.NOT_FOUND: "not_found_count",
        MatchStatus.ERROR: "error_count",
    }
    field_name = field_by_status[status]
    setattr(run, field_name, getattr(run, field_name) + 1)
