"""HTTP surface for deliberate, persistent commercial searches."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import BytesIO

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine, get_db
from app.models import SearchRun
from app.schemas import (
    CommercialLeadListResponse, CommercialLeadResponse, SearchRunCreateRequest, SearchRunResponse,
)
from app.services.search_runs import (
    OfficialWebSearchEnricher, SearchRunOrchestrator, ensure_search_run_schema,
)
from app.services.commercial_leads.excel_export import (
    XLSX_MEDIA_TYPE,
    CommercialExcelItem,
    build_commercial_xlsx,
    export_filename,
)


router = APIRouter(prefix="/api/v1/search-runs", tags=["search runs"])


def _response(orchestrator: SearchRunOrchestrator, session: Session, run_id: int) -> SearchRunResponse:
    progress = orchestrator.progress(session, run_id)
    run = session.get(SearchRun, run_id)
    return SearchRunResponse(**progress.__dict__, stop_requested=run.stop_requested,
                             configuration_fingerprint=run.configuration_fingerprint)


def _background_resume(run_id: int) -> None:
    # A background task owns a separate session; the request session is closed
    # before execution and must not be retained by a worker.
    with SessionLocal() as session:
        SearchRunOrchestrator(OfficialWebSearchEnricher()).resume(session, run_id)


@router.post("", response_model=SearchRunResponse, status_code=201)
def create_search_run(
    request: SearchRunCreateRequest, background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> SearchRunResponse:
    ensure_search_run_schema(engine)
    orchestrator = SearchRunOrchestrator()
    run = orchestrator.create(session, department=request.department,
                              requested_actionable_leads=request.requested_actionable_leads,
                              brave_hard_cap=request.brave_hard_cap)
    background_tasks.add_task(_background_resume, run.id)
    return _response(orchestrator, session, run.id)


@router.get("/{run_id}", response_model=SearchRunResponse)
def get_search_run(run_id: int, session: Session = Depends(get_db)) -> SearchRunResponse:
    try:
        return _response(SearchRunOrchestrator(), session, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{run_id}/stop", response_model=SearchRunResponse)
def stop_search_run(run_id: int, session: Session = Depends(get_db)) -> SearchRunResponse:
    orchestrator = SearchRunOrchestrator()
    try:
        orchestrator.request_stop(session, run_id)
        return _response(orchestrator, session, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{run_id}/resume", response_model=SearchRunResponse)
def resume_search_run(run_id: int, background_tasks: BackgroundTasks,
                      session: Session = Depends(get_db)) -> SearchRunResponse:
    try:
        SearchRunOrchestrator()._get(session, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(_background_resume, run_id)
    return _response(SearchRunOrchestrator(), session, run_id)


@router.get("/{run_id}/results", response_model=CommercialLeadListResponse)
def search_run_results(run_id: int, session: Session = Depends(get_db)) -> CommercialLeadListResponse:
    orchestrator = SearchRunOrchestrator()
    try:
        leads = orchestrator.results(session, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CommercialLeadListResponse(
        items=[CommercialLeadResponse.from_lead(lead) for lead in leads],
        total=len(leads), offset=0, limit=len(leads),
    )


@router.get("/{run_id}/export.xlsx")
def export_search_run_results(
    run_id: int, session: Session = Depends(get_db)
) -> StreamingResponse:
    """Export exactly the currently usable results returned by this SearchRun."""
    generated_at = datetime.now(timezone.utc)
    try:
        leads = SearchRunOrchestrator().results(session, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    content = build_commercial_xlsx(
        tuple(CommercialExcelItem(lead=lead) for lead in leads),
        generated_at=generated_at,
    )
    filename = export_filename(generated_at=generated_at, suffix=f"recherche-{run_id}")
    return StreamingResponse(
        BytesIO(content), media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{run_id}/events")
async def search_run_events(run_id: int) -> StreamingResponse:
    """SSE progress snapshots; clients reconnect with ordinary GET after completion."""
    async def stream():
        last = None
        while True:
            with SessionLocal() as session:
                try:
                    progress = SearchRunOrchestrator().progress(session, run_id)
                except ValueError:
                    yield "event: error\ndata: {\"detail\": \"search run does not exist\"}\n\n"
                    return
                payload = json.dumps(progress.__dict__, default=lambda value: value.isoformat())
                if payload != last:
                    yield f"event: progress\ndata: {payload}\n\n"
                    last = payload
                if progress.status in {"completed", "stopped", "failed"}:
                    return
            await asyncio.sleep(0.5)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
