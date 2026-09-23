"""Persistent configuration and independent refreshes for public ATS boards."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CollectionRun, JobSourceBoard, ObservedJobOffer
from app.services.collection.greenhouse import (
    EMPLOYER_CAREER_SOURCE,
    GreenhouseBoard,
    GreenhouseJobBoardProvider,
)
from app.services.collection.lever import LeverBoard, LeverJobBoardProvider
from app.services.collection.providers import JobOfferProviderCollector
from app.services.persistence.offers import CollectionRunStatus, create_collection_run


SUPPORTED_BOARD_PROVIDERS = ("greenhouse", "lever")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class BoardValidationError(ValueError):
    pass


class DuplicateBoardError(ValueError):
    pass


def normalize_board_identifier(provider_id: str, value: str) -> str:
    provider = provider_id.strip().casefold()
    raw = value.strip()
    if provider not in SUPPORTED_BOARD_PROVIDERS:
        raise BoardValidationError("Provider ATS non pris en charge.")
    if not raw:
        raise BoardValidationError("Un identifiant ou une URL de board est requis.")
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").casefold()
        parts = [part for part in parsed.path.split("/") if part]
        if provider == "greenhouse":
            allowed = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"}
            if host not in allowed:
                raise BoardValidationError("Cette URL n’est pas une URL publique Greenhouse reconnue.")
            if host == "boards-api.greenhouse.io" and parts[:1] == ["v1"]:
                parts = parts[2:] if len(parts) > 1 and parts[1] == "boards" else []
        else:
            allowed = {"jobs.lever.co", "api.lever.co"}
            if host not in allowed:
                raise BoardValidationError("Cette URL n’est pas une URL publique Lever reconnue.")
            if host == "api.lever.co" and parts[:2] == ["v0", "postings"]:
                parts = parts[2:]
        if not parts:
            raise BoardValidationError("L’URL ne contient pas d’identifiant de board.")
        raw = parts[0]
    if not _IDENTIFIER.fullmatch(raw):
        raise BoardValidationError("L’identifiant du board contient des caractères non autorisés.")
    return raw.casefold()


def create_board(
    session: Session,
    *,
    provider_id: str,
    display_name: str,
    board_identifier: str,
    company_name_hint: str,
    enabled: bool = True,
) -> JobSourceBoard:
    provider = provider_id.strip().casefold()
    identifier = normalize_board_identifier(provider, board_identifier)
    display = " ".join(display_name.split()).strip()
    company = " ".join(company_name_hint.split()).strip()
    if not display or not company:
        raise BoardValidationError("Le nom affiché et l’entreprise sont requis.")
    board = JobSourceBoard(
        provider_id=provider,
        display_name=display[:255],
        board_identifier=identifier,
        company_name_hint=company[:500],
        enabled=enabled,
    )
    session.add(board)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateBoardError("Ce board ATS est déjà configuré.") from exc
    session.refresh(board)
    return board


def update_board(
    session: Session,
    board: JobSourceBoard,
    *,
    display_name: Optional[str] = None,
    company_name_hint: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> JobSourceBoard:
    if display_name is not None:
        value = " ".join(display_name.split()).strip()
        if not value:
            raise BoardValidationError("Le nom affiché ne peut pas être vide.")
        board.display_name = value[:255]
    if company_name_hint is not None:
        value = " ".join(company_name_hint.split()).strip()
        if not value:
            raise BoardValidationError("L’entreprise ne peut pas être vide.")
        board.company_name_hint = value[:500]
    if enabled is not None:
        board.enabled = enabled
    session.commit()
    session.refresh(board)
    return board


def delete_board(session: Session, board: JobSourceBoard) -> None:
    if board.last_refresh_status in {CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING}:
        raise BoardValidationError("Un board en cours de rafraîchissement ne peut pas être supprimé.")
    session.delete(board)
    session.commit()


def create_board_refresh_run(session: Session, board: JobSourceBoard) -> tuple[CollectionRun, bool]:
    if not board.enabled:
        raise BoardValidationError("Activez ce board avant de le rafraîchir.")
    scope = f"{board.provider_id}:{board.board_identifier}"
    active = session.scalar(
        select(CollectionRun).where(
            CollectionRun.source == EMPLOYER_CAREER_SOURCE,
            CollectionRun.scope_type == "provider_board",
            CollectionRun.scope_value == scope,
            CollectionRun.status.in_((CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)),
        ).order_by(CollectionRun.id.desc())
    )
    if active is not None:
        return active, False
    run = create_collection_run(
        session, EMPLOYER_CAREER_SOURCE, "provider_board", scope
    )
    run.status = CollectionRunStatus.QUEUED
    board.last_refresh_status = CollectionRunStatus.QUEUED
    board.last_error = None
    board.last_run_id = run.id
    session.commit()
    return run, True


def run_board_refresh(session: Session, board_id: int, run_id: int) -> None:
    board = session.get(JobSourceBoard, board_id)
    run = session.get(CollectionRun, run_id)
    if board is None or run is None or run.status not in {
        CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING,
    }:
        return
    try:
        provider = build_board_provider(board)
        JobOfferProviderCollector(provider).collect(session, run=run)
    except Exception as exc:
        session.rollback()
        board = session.get(JobSourceBoard, board_id)
        failed = session.get(CollectionRun, run_id)
        if board is not None:
            board.last_refresh_at = datetime.now(timezone.utc)
            board.last_refresh_status = failed.status if failed is not None else CollectionRunStatus.FAILED
            board.last_error = (str(exc).strip() or "Le rafraîchissement ATS a échoué.")[:1000]
            session.commit()
        return
    board = session.get(JobSourceBoard, board_id)
    completed = session.get(CollectionRun, run_id)
    if board is None or completed is None:
        return
    board.last_refresh_at = completed.finished_at or datetime.now(timezone.utc)
    board.last_refresh_status = completed.status
    board.last_error = completed.error_summary
    board.last_run_id = completed.id
    board.active_offer_count = int(session.scalar(
        select(func.count()).select_from(ObservedJobOffer).where(
            ObservedJobOffer.origin == f"{board.provider_id}:{board.board_identifier}",
            ObservedJobOffer.is_active.is_(True),
        )
    ) or 0)
    session.commit()


def build_board_provider(board: JobSourceBoard):
    if board.provider_id == "greenhouse":
        return GreenhouseJobBoardProvider(GreenhouseBoard(
            board.board_identifier, board.company_name_hint, discovery_provider="greenhouse"
        ))
    if board.provider_id == "lever":
        return LeverJobBoardProvider(LeverBoard(
            board.board_identifier, board.company_name_hint, discovery_provider="lever"
        ))
    raise BoardValidationError("Provider ATS non pris en charge.")


def recover_orphaned_board_runs(session: Session) -> int:
    runs = session.scalars(select(CollectionRun).where(
        CollectionRun.source == EMPLOYER_CAREER_SOURCE,
        CollectionRun.scope_type == "provider_board",
        CollectionRun.status.in_((CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)),
    )).all()
    now = datetime.now(timezone.utc)
    for run in runs:
        run.status = CollectionRunStatus.FAILED
        run.finished_at = now
        run.error_summary = "Le rafraîchissement ATS a été interrompu par l’arrêt du serveur."
        board = session.scalar(select(JobSourceBoard).where(
            JobSourceBoard.last_run_id == run.id
        ))
        if board is not None:
            board.last_refresh_at = now
            board.last_refresh_status = CollectionRunStatus.FAILED
            board.last_error = run.error_summary
    if runs:
        session.commit()
    return len(runs)
