"""Persistence of normalized offers and safe collection-run lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models import CollectionRun, ObservedJobOffer


class CollectionRunStatus:
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UpsertOutcome(str, Enum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    DUPLICATE_IN_RUN = "duplicate_in_run"


@dataclass(frozen=True)
class OfferSnapshot:
    """The source-independent data contract accepted by local offer persistence."""

    source: str
    source_offer_id: str
    title: str
    description: Optional[str] = None
    company_name: Optional[str] = None
    location_label: Optional[str] = None
    commune: Optional[str] = None
    department_code: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    contract_type: Optional[str] = None
    salary: Optional[str] = None
    source_url: Optional[str] = None
    origin: Optional[str] = None


@dataclass(frozen=True)
class UpsertResult:
    offer: ObservedJobOffer
    outcome: UpsertOutcome


_SIGNIFICANT_FIELDS = (
    "title",
    "description",
    "company_name",
    "location_label",
    "commune",
    "department_code",
    "created_at",
    "updated_at",
    "contract_type",
    "salary",
    "source_url",
    "origin",
)


def create_collection_run(
    session: Session,
    source: str,
    scope_type: str,
    scope_value: str,
    now: Optional[datetime] = None,
) -> CollectionRun:
    run = CollectionRun(
        source=source,
        scope_type=scope_type,
        scope_value=scope_value,
        status=CollectionRunStatus.RUNNING,
        started_at=now or _utc_now(),
    )
    session.add(run)
    session.flush()
    return run


def upsert_offer(
    session: Session,
    run: CollectionRun,
    snapshot: OfferSnapshot,
    now: Optional[datetime] = None,
) -> UpsertResult:
    """Persist one observation while retaining first-seen and significant-change history."""
    _require_running_run(run)
    if snapshot.source != run.source:
        raise ValueError("offer source must match its collection run")

    observed_at = now or _utc_now()
    offer = session.scalar(
        select(ObservedJobOffer).where(
            ObservedJobOffer.source == snapshot.source,
            ObservedJobOffer.source_offer_id == snapshot.source_offer_id,
        )
    )

    if offer is None:
        run.offers_received += 1
        offer = ObservedJobOffer(
            **_snapshot_values(snapshot),
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            last_changed_at=observed_at,
            is_active=True,
            observation_count=1,
            last_seen_run_id=run.id,
        )
        session.add(offer)
        run.offers_new += 1
        session.flush()
        return UpsertResult(offer=offer, outcome=UpsertOutcome.NEW)

    # A temporal boundary (or any source-side overlap) can emit the same offer
    # more than once in one collection. One local observation represents one
    # distinct collection run, not one raw remote occurrence.
    if offer.last_seen_run_id == run.id:
        return UpsertResult(offer=offer, outcome=UpsertOutcome.DUPLICATE_IN_RUN)

    run.offers_received += 1
    has_data_changes = False
    for field_name, value in _snapshot_values(snapshot).items():
        if getattr(offer, field_name) != value:
            setattr(offer, field_name, value)
            has_data_changes = True

    was_inactive = not offer.is_active
    offer.last_seen_at = observed_at
    offer.last_seen_run_id = run.id
    offer.observation_count += 1
    offer.is_active = True
    if has_data_changes:
        offer.last_changed_at = observed_at

    if has_data_changes or was_inactive:
        run.offers_updated += 1
        outcome = UpsertOutcome.UPDATED
    else:
        run.offers_unchanged += 1
        outcome = UpsertOutcome.UNCHANGED
    session.flush()
    return UpsertResult(offer=offer, outcome=outcome)


def record_skipped_offers(run: CollectionRun, count: int = 1) -> None:
    _require_running_run(run)
    if count < 0:
        raise ValueError("count must be positive")
    run.offers_skipped += count


def complete_collection_run(
    session: Session,
    run: CollectionRun,
    now: Optional[datetime] = None,
    full_scope_completed: bool = False,
    deactivate_unseen: bool = False,
    allow_empty_deactivation: bool = False,
) -> CollectionRun:
    """Close a successful run; deactivation requires explicit full-scope confirmation."""
    _require_running_run(run)
    if deactivate_unseen and not full_scope_completed:
        raise ValueError("only an explicitly complete scope may deactivate unseen offers")
    if deactivate_unseen and run.offers_received == 0 and not allow_empty_deactivation:
        raise ValueError("empty collections cannot deactivate offers without explicit approval")

    run.status = CollectionRunStatus.COMPLETED
    run.finished_at = now or _utc_now()
    run.is_full_scope = full_scope_completed
    if deactivate_unseen:
        run.offers_deactivated = _deactivate_unseen_offers(session, run)
    session.flush()
    return run


def fail_collection_run(
    session: Session, run: CollectionRun, now: Optional[datetime] = None
) -> CollectionRun:
    """Close a failed/interrupted run without changing any offer activity state."""
    _require_running_run(run)
    run.status = CollectionRunStatus.FAILED
    run.finished_at = now or _utc_now()
    session.flush()
    return run


def _deactivate_unseen_offers(session: Session, run: CollectionRun) -> int:
    if run.status != CollectionRunStatus.COMPLETED or not run.is_full_scope:
        raise ValueError("only a completed full-scope run may deactivate unseen offers")
    if run.scope_type != "department":
        raise ValueError("automatic deactivation currently supports department scopes only")

    result = session.execute(
        update(ObservedJobOffer)
        .where(
            ObservedJobOffer.source == run.source,
            ObservedJobOffer.department_code == run.scope_value,
            ObservedJobOffer.is_active.is_(True),
            or_(
                ObservedJobOffer.last_seen_run_id.is_(None),
                ObservedJobOffer.last_seen_run_id != run.id,
            ),
        )
        .values(is_active=False)
    )
    return result.rowcount or 0


def _snapshot_values(snapshot: OfferSnapshot) -> dict:
    return {field_name: getattr(snapshot, field_name) for field_name in ("source", "source_offer_id", *_SIGNIFICANT_FIELDS)}


def _require_running_run(run: CollectionRun) -> None:
    if run.status != CollectionRunStatus.RUNNING:
        raise ValueError("collection run is not running")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
