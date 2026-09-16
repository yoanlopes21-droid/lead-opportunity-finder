"""Resumable, provider-neutral orchestration of company enrichment batches."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from app.models import EnrichmentRun
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
from app.services.opportunities.company import CompanyOpportunity


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


class CompanyEnrichmentBatchOrchestrator:
    """Process unique company keys sequentially and persist each durable result."""

    def __init__(
        self,
        provider: CompanyEnrichmentProvider,
        policy: BatchPolicy = BatchPolicy(),
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._provider = provider
        self._policy = policy
        self._sleeper = sleeper
        self._clock = clock

    def run(
        self, session: Session, opportunities: Iterable[CompanyOpportunity]
    ) -> EnrichmentRun:
        selected = _unique_opportunities(opportunities)
        run = create_enrichment_run(
            session, self._provider.name, selected_count=len(selected), now=self._clock()
        )
        self._durable_flush(session)
        consecutive_systemic_errors = 0

        try:
            for opportunity in selected:
                fingerprint = compute_input_fingerprint(opportunity)
                existing = find_company_enrichment(
                    session, opportunity.company_key, self._provider.name
                )
                now = self._clock()
                if is_fresh_reusable_high_confidence(
                    existing, fingerprint, now, self._policy.confirmed_ttl
                ):
                    run.processed_count += 1
                    run.high_confidence_count += 1
                    consecutive_systemic_errors = 0
                    self._durable_flush(session)
                    continue

                result, attempts, error = self._call_with_limited_retry(opportunity)
                upsert_company_enrichment(
                    session=session,
                    run=run,
                    opportunity=opportunity,
                    result=result,
                    input_fingerprint=fingerprint,
                    attempts=attempts,
                    now=self._clock(),
                    error_type=error.error_type if error else None,
                    error_message=error.safe_message if error else None,
                )
                run.processed_count += 1
                _increment_status_counter(run, result.status)
                self._durable_flush(session)

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
                if run.error_count
                else EnrichmentRunStatus.COMPLETED
            )
            finish_enrichment_run(session, run, terminal_status, now=self._clock())
            self._durable_flush(session)
            return run
        except BaseException:
            if run.status == EnrichmentRunStatus.RUNNING:
                finish_enrichment_run(
                    session, run, EnrichmentRunStatus.FAILED, now=self._clock()
                )
                self._durable_flush(session)
            raise

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

    def _durable_flush(self, session: Session) -> None:
        if self._policy.commit_each_result:
            session.commit()
        else:
            session.flush()


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
