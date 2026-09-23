"""Application endpoints for bounded Open Web recruitment discovery."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.source_boards import source_run_response
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import CollectionRun
from app.schemas import OpenWebRunCreateRequest, SourceRefreshRunResponse
from app.services.collection.open_web import BRAVE_DISCOVERY_PROVIDER
from app.services.open_web_runs import (
    OpenWebRunError,
    create_open_web_run,
    request_open_web_stop,
    run_open_web_discovery,
)


router = APIRouter(prefix="/api/v1/open-web-runs", tags=["open web discovery"])


def _background_run(run_id: int) -> None:
    with SessionLocal() as session:
        run_open_web_discovery(session, run_id, get_settings())


@router.post("", response_model=SourceRefreshRunResponse)
def create_run(
    payload: OpenWebRunCreateRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> SourceRefreshRunResponse:
    try:
        run, created = create_open_web_run(
            session, get_settings(), target_signal_count=payload.target_signal_count,
            brave_max_requests=payload.brave_max_requests,
        )
    except OpenWebRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        background_tasks.add_task(_background_run, run.id)
    return source_run_response(run)


@router.get("/latest", response_model=Optional[SourceRefreshRunResponse])
def latest_run(session: Session = Depends(get_db)) -> Optional[SourceRefreshRunResponse]:
    run = session.scalar(select(CollectionRun).where(
        CollectionRun.source == BRAVE_DISCOVERY_PROVIDER,
        CollectionRun.scope_type == "department",
        CollectionRun.scope_value == "94",
    ).order_by(CollectionRun.id.desc()))
    return source_run_response(run) if run is not None else None


@router.get("/{run_id}", response_model=SourceRefreshRunResponse)
def get_run(run_id: int, session: Session = Depends(get_db)) -> SourceRefreshRunResponse:
    run = session.get(CollectionRun, run_id)
    if run is None or run.source != BRAVE_DISCOVERY_PROVIDER:
        raise HTTPException(status_code=404, detail="Run Open Web introuvable.")
    return source_run_response(run)


@router.post("/{run_id}/stop", response_model=SourceRefreshRunResponse)
def stop_run(run_id: int, session: Session = Depends(get_db)) -> SourceRefreshRunResponse:
    run = session.get(CollectionRun, run_id)
    if run is None or run.source != BRAVE_DISCOVERY_PROVIDER:
        raise HTTPException(status_code=404, detail="Run Open Web introuvable.")
    try:
        return source_run_response(request_open_web_stop(session, run))
    except OpenWebRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
