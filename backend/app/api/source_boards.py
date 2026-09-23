"""Manual configuration and refresh endpoints for public employer ATS boards."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import CollectionRun, JobSourceBoard
from app.schemas import (
    JobSourceBoardCreateRequest,
    JobSourceBoardResponse,
    JobSourceBoardUpdateRequest,
    SourceRefreshRunResponse,
)
from app.services.job_source_boards import (
    BoardValidationError,
    DuplicateBoardError,
    create_board,
    create_board_refresh_run,
    delete_board,
    run_board_refresh,
    update_board,
)


router = APIRouter(prefix="/api/v1/job-source-boards", tags=["job source boards"])


def source_run_response(run: CollectionRun) -> SourceRefreshRunResponse:
    return SourceRefreshRunResponse(
        id=run.id, source=run.source, scope_type=run.scope_type, scope_value=run.scope_value,
        status=run.status, started_at=run.started_at, finished_at=run.finished_at,
        offers_received=run.offers_received, offers_new=run.offers_new,
        offers_updated=run.offers_updated, offers_unchanged=run.offers_unchanged,
        offers_skipped=run.offers_skipped, offers_deactivated=run.offers_deactivated,
        pages_processed=run.pages_processed, signals_found=run.signals_found,
        signals_promoted=run.signals_promoted, brave_requests_used=run.brave_requests_used,
        target_signal_count=run.target_signal_count, brave_hard_cap=run.brave_hard_cap,
        stop_requested=run.stop_requested, completion_reason=run.completion_reason,
        error_summary=run.error_summary,
    )


def _board_response(board: JobSourceBoard, session: Session) -> JobSourceBoardResponse:
    run = session.get(CollectionRun, board.last_run_id) if board.last_run_id else None
    values = JobSourceBoardResponse.model_validate(board, from_attributes=True).model_dump()
    values.update(
        last_offers_received=run.offers_received if run else 0,
        last_offers_new=run.offers_new if run else 0,
        last_offers_updated=run.offers_updated if run else 0,
        last_offers_deactivated=run.offers_deactivated if run else 0,
        last_duration_seconds=(
            max(0.0, (run.finished_at - run.started_at).total_seconds())
            if run and run.finished_at else None
        ),
    )
    return JobSourceBoardResponse(**values)


def _background_refresh(board_id: int, run_id: int) -> None:
    with SessionLocal() as session:
        run_board_refresh(session, board_id, run_id)


@router.get("", response_model=list[JobSourceBoardResponse])
def list_boards(session: Session = Depends(get_db)) -> list[JobSourceBoardResponse]:
    boards = session.scalars(select(JobSourceBoard).order_by(JobSourceBoard.id)).all()
    return [_board_response(board, session) for board in boards]


@router.post("", response_model=JobSourceBoardResponse, status_code=201)
def add_board(
    payload: JobSourceBoardCreateRequest, session: Session = Depends(get_db)
) -> JobSourceBoardResponse:
    try:
        board = create_board(session, **payload.model_dump())
    except DuplicateBoardError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BoardValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_response(board, session)


@router.patch("/{board_id}", response_model=JobSourceBoardResponse)
def edit_board(
    board_id: int, payload: JobSourceBoardUpdateRequest, session: Session = Depends(get_db)
) -> JobSourceBoardResponse:
    board = session.get(JobSourceBoard, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Board ATS introuvable.")
    try:
        board = update_board(session, board, **payload.model_dump())
    except BoardValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_response(board, session)


@router.delete("/{board_id}", status_code=204)
def remove_board(board_id: int, session: Session = Depends(get_db)) -> Response:
    board = session.get(JobSourceBoard, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Board ATS introuvable.")
    try:
        delete_board(session, board)
    except BoardValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/{board_id}/refresh", response_model=SourceRefreshRunResponse)
def refresh_board(
    board_id: int, background_tasks: BackgroundTasks, session: Session = Depends(get_db)
) -> SourceRefreshRunResponse:
    board = session.get(JobSourceBoard, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Board ATS introuvable.")
    try:
        run, created = create_board_refresh_run(session, board)
    except BoardValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        background_tasks.add_task(_background_refresh, board.id, run.id)
    return source_run_response(run)


@router.get("/runs/{run_id}", response_model=SourceRefreshRunResponse)
def get_board_run(run_id: int, session: Session = Depends(get_db)) -> SourceRefreshRunResponse:
    run = session.get(CollectionRun, run_id)
    if run is None or run.scope_type != "provider_board":
        raise HTTPException(status_code=404, detail="Rafraîchissement ATS introuvable.")
    return source_run_response(run)
