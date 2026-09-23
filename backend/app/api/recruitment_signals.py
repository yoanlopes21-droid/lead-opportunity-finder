"""Lightweight review queue for recruitment signals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RecruitmentSignal
from app.schemas import (
    RecruitmentSignalListResponse,
    RecruitmentSignalResponse,
    SignalActionResponse,
)
from app.services.collection.open_web import normalize_index_text
from app.services.recruitment_signals import (
    SignalPromotionError,
    assess_signal_promotion,
    dismiss_signal,
    promote_signal,
)


router = APIRouter(prefix="/api/v1/recruitment-signals", tags=["recruitment signals"])


def _response(signal: RecruitmentSignal) -> RecruitmentSignalResponse:
    assessment = assess_signal_promotion(signal)
    return RecruitmentSignalResponse(
        id=signal.id, discovery_provider=signal.discovery_provider, source=signal.source,
        source_url=signal.source_url, domain=signal.domain, page_type=assessment.page_type,
        page_type_label=assessment.page_type_label, title=normalize_index_text(signal.title),
        snippet=normalize_index_text(signal.snippet), company_name=signal.company_name,
        job_title=signal.job_title,
        location_label=signal.location_label, commune=signal.commune,
        department_code=signal.department_code, published_at=signal.published_at,
        confidence=signal.confidence, detection_reason=signal.detection_reason,
        extraction=signal.extraction or {}, status=signal.status,
        promoted_offer_id=signal.promoted_offer_id, first_seen_at=signal.first_seen_at,
        last_seen_at=signal.last_seen_at, observation_count=signal.observation_count,
        is_promotable=assessment.is_promotable,
        promotion_blockers=list(assessment.blockers),
    )


@router.get("", response_model=RecruitmentSignalListResponse)
def list_signals(
    status: str = "new,review_needed",
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_db),
) -> RecruitmentSignalListResponse:
    statuses = tuple(value.strip() for value in status.split(",") if value.strip())
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(status_code=422, detail="Pagination invalide.")
    statement = select(RecruitmentSignal)
    count_statement = select(func.count()).select_from(RecruitmentSignal)
    if statuses:
        statement = statement.where(RecruitmentSignal.status.in_(statuses))
        count_statement = count_statement.where(RecruitmentSignal.status.in_(statuses))
    items = session.scalars(
        statement.order_by(RecruitmentSignal.last_seen_at.desc(), RecruitmentSignal.id.desc())
        .offset(offset).limit(limit)
    ).all()
    counts = dict(session.execute(
        select(RecruitmentSignal.status, func.count()).group_by(RecruitmentSignal.status)
    ).all())
    return RecruitmentSignalListResponse(
        items=[_response(item) for item in items],
        total=int(session.scalar(count_statement) or 0),
        new_count=int(counts.get("new", 0)),
        review_needed_count=int(counts.get("review_needed", 0)),
    )


@router.post("/{signal_id}/promote", response_model=SignalActionResponse)
def promote(signal_id: int, session: Session = Depends(get_db)) -> SignalActionResponse:
    signal = session.get(RecruitmentSignal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal introuvable.")
    try:
        offer = promote_signal(session, signal)
    except SignalPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.refresh(signal)
    return SignalActionResponse(
        signal=_response(signal), message=f"Signal promu vers l’offre nº {offer.id}.",
    )


@router.post("/{signal_id}/dismiss", response_model=SignalActionResponse)
def dismiss(signal_id: int, session: Session = Depends(get_db)) -> SignalActionResponse:
    signal = session.get(RecruitmentSignal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal introuvable.")
    try:
        signal = dismiss_signal(session, signal)
    except SignalPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SignalActionResponse(signal=_response(signal), message="Signal ignoré.")
