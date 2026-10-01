"""Review and conservative promotion of Open Web recruitment signals."""

from __future__ import annotations

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CollectionRun, ObservedJobOffer, RecruitmentSignal, VerifiedWebsiteRecord
from app.services.collection.jobposting import parse_jobpostings
from app.services.contactability.providers.official_web.fetcher import SecureWebFetcher
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy
from app.services.contactability.providers.official_web.discovery import registrable_domain
from app.services.opportunities.company import normalize_company_key
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
PROMOTABLE_SOURCES = {"employer_career_site"}


class SignalPromotionError(ValueError):
    pass


@dataclass(frozen=True)
class SignalPromotionAssessment:
    is_promotable: bool
    page_type: str
    page_type_label: str
    blockers: tuple[str, ...]
    geography: OfferGeography


def assess_signal_promotion(signal: RecruitmentSignal, session: Session | None = None) -> SignalPromotionAssessment:
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
        blockers.append("Page tierce : seule une offre sur domaine employeur vérifié peut être promue")
    elif session is None or not parsed.hostname or not signal.company_name or not session.scalar(
        select(VerifiedWebsiteRecord.id).where(
            VerifiedWebsiteRecord.company_key == normalize_company_key(signal.company_name),
            VerifiedWebsiteRecord.registrable_domain == registrable_domain(signal.source_url),
            VerifiedWebsiteRecord.status == "high_confidence",
            VerifiedWebsiteRecord.fresh_until >= datetime.now(timezone.utc),
        )
    ):
        blockers.append("Domaine officiel non vérifié pour cet employeur")
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
    assessment = assess_signal_promotion(signal, session)
    if not assessment.is_promotable:
        signal.status = "review_needed"
        signal.reviewed_at = datetime.now(timezone.utc)
        session.commit()
        raise SignalPromotionError(
            "Promotion refusée : " + ", ".join(assessment.blockers) + "."
        )

    fetcher = SecureWebFetcher()
    policy = RobotsTxtPolicy(fetcher)
    fetcher.set_robots_checker(policy.allowed)
    try:
        page = fetcher.fetch(signal.source_url, initial=False)
        domain = urlparse(signal.source_url).hostname or ""
        confirmed = parse_jobpostings(page.html, page.final_url, signal.company_name, domain)
    except Exception as exc:
        raise SignalPromotionError("Page employeur impossible à vérifier") from exc
    snapshot = next((row for row in confirmed if normalize_company_key(row.title) == normalize_company_key(signal.job_title)), None)
    if snapshot is None:
        raise SignalPromotionError("Offre JobPosting correspondante absente ou insuffisante")
    run = create_collection_run(session, BRAVE_DISCOVERY_PROVIDER, "signal", str(signal.id))
    result = upsert_offer(session, run, OfferSnapshot(
        **{**snapshot.__dict__, "discovery_provider": BRAVE_DISCOVERY_PROVIDER,
           "recruitment_signal_id": signal.id}
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
