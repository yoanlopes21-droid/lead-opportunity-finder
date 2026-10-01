"""Manual, bounded non-FT discovery and paginated company-seed review."""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.source_boards import source_run_response
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import CollectionRun, CompanyDiscoverySeed
from app.schemas import SourceRefreshRunResponse
from app.services.non_ft_runs import RUN_SOURCE, create_non_ft_run, run_non_ft_discovery


router = APIRouter(prefix="/api/v1/non-ft-discovery", tags=["non FT discovery"])


class NonFtRunCreate(BaseModel):
    target_leads: int = Field(default=25, ge=1, le=30)
    brave_max_requests: int = Field(default=10, ge=0, le=40)


def _background(run_id: int) -> None:
    with SessionLocal() as session:
        run_non_ft_discovery(session, run_id, get_settings())


@router.post("/runs", response_model=SourceRefreshRunResponse)
def create_run(payload: NonFtRunCreate, background_tasks: BackgroundTasks,
               session: Session = Depends(get_db)) -> SourceRefreshRunResponse:
    try:
        run, created = create_non_ft_run(session, get_settings(), **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if created:
        background_tasks.add_task(_background, run.id)
    return source_run_response(run)


@router.get("/runs/latest", response_model=Optional[SourceRefreshRunResponse])
def latest_run(session: Session = Depends(get_db)) -> Optional[SourceRefreshRunResponse]:
    run = session.scalar(select(CollectionRun).where(CollectionRun.source == RUN_SOURCE).order_by(CollectionRun.id.desc()))
    return source_run_response(run) if run else None


@router.get("/runs/{run_id}", response_model=SourceRefreshRunResponse)
def get_run(run_id: int, session: Session = Depends(get_db)) -> SourceRefreshRunResponse:
    run = session.get(CollectionRun, run_id)
    if run is None or run.source != RUN_SOURCE:
        raise HTTPException(404, detail="Run introuvable")
    return source_run_response(run)


@router.post("/runs/{run_id}/stop", response_model=SourceRefreshRunResponse)
def stop_run(run_id: int, session: Session = Depends(get_db)) -> SourceRefreshRunResponse:
    run = session.get(CollectionRun, run_id)
    if run is None or run.source != RUN_SOURCE:
        raise HTTPException(404, detail="Run introuvable")
    if run.status not in {"queued", "running"}:
        raise HTTPException(409, detail="Run déjà terminé")
    run.stop_requested = True
    session.commit()
    return source_run_response(run)


@router.get("/seeds")
def list_seeds(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
               session: Session = Depends(get_db)) -> dict:
    total = session.scalar(select(func.count()).select_from(CompanyDiscoverySeed)) or 0
    rows = session.scalars(select(CompanyDiscoverySeed).order_by(CompanyDiscoverySeed.id).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit, "items": [
        {"id": row.id, "company_name": row.company_name, "commune": row.commune,
         "siren": row.siren, "siret": row.siret, "status": row.status,
         "site_status": row.site_status, "official_site_url": row.official_site_url,
         "last_result": row.last_result, "ats_candidates": row.ats_candidates}
        for row in rows]}
