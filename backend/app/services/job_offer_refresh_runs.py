"""Persistent UI orchestration for the existing France Travail collector.

This module deliberately constructs only the official offers client.  It has
no dependency on SearchRun, Brave, contactability, or company enrichment.
"""

from __future__ import annotations

from sqlalchemy import func, select
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
from app.services.persistence.offers import CollectionRunStatus, create_collection_run


ACTIVE_STATUSES = (CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)


def create_or_get_active_refresh_run(session: Session) -> tuple[CollectionRun, bool]:
    """Return the current official collection, preventing concurrent refreshes."""
    existing = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.source == FRANCE_TRAVAIL_SOURCE, CollectionRun.status.in_(ACTIVE_STATUSES))
        .order_by(CollectionRun.id.desc())
    )
    if existing is not None:
        return existing, False
    run = create_collection_run(
        session, source=FRANCE_TRAVAIL_SOURCE, scope_type=DEPARTMENT_SCOPE_TYPE,
        scope_value=VAL_DE_MARNE_DEPARTMENT,
    )
    run.status = CollectionRunStatus.QUEUED
    session.commit()
    return run, True


def run_refresh(session: Session, run_id: int, settings: Settings) -> None:
    run = session.get(CollectionRun, run_id)
    if run is None or run.status not in ACTIVE_STATUSES:
        return
    client = FranceTravailOffersClient(settings, FranceTravailOAuthClient(settings))
    FranceTravailCompleteDepartmentCollector(client).collect(session, run=run)


def active_offer_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(ObservedJobOffer).where(
        ObservedJobOffer.source == FRANCE_TRAVAIL_SOURCE,
        ObservedJobOffer.department_code == VAL_DE_MARNE_DEPARTMENT,
        ObservedJobOffer.is_active.is_(True),
    )) or 0)
