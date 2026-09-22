"""Persistent UI orchestration for the existing France Travail collector.

This module deliberately constructs only the official offers client.  It has
no dependency on SearchRun, Brave, contactability, or company enrichment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CollectionRun, ObservedJobOffer
from app.services.collection.france_travail import (
    DEPARTMENT_SCOPE_TYPE,
    FRANCE_TRAVAIL_SOURCE,
    FranceTravailCompleteDepartmentCollector,
)
from app.services.france_travail.auth import FranceTravailOAuthClient
from app.services.france_travail.offers import FranceTravailOffersClient, VAL_DE_MARNE_DEPARTMENT
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.persistence.offers import (
    CollectionRunStatus,
    create_collection_run,
    fail_collection_run,
)


ACTIVE_STATUSES = (CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)
INTERRUPTED_MESSAGE = "La collecte a été interrompue par l’arrêt du serveur. Relancez-la manuellement."
_worker_lock = Lock()
_worker_run_ids: set[int] = set()


def create_or_get_active_refresh_run(session: Session) -> tuple[CollectionRun, bool]:
    """Claim one local worker slot or return the run already owned by it."""
    with _worker_lock:
        # A completed collector can still be persisting its final summary.
        # Keep that worker exclusive until its background task has fully exited.
        for worker_run_id in tuple(_worker_run_ids):
            worker_run = session.get(CollectionRun, worker_run_id)
            if worker_run is not None:
                return worker_run, False
            _worker_run_ids.discard(worker_run_id)
        existing = session.scalar(
            select(CollectionRun)
            .where(CollectionRun.source == FRANCE_TRAVAIL_SOURCE,
                   CollectionRun.status.in_(ACTIVE_STATUSES))
            .order_by(CollectionRun.id.desc())
        )
        if existing is not None and existing.id in _worker_run_ids:
            return existing, False
        if existing is not None:
            _mark_interrupted(session, existing)
            session.commit()
        run = create_collection_run(
            session, source=FRANCE_TRAVAIL_SOURCE, scope_type=DEPARTMENT_SCOPE_TYPE,
            scope_value=VAL_DE_MARNE_DEPARTMENT,
        )
        run.status = CollectionRunStatus.QUEUED
        session.commit()
        _worker_run_ids.add(run.id)
        return run, True


def run_refresh(session: Session, run_id: int, settings: Settings) -> None:
    try:
        run = session.get(CollectionRun, run_id)
        if run is None or run.status not in ACTIVE_STATUSES:
            return
        client = FranceTravailOffersClient(settings, FranceTravailOAuthClient(settings))
        result = FranceTravailCompleteDepartmentCollector(client).collect(session, run=run)
        if result.status == CollectionRunStatus.COMPLETED:
            _persist_final_summary(session, run_id)
    finally:
        release_refresh_worker(run_id)


def recover_orphaned_refresh_runs(session: Session) -> int:
    """Fail active database runs that have no worker in this process."""
    recovered = 0
    with _worker_lock:
        runs = session.scalars(
            select(CollectionRun).where(
                CollectionRun.source == FRANCE_TRAVAIL_SOURCE,
                CollectionRun.status.in_(ACTIVE_STATUSES),
            )
        ).all()
        for run in runs:
            if run.id not in _worker_run_ids:
                _mark_interrupted(session, run)
                recovered += 1
        if recovered:
            session.commit()
    return recovered


def release_refresh_worker(run_id: int) -> None:
    with _worker_lock:
        _worker_run_ids.discard(run_id)


def _mark_interrupted(session: Session, run: CollectionRun) -> None:
    run.error_summary = INTERRUPTED_MESSAGE
    fail_collection_run(session, run, now=datetime.now(timezone.utc))


def _persist_final_summary(session: Session, run_id: int) -> None:
    run = session.get(CollectionRun, run_id)
    if run is None or run.status != CollectionRunStatus.COMPLETED:
        return
    final_active_offer_count = active_offer_count(session)
    page = list_commercial_leads(session, CommercialLeadQuery(
        department_code=VAL_DE_MARNE_DEPARTMENT,
        include_excluded=False,
        limit=1,
        offset=0,
    ))
    final_active_opportunity_count = page.total_count
    # End the potentially long read snapshot before the short summary write.
    # This avoids upgrading a stale SQLite read transaction into a writer.
    session.rollback()
    session.execute(
        update(CollectionRun)
        .where(CollectionRun.id == run_id, CollectionRun.status == CollectionRunStatus.COMPLETED)
        .values(
            active_offer_count=final_active_offer_count,
            active_opportunity_count=final_active_opportunity_count,
        )
    )
    session.commit()


def active_offer_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(ObservedJobOffer).where(
        ObservedJobOffer.source == FRANCE_TRAVAIL_SOURCE,
        ObservedJobOffer.department_code == VAL_DE_MARNE_DEPARTMENT,
        ObservedJobOffer.is_active.is_(True),
    )) or 0)
