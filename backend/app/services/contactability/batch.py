"""Durable, resource-aware batches for optional contact providers."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ContactEnrichmentRun, ContactEnrichmentRunItem, ContactProviderState
from app.services.contactability.contracts import (
    ContactProviderAttemptMetadata,
    ContactProviderResult,
    ContactProviderStatus,
    ContactTarget,
)
from app.services.contactability.persistence import persist_contact_provider_result


class ContactEnrichmentRunStatus:
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class ContactEnrichmentRunItemStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CACHED = "cached"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


_TERMINAL_ITEM_STATUSES = {
    ContactEnrichmentRunItemStatus.COMPLETED,
    ContactEnrichmentRunItemStatus.CACHED,
    ContactEnrichmentRunItemStatus.NOT_APPLICABLE,
    ContactEnrichmentRunItemStatus.ERROR,
}
_FRESH_RESOURCE_STATUSES = {
    ContactProviderStatus.COMPLETED,
    ContactProviderStatus.NOT_FOUND,
}
_TRANSIENT_ERROR_TYPES = {"timeout", "rate_limited", "server_error", "network"}


class ContactResourceProvider(Protocol):
    name: str
    resources: tuple[str, ...]

    @property
    def is_configured(self) -> bool:
        ...

    def target_fingerprint(self, target: ContactTarget) -> str:
        ...

    def inapplicability_reason(self, target: ContactTarget) -> Optional[str]:
        ...

    def resource_ttl(self, resource: str, status: str) -> timedelta:
        ...

    def discover_resource(self, target: ContactTarget, resource: str) -> ContactProviderResult:
        ...


@dataclass(frozen=True)
class ContactBatchPolicy:
    max_retries_per_resource: int = 1
    initial_backoff_seconds: float = 1.0
    systemic_error_threshold: int = 3
    commit_each_result: bool = True

    def __post_init__(self) -> None:
        if self.max_retries_per_resource < 0:
            raise ValueError("max_retries_per_resource must not be negative")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must not be negative")
        if self.systemic_error_threshold < 1:
            raise ValueError("systemic_error_threshold must be positive")


ProgressCallback = Callable[[ContactEnrichmentRun], None]


def ensure_contact_enrichment_schema(engine: Engine) -> None:
    """Explicitly create only the additive durable contact enrichment tables."""
    ContactEnrichmentRun.__table__.create(bind=engine, checkfirst=True)
    ContactEnrichmentRunItem.__table__.create(bind=engine, checkfirst=True)
    ContactProviderState.__table__.create(bind=engine, checkfirst=True)


class ContactEnrichmentBatchOrchestrator:
    """Persist all targets first, then cache and process each resource independently."""

    def __init__(
        self,
        provider: ContactResourceProvider,
        policy: ContactBatchPolicy = ContactBatchPolicy(),
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._sleeper = sleeper
        self._clock = clock
        self._progress_callback = progress_callback

    def run(self, session: Session, targets: Iterable[ContactTarget]) -> ContactEnrichmentRun:
        run = self.create_run(session, targets)
        return self.resume(session, run.id)

    def create_run(self, session: Session, targets: Iterable[ContactTarget]) -> ContactEnrichmentRun:
        selected = self._unique_sorted_targets(targets)
        run = ContactEnrichmentRun(
            provider=self._provider.name,
            status=ContactEnrichmentRunStatus.RUNNING,
            started_at=self._clock(),
            selected_count=len(selected),
        )
        session.add(run)
        session.flush()
        for position, target in enumerate(selected):
            session.add(ContactEnrichmentRunItem(
                run_id=run.id,
                deterministic_position=position,
                company_key=target.company_key,
                target_scope=target.scope,
                siren=target.siren,
                local_key=target.local_key,
                organization_name_snapshot=target.organization_name_snapshot,
                target_fingerprint=self._provider.target_fingerprint(target),
                input_snapshot=_serialize_target(target),
                status=ContactEnrichmentRunItemStatus.PENDING,
                attempt_count=0,
            ))
        self._durable_flush(session)
        return run

    def resume(self, session: Session, run_id: int) -> ContactEnrichmentRun:
        run = session.get(ContactEnrichmentRun, run_id)
        if run is None:
            raise ValueError("contact enrichment run does not exist")
        if run.provider != self._provider.name:
            raise ValueError("contact enrichment run provider does not match")
        if run.status in {ContactEnrichmentRunStatus.COMPLETED, ContactEnrichmentRunStatus.COMPLETED_WITH_ERRORS}:
            return run
        items = self._items(session, run.id)
        self._validate_selection(run, items)
        for item in items:
            if item.status == ContactEnrichmentRunItemStatus.PROCESSING:
                item.status = ContactEnrichmentRunItemStatus.PENDING
                item.started_at = None
        run.status = ContactEnrichmentRunStatus.RUNNING
        run.finished_at = None
        self._durable_flush(session)
        consecutive_transient_item_errors = 0
        try:
            for item in items:
                if item.status != ContactEnrichmentRunItemStatus.PENDING:
                    continue
                transient_error = self._process_item(session, run, item)
                if transient_error:
                    consecutive_transient_item_errors += 1
                    if consecutive_transient_item_errors >= self._policy.systemic_error_threshold:
                        self._finish_run(session, run, ContactEnrichmentRunStatus.FAILED)
                        return run
                else:
                    consecutive_transient_item_errors = 0
            terminal = (
                ContactEnrichmentRunStatus.COMPLETED_WITH_ERRORS
                if run.error_count else ContactEnrichmentRunStatus.COMPLETED
            )
            self._finish_run(session, run, terminal)
            return run
        except BaseException:
            if run.status == ContactEnrichmentRunStatus.RUNNING:
                self._finish_run(session, run, ContactEnrichmentRunStatus.FAILED)
            raise

    def _process_item(
        self, session: Session, run: ContactEnrichmentRun, item: ContactEnrichmentRunItem,
    ) -> bool:
        target = _deserialize_target(item.input_snapshot)
        reason = self._provider.inapplicability_reason(target)
        if reason is not None:
            self._finish_item(session, run, item, ContactEnrichmentRunItemStatus.NOT_APPLICABLE)
            return False
        if not self._provider.is_configured:
            self._finish_item(
                session, run, item, ContactEnrichmentRunItemStatus.ERROR,
                error_type="not_configured",
            )
            return False

        item.status = ContactEnrichmentRunItemStatus.PROCESSING
        item.started_at = self._clock()
        item.attempt_count += 1
        self._durable_flush(session)
        cached_resources = 0
        all_not_found = True
        transient_error = False
        for resource in self._provider.resources:
            input_fingerprint = self._resource_input_fingerprint(target, resource)
            state = self._find_state(session, input_fingerprint, resource)
            now = self._clock()
            if _is_fresh(state, now):
                cached_resources += 1
                if state.last_status != ContactProviderStatus.NOT_FOUND:
                    all_not_found = False
                continue
            result, calls, retries, final_error_type = self._call_resource(target, resource)
            run.external_call_count += calls
            run.retry_count += retries
            item.attempt_count += retries
            if result.status == ContactProviderStatus.COMPLETED:
                self._persist_resource_result(session, target, resource, result)
            state = self._record_resource_state(
                session, item, resource, input_fingerprint, result, calls, final_error_type,
            )
            if result.status == ContactProviderStatus.ERROR:
                transient_error = final_error_type in _TRANSIENT_ERROR_TYPES
                self._finish_item(
                    session, run, item, ContactEnrichmentRunItemStatus.ERROR,
                    error_type=final_error_type,
                )
                return transient_error
            if result.status in {ContactProviderStatus.NOT_CONFIGURED, ContactProviderStatus.NOT_APPLICABLE}:
                self._finish_item(
                    session, run, item, ContactEnrichmentRunItemStatus.ERROR,
                    error_type=result.status,
                )
                return False
            if result.status == ContactProviderStatus.COMPLETED:
                all_not_found = False
            self._durable_flush(session)

        if cached_resources == len(self._provider.resources):
            self._finish_item(session, run, item, ContactEnrichmentRunItemStatus.CACHED)
        else:
            self._finish_item(
                session, run, item, ContactEnrichmentRunItemStatus.COMPLETED,
                all_not_found=all_not_found,
            )
        return False

    def _call_resource(
        self, target: ContactTarget, resource: str,
    ) -> tuple[ContactProviderResult, int, int, Optional[str]]:
        retries = 0
        calls = 0
        while True:
            try:
                result = self._provider.discover_resource(target, resource)
            except Exception:
                result = ContactProviderResult(
                    provider=self._provider.name,
                    status=ContactProviderStatus.ERROR,
                    warnings=("contact_provider_unexpected_error",),
                    metadata=ContactProviderAttemptMetadata(
                        target_fingerprint=self._provider.target_fingerprint(target),
                        attempted_at=self._clock(),
                        request_count=0,
                        error_type="unexpected_error",
                    ),
                )
            request_count = result.metadata.request_count if result.metadata else 1
            calls += max(request_count, 0)
            error_type = result.metadata.error_type if result.metadata else None
            if (
                result.status != ContactProviderStatus.ERROR
                or error_type not in _TRANSIENT_ERROR_TYPES
                or retries >= self._policy.max_retries_per_resource
            ):
                return result, calls, retries, error_type
            delay = self._policy.initial_backoff_seconds * (2 ** retries)
            retries += 1
            self._sleeper(delay)

    def _record_resource_state(
        self,
        session: Session,
        item: ContactEnrichmentRunItem,
        resource: str,
        input_fingerprint: str,
        result: ContactProviderResult,
        calls: int,
        error_type: Optional[str],
    ) -> ContactProviderState:
        state = self._find_state(session, input_fingerprint, resource)
        now = self._clock()
        if state is None:
            state = ContactProviderState(
                provider=self._provider.name,
                target_fingerprint=input_fingerprint,
                resource=resource,
                company_key=item.company_key,
                target_scope=item.target_scope,
                siren=item.siren,
                local_key=item.local_key,
                last_status=result.status,
            )
            session.add(state)
        state.last_attempt_at = now
        state.attempt_count = (state.attempt_count or 0) + calls
        state.last_status = result.status
        state.last_error_type = _safe_error_type(error_type) if result.status == ContactProviderStatus.ERROR else None
        state.last_error_message = _safe_error_message(error_type) if result.status == ContactProviderStatus.ERROR else None
        if result.status in _FRESH_RESOURCE_STATUSES:
            state.last_success_at = now
            state.fresh_until = now + self._resource_ttl(resource, result)
            state.result_count = (
                len(result.candidates) + len(result.person_candidates) + len(result.artifacts)
            )
            state.last_error_type = None
            state.last_error_message = None
        else:
            state.fresh_until = None
        session.flush()
        return state

    def _resource_input_fingerprint(self, target: ContactTarget, resource: str) -> str:
        hook = getattr(self._provider, "resource_input_fingerprint", None)
        if hook is None:
            return self._provider.target_fingerprint(target)
        return hook(target, resource)

    def _persist_resource_result(
        self,
        session: Session,
        target: ContactTarget,
        resource: str,
        result: ContactProviderResult,
    ) -> None:
        hook = getattr(self._provider, "persist_resource_result", None)
        if hook is not None:
            hook(session, target, resource, result)
            return
        persist_contact_provider_result(session, result)

    def _resource_ttl(self, resource: str, result: ContactProviderResult) -> timedelta:
        hook = getattr(self._provider, "resource_ttl_for_result", None)
        if hook is not None:
            return hook(resource, result)
        return self._provider.resource_ttl(resource, result.status)

    def _finish_item(
        self,
        session: Session,
        run: ContactEnrichmentRun,
        item: ContactEnrichmentRunItem,
        status: str,
        *,
        error_type: Optional[str] = None,
        all_not_found: bool = False,
    ) -> None:
        item.status = status
        item.finished_at = self._clock()
        item.last_error_type = _safe_error_type(error_type)
        item.last_error_message = _safe_error_message(error_type)
        run.processed_count += 1
        if status == ContactEnrichmentRunItemStatus.CACHED:
            run.cached_count += 1
        elif status == ContactEnrichmentRunItemStatus.COMPLETED:
            run.completed_count += 1
            if all_not_found:
                run.not_found_count += 1
        elif status == ContactEnrichmentRunItemStatus.NOT_APPLICABLE:
            run.not_applicable_count += 1
        elif status == ContactEnrichmentRunItemStatus.ERROR:
            run.error_count += 1
        self._durable_flush(session)
        self._notify(run)

    def _finish_run(self, session: Session, run: ContactEnrichmentRun, status: str) -> None:
        run.status = status
        run.finished_at = self._clock()
        self._durable_flush(session)
        self._notify(run)

    def _validate_selection(
        self, run: ContactEnrichmentRun, items: tuple[ContactEnrichmentRunItem, ...],
    ) -> None:
        if len(items) != run.selected_count:
            raise ValueError("contact enrichment run selection is incomplete")
        for expected_position, item in enumerate(items):
            if item.deterministic_position != expected_position:
                raise ValueError("contact enrichment run selection order is inconsistent")
            target = _deserialize_target(item.input_snapshot)
            if self._provider.target_fingerprint(target) != item.target_fingerprint:
                raise ValueError("contact enrichment run target snapshot is inconsistent")

    def _items(self, session: Session, run_id: int) -> tuple[ContactEnrichmentRunItem, ...]:
        return tuple(session.scalars(
            select(ContactEnrichmentRunItem)
            .where(ContactEnrichmentRunItem.run_id == run_id)
            .order_by(ContactEnrichmentRunItem.deterministic_position)
        ))

    def _find_state(
        self, session: Session, target_fingerprint: str, resource: str,
    ) -> Optional[ContactProviderState]:
        return session.scalar(select(ContactProviderState).where(
            ContactProviderState.provider == self._provider.name,
            ContactProviderState.target_fingerprint == target_fingerprint,
            ContactProviderState.resource == resource,
        ))

    def _unique_sorted_targets(self, targets: Iterable[ContactTarget]) -> tuple[ContactTarget, ...]:
        selected = {
            self._provider.target_fingerprint(target): target for target in targets
        }
        return tuple(sorted(
            selected.values(),
            key=lambda item: (
                item.company_key.casefold(), item.scope, item.local_key or "", item.siren or "",
            ),
        ))

    def _durable_flush(self, session: Session) -> None:
        if self._policy.commit_each_result:
            session.commit()
        else:
            session.flush()

    def _notify(self, run: ContactEnrichmentRun) -> None:
        if self._progress_callback:
            self._progress_callback(run)


def _serialize_target(target: ContactTarget) -> dict:
    payload = asdict(target)
    payload.pop("warnings", None)
    return payload


def _deserialize_target(payload: dict) -> ContactTarget:
    values = dict(payload)
    values["warnings"] = ()
    return ContactTarget(**values)


def _is_fresh(state: Optional[ContactProviderState], now: datetime) -> bool:
    if state is None or state.last_status not in _FRESH_RESOURCE_STATUSES or state.fresh_until is None:
        return False
    return _as_utc(state.fresh_until) >= _as_utc(now)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_error_type(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return "".join(character if character.isalnum() or character in "_.-" else "_" for character in value)[:100]


def _safe_error_message(error_type: Optional[str]) -> Optional[str]:
    safe_type = _safe_error_type(error_type)
    return f"Contact provider resource failed ({safe_type})." if safe_type else None
