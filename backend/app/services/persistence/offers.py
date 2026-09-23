"""Persistence of normalized offers and safe collection-run lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from sqlalchemy import func, inspect, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import CollectionRun, ObservedJobOffer, RecruitmentSignal


class CollectionRunStatus:
    QUEUED = "queued"
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
    discovery_provider: Optional[str] = None
    origin: Optional[str] = None
    recruitment_signal_id: Optional[int] = None


@dataclass(frozen=True)
class RecruitmentSignalSnapshot:
    """Minimum durable form of a web result that is not a verified job offer."""

    discovery_provider: str
    source: str
    source_url: str
    domain: Optional[str] = None
    page_type: str = "unknown"
    title: Optional[str] = None
    snippet: Optional[str] = None
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    location_label: Optional[str] = None
    commune: Optional[str] = None
    department_code: Optional[str] = None
    published_at: Optional[str] = None
    confidence: Optional[float] = None
    detection_reason: Optional[str] = None
    extraction: Optional[dict] = None
    status: str = "new"


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
    "discovery_provider",
    "origin",
    "recruitment_signal_id",
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
    if snapshot.source != run.source and snapshot.discovery_provider != run.source:
        raise ValueError("offer source or discovery provider must match its collection run")

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


def upsert_recruitment_signal(
    session: Session,
    run: CollectionRun,
    snapshot: RecruitmentSignalSnapshot,
    now: Optional[datetime] = None,
) -> RecruitmentSignal:
    """Persist a clue separately; missing offer fields are never synthesized."""
    _require_running_run(run)
    if snapshot.discovery_provider != run.source:
        raise ValueError("signal discovery provider must match its collection run")
    if not snapshot.source_url.strip():
        raise ValueError("signal source URL is required")
    observed_at = now or _utc_now()
    signal = session.scalar(select(RecruitmentSignal).where(
        RecruitmentSignal.discovery_provider == snapshot.discovery_provider,
        RecruitmentSignal.source_url == snapshot.source_url,
    ))
    if signal is None:
        signal = RecruitmentSignal(
            **{**snapshot.__dict__, "extraction": snapshot.extraction or {}},
            first_seen_at=observed_at, last_seen_at=observed_at,
            observation_count=1, last_seen_run_id=run.id,
        )
        session.add(signal)
        run.signals_found += 1
    elif signal.last_seen_run_id != run.id:
        for name, value in snapshot.__dict__.items():
            if name == "status" and signal.status in {"promoted", "dismissed"}:
                continue
            setattr(signal, name, value if name != "extraction" else (value or {}))
        signal.last_seen_at = observed_at
        signal.observation_count += 1
        signal.last_seen_run_id = run.id
        run.signals_found += 1
    session.flush()
    return signal


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
    if (
        deactivate_unseen and run.offers_received == 0 and not allow_empty_deactivation
        and _active_scope_offer_count(session, run) > 0
    ):
        raise ValueError("empty collections cannot deactivate existing offers without explicit approval")

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
    scope_filter = None
    if run.scope_type == "department":
        scope_filter = ObservedJobOffer.department_code == run.scope_value
    elif run.scope_type == "provider_board":
        scope_filter = ObservedJobOffer.origin == run.scope_value
    else:
        raise ValueError("automatic deactivation does not support this scope type")

    result = session.execute(
        update(ObservedJobOffer)
        .where(
            ObservedJobOffer.source == run.source,
            scope_filter,
            ObservedJobOffer.is_active.is_(True),
            or_(
                ObservedJobOffer.last_seen_run_id.is_(None),
                ObservedJobOffer.last_seen_run_id != run.id,
            ),
        )
        .values(is_active=False)
    )
    return result.rowcount or 0


def _active_scope_offer_count(session: Session, run: CollectionRun) -> int:
    if run.scope_type == "department":
        scope_filter = ObservedJobOffer.department_code == run.scope_value
    elif run.scope_type == "provider_board":
        scope_filter = ObservedJobOffer.origin == run.scope_value
    else:
        return 0
    return int(session.scalar(
        select(func.count()).select_from(ObservedJobOffer).where(
            ObservedJobOffer.source == run.source,
            scope_filter,
            ObservedJobOffer.is_active.is_(True),
        )
    ) or 0)


def _snapshot_values(snapshot: OfferSnapshot) -> dict:
    return {field_name: getattr(snapshot, field_name) for field_name in ("source", "source_offer_id", *_SIGNIFICANT_FIELDS)}


def _require_running_run(run: CollectionRun) -> None:
    if run.status != CollectionRunStatus.RUNNING:
        raise ValueError("collection run is not running")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_collection_run_schema(engine: Engine) -> None:
    """Add progress columns to existing local databases without a migration tool."""
    if "collection_runs" not in inspect(engine).get_table_names():
        return
    existing = {column["name"] for column in inspect(engine).get_columns("collection_runs")}
    additions = {
        "temporal_windows": "INTEGER NOT NULL DEFAULT 0",
        "pages_processed": "INTEGER NOT NULL DEFAULT 0",
        "error_summary": "VARCHAR(1000)",
        "active_offer_count": "INTEGER",
        "active_opportunity_count": "INTEGER",
        "signals_found": "INTEGER NOT NULL DEFAULT 0",
        "signals_promoted": "INTEGER NOT NULL DEFAULT 0",
        "brave_requests_used": "INTEGER NOT NULL DEFAULT 0",
        "target_signal_count": "INTEGER",
        "brave_hard_cap": "INTEGER",
        "stop_requested": "BOOLEAN NOT NULL DEFAULT 0",
        "completion_reason": "VARCHAR(80)",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE collection_runs ADD COLUMN {name} {definition}"))
    if "observed_job_offers" in inspect(engine).get_table_names():
        offer_columns = {
            column["name"] for column in inspect(engine).get_columns("observed_job_offers")
        }
        with engine.begin() as connection:
            if "discovery_provider" not in offer_columns:
                connection.execute(text(
                    "ALTER TABLE observed_job_offers ADD COLUMN discovery_provider VARCHAR(120)"
                ))
            if "recruitment_signal_id" not in offer_columns:
                connection.execute(text(
                    "ALTER TABLE observed_job_offers ADD COLUMN recruitment_signal_id INTEGER"
                ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_observed_job_offers_discovery_provider "
                "ON observed_job_offers (discovery_provider)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_observed_job_offers_recruitment_signal_id "
                "ON observed_job_offers (recruitment_signal_id)"
            ))
    if "recruitment_signals" in inspect(engine).get_table_names():
        signal_columns = {
            column["name"] for column in inspect(engine).get_columns("recruitment_signals")
        }
        signal_additions = {
            "domain": "VARCHAR(255)",
            "page_type": "VARCHAR(40) NOT NULL DEFAULT 'unknown'",
            "job_title": "VARCHAR(500)",
            "commune": "VARCHAR(255)",
            "published_at": "VARCHAR(64)",
            "confidence": "FLOAT",
            "detection_reason": "VARCHAR(500)",
            "extraction": "JSON NOT NULL DEFAULT '{}'",
            "status": "VARCHAR(30) NOT NULL DEFAULT 'new'",
            "promoted_offer_id": "INTEGER",
            "reviewed_at": "DATETIME",
        }
        with engine.begin() as connection:
            for name, definition in signal_additions.items():
                if name not in signal_columns:
                    connection.execute(text(
                        f"ALTER TABLE recruitment_signals ADD COLUMN {name} {definition}"
                    ))
            for name in ("domain", "page_type", "job_title", "commune", "status", "promoted_offer_id"):
                connection.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_recruitment_signals_{name} "
                    f"ON recruitment_signals ({name})"
                ))
