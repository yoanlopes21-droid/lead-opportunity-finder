"""Review and conservative promotion of Open Web recruitment signals."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import CollectionRun, ObservedJobOffer, RecruitmentSignal
from app.services.collection.open_web import (
    BRAVE_DISCOVERY_PROVIDER,
    PAGE_TYPE_LABELS,
    OfferGeography,
    RecruitmentPageType,
    classify_page_type,
    extract_offer_geography,
    normalize_index_text,
)
from app.services.persistence.offers import (
    OfferSnapshot,
    complete_collection_run,
    create_collection_run,
    upsert_offer,
)


SIGNAL_STATUSES = ("new", "review_needed", "promoted", "dismissed")
PROMOTABLE_SOURCES = {
    "linkedin", "indeed", "hellowork", "leboncoin", "welcome_to_the_jungle",
    "employer_career_site", "cadremploi", "directemploi", "france_travail",
    "glassdoor", "jooble", "meteojob",
}


class SignalPromotionError(ValueError):
    pass


@dataclass(frozen=True)
class SignalPromotionAssessment:
    is_promotable: bool
    page_type: str
    page_type_label: str
    blockers: tuple[str, ...]
    geography: OfferGeography


def assess_signal_promotion(signal: RecruitmentSignal) -> SignalPromotionAssessment:
    title = normalize_index_text(signal.title)
    snippet = normalize_index_text(signal.snippet)
    page_type = classify_page_type(signal.source_url, source=signal.source, title=title)
    geography = extract_offer_geography(page_type, title, snippet)
    blockers = []
    if page_type != RecruitmentPageType.INDIVIDUAL_JOB_OFFER:
        blockers.append("Offre individuelle non identifiée")
    if not _text(signal.company_name):
        blockers.append("Entreprise manquante")
    if not _text(signal.job_title):
        blockers.append("Intitulé manquant")
    parsed = urlparse(signal.source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        blockers.append("URL preuve invalide")
    if signal.source not in PROMOTABLE_SOURCES:
        blockers.append("Source non reconnue")
    if geography.department_code != "94":
        blockers.append("Localisation 94 non prouvée")
    return SignalPromotionAssessment(
        is_promotable=not blockers,
        page_type=page_type,
        page_type_label=PAGE_TYPE_LABELS[page_type],
        blockers=tuple(blockers),
        geography=geography,
    )


def promote_signal(session: Session, signal: RecruitmentSignal) -> ObservedJobOffer:
    if signal.status == "promoted" and signal.promoted_offer_id is not None:
        existing = session.get(ObservedJobOffer, signal.promoted_offer_id)
        if existing is not None:
            return existing
    if signal.status == "dismissed":
        raise SignalPromotionError("Un signal ignoré ne peut pas être promu sans nouvelle revue.")
    assessment = assess_signal_promotion(signal)
    if not assessment.is_promotable:
        signal.status = "review_needed"
        signal.reviewed_at = datetime.now(timezone.utc)
        session.commit()
        raise SignalPromotionError(
            "Promotion refusée : " + ", ".join(assessment.blockers) + "."
        )

    run = create_collection_run(session, BRAVE_DISCOVERY_PROVIDER, "signal", str(signal.id))
    source_offer_id = "web:" + hashlib.sha256(signal.source_url.encode("utf-8")).hexdigest()[:32]
    result = upsert_offer(session, run, OfferSnapshot(
        source=signal.source,
        source_offer_id=source_offer_id,
        title=signal.job_title.strip(),
        description=signal.snippet,
        company_name=signal.company_name.strip(),
        location_label=assessment.geography.location_label,
        commune=assessment.geography.commune,
        department_code="94",
        created_at=signal.published_at,
        source_url=signal.source_url,
        discovery_provider=BRAVE_DISCOVERY_PROVIDER,
        origin=f"recruitment_signal:{signal.id}",
        recruitment_signal_id=signal.id,
    ))
    run.signals_promoted = 1
    complete_collection_run(session, run, full_scope_completed=True, deactivate_unseen=False)
    signal.status = "promoted"
    signal.promoted_offer_id = result.offer.id
    signal.reviewed_at = datetime.now(timezone.utc)
    if signal.last_seen_run_id is not None:
        discovery_run = session.get(CollectionRun, signal.last_seen_run_id)
        if discovery_run is not None:
            discovery_run.signals_promoted += 1
    session.commit()
    return result.offer


def dismiss_signal(session: Session, signal: RecruitmentSignal) -> RecruitmentSignal:
    if signal.status == "promoted":
        raise SignalPromotionError("Une offre déjà promue ne peut pas être ignorée.")
    signal.status = "dismissed"
    signal.reviewed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(signal)
    return signal


def _text(value: str | None) -> str | None:
    compact = " ".join(value.split()).strip() if value else ""
    return compact or None
