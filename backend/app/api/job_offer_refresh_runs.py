"""HTTP API for manual France Travail offer refreshes."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import CollectionRun
from app.schemas import JobOfferRefreshRunResponse
from app.services.collection.france_travail import FRANCE_TRAVAIL_SOURCE
from app.services.job_offer_refresh_runs import active_offer_count, create_or_get_active_refresh_run, run_refresh
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.persistence.offers import CollectionRunStatus


router = APIRouter(prefix="/api/v1/job-offer-refresh-runs", tags=["job offer refresh runs"])


def _response(session: Session, run: CollectionRun) -> JobOfferRefreshRunResponse:
    active_offers = None
    active_opportunities = None
    if run.status == CollectionRunStatus.COMPLETED:
        active_offers = active_offer_count(session)
        active_opportunities = list_commercial_leads(session, CommercialLeadQuery(
            department_code="94", include_excluded=False, limit=1, offset=0,
        )).total
    return JobOfferRefreshRunResponse(
        id=run.id, status=run.status, started_at=run.started_at, finished_at=run.finished_at,
        offers_received=run.offers_received, offers_new=run.offers_new, offers_updated=run.offers_updated,
        offers_unchanged=run.offers_unchanged, offers_skipped=run.offers_skipped,
        offers_deactivated=run.offers_deactivated, temporal_windows=run.temporal_windows,
        pages_processed=run.pages_processed, active_offer_count=active_offers,
        active_opportunity_count=active_opportunities, error_summary=run.error_summary,
    )


def _background_refresh(run_id: int) -> None:
    with SessionLocal() as session:
        run_refresh(session, run_id, get_settings())


@router.post("", response_model=JobOfferRefreshRunResponse)
def create_refresh_run(background_tasks: BackgroundTasks, session: Session = Depends(get_db)) -> JobOfferRefreshRunResponse:
    run, created = create_or_get_active_refresh_run(session)
    if created:
        background_tasks.add_task(_background_refresh, run.id)
    return _response(session, run)


@router.get("/active", response_model=Optional[JobOfferRefreshRunResponse])
def get_active_refresh_run(session: Session = Depends(get_db)) -> Optional[JobOfferRefreshRunResponse]:
    run = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.source == FRANCE_TRAVAIL_SOURCE,
               CollectionRun.status.in_((CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)))
        .order_by(CollectionRun.id.desc())
    )
    return _response(session, run) if run is not None else None


@router.get("/{run_id}", response_model=JobOfferRefreshRunResponse)
def get_refresh_run(run_id: int, session: Session = Depends(get_db)) -> JobOfferRefreshRunResponse:
    run = session.get(CollectionRun, run_id)
    if run is None or run.source != FRANCE_TRAVAIL_SOURCE:
        raise HTTPException(status_code=404, detail="job offer refresh run does not exist")
    return _response(session, run)


@router.get("/{run_id}/events")
async def refresh_run_events(run_id: int) -> StreamingResponse:
    async def stream():
        last = None
        while True:
            with SessionLocal() as session:
                run = session.get(CollectionRun, run_id)
                if run is None or run.source != FRANCE_TRAVAIL_SOURCE:
                    yield 'event: error\ndata: {"detail": "job offer refresh run does not exist"}\n\n'
                    return
                payload = json.dumps(_response(session, run).model_dump(), default=lambda value: value.isoformat())
                if payload != last:
                    yield f"event: progress\ndata: {payload}\n\n"
                    last = payload
                if run.status in {CollectionRunStatus.COMPLETED, CollectionRunStatus.FAILED}:
                    return
            await asyncio.sleep(0.5)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
