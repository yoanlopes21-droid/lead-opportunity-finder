"""One bounded manual source run over boards, verified sites and company seeds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BraveUsageEvent, CollectionRun, CompanyDiscoverySeed, JobSourceBoard, VerifiedWebsiteRecord, CompanyEnrichment, CommercialExclusion, CommercialRelationship, JobDiscoveryQueryCache
from app.services.brave_usage import BraveUsageService
from app.services.collection.open_web import CachedSearchClient, _signal_from_result
from app.services.company_first import import_dinum_seed_page, inspect_seed, eligible_seed
from app.services.collection.official_site import OfficialSiteJobProvider
from app.services.collection.providers import JobOfferProviderCollector
from app.services.contactability.providers.official_web.brave_client import BraveSearchClient, BraveSearchError
from app.services.contactability.contracts import ContactTarget, ContactScope
from app.services.contactability.providers.official_web.contracts import WebsiteSeed, WebsiteVerificationStatus
from app.services.contactability.providers.official_web.discovery import discover_website_candidates, classify_domain, registrable_domain
from app.services.contactability.providers.official_web.fetcher import SecureWebFetcher
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy
from app.services.contactability.providers.official_web.verification import verify_website_candidates
from app.services.contactability.providers.official_web.persistence import OfficialWebRepository
from app.services.contactability.providers.official_web.provider import official_web_target_fingerprint
from app.services.job_source_boards import create_board_refresh_run, run_board_refresh
from app.services.open_web_runs import _policy
from app.services.persistence.offers import create_collection_run, complete_collection_run, upsert_recruitment_signal


RUN_SOURCE = "non_ft_discovery"


def recover_orphaned_non_ft_runs(session: Session) -> int:
    runs = session.scalars(select(CollectionRun).where(
        CollectionRun.source == RUN_SOURCE, CollectionRun.status.in_(("queued", "running")),
    )).all()
    for run in runs:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.error_summary = "Découverte interrompue au redémarrage ; relancez pour reprendre les seeds."
    if runs:
        session.commit()
    return len(runs)


def create_non_ft_run(session: Session, settings: Settings, *, target_leads: int = 25,
                      brave_max_requests: int = 10) -> tuple[CollectionRun, bool]:
    if not 1 <= target_leads <= 30 or not 0 <= brave_max_requests <= 40:
        raise ValueError("Cible 1–30 et budget Brave 0–40 requis.")
    active = session.scalar(select(CollectionRun).where(
        CollectionRun.source == RUN_SOURCE,
        CollectionRun.status.in_(("queued", "running")),
    ).order_by(CollectionRun.id.desc()))
    if active:
        return active, False
    cap = 0
    if settings.brave_search_api_key and brave_max_requests:
        usage = BraveUsageService(session, _policy(settings)).snapshot()
        cap = min(brave_max_requests, max(0, usage.monthly_remaining - settings.brave_search_monthly_reserve))
    run = create_collection_run(session, RUN_SOURCE, "department", "94")
    run.status = "queued"
    run.target_signal_count = target_leads
    run.brave_hard_cap = cap
    session.commit()
    return run, True


def run_non_ft_discovery(session: Session, run_id: int, settings: Settings) -> None:
    run = session.get(CollectionRun, run_id)
    if run is None or run.status not in {"queued", "running"}:
        return
    run.status = "running"
    session.commit()
    before = _usage(session, run_id)
    try:
        for board in session.scalars(select(JobSourceBoard).where(JobSourceBoard.enabled.is_(True)).order_by(JobSourceBoard.id).limit(30)):
            if _stop(session, run):
                break
            child, created = create_board_refresh_run(session, board)
            if created:
                run_board_refresh(session, board.id, child.id)
            session.refresh(child)
            if child.status == "completed":
                run.offers_new += child.offers_new
                run.offers_updated += child.offers_updated
                run.pages_processed += 1
                session.commit()
            if run.offers_new >= (run.target_signal_count or 25):
                break
        if not _stop(session, run) and run.offers_new < (run.target_signal_count or 25):
            _inspect_known_sites(session, run)
        if not _stop(session, run) and run.offers_new < (run.target_signal_count or 25):
            _inspect_seeds(session, run)
        if not _stop(session, run) and run.offers_new < (run.target_signal_count or 25):
            _search_unresolved_seeds(session, run, settings)
        run.status = "completed"
        run.completion_reason = "stopped_by_user" if run.stop_requested else "target_reached" if run.offers_new >= (run.target_signal_count or 25) else "candidates_exhausted"
    except Exception as exc:
        session.rollback()
        run = session.get(CollectionRun, run_id)
        run.status = "failed"
        run.error_summary = (str(exc).strip() or "Découverte hors FT interrompue")[:1000]
    finally:
        run.brave_requests_used = max(0, _usage(session, run_id) - before)
        run.finished_at = datetime.now(timezone.utc)
        session.commit()


def _inspect_known_sites(session: Session, run: CollectionRun) -> None:
    now = datetime.now(timezone.utc)
    sites = session.scalars(select(VerifiedWebsiteRecord).where(
        VerifiedWebsiteRecord.status == "high_confidence",
        VerifiedWebsiteRecord.target_scope == "company",
        VerifiedWebsiteRecord.fresh_until >= now,
    ).order_by(VerifiedWebsiteRecord.verified_at.desc()).limit(20)).all()
    seen_domains = set()
    for site in sites:
        if _stop(session, run) or run.offers_new >= (run.target_signal_count or 25):
            return
        if site.registrable_domain in seen_domains:
            continue
        seen_domains.add(site.registrable_domain)
        parsed = urlsplit(site.canonical_url)
        if parsed.scheme != "https" or classify_domain(parsed.hostname or "")[1]:
            continue
        if session.scalar(select(CommercialExclusion.id).where(CommercialExclusion.company_key == site.company_key)):
            continue
        if session.scalar(select(CommercialRelationship.id).where(
            CommercialRelationship.company_key == site.company_key,
            CommercialRelationship.is_active.is_(True),
            CommercialRelationship.status == "client",
        )):
            continue
        name = session.scalar(select(CompanyEnrichment.source_company_name).where(
            CompanyEnrichment.company_key == site.company_key,
        ).limit(1))
        if not name:
            continue
        try:
            provider = OfficialSiteJobProvider(name, site.canonical_url)
            child = JobOfferProviderCollector(provider).collect(session)
            run.offers_new += child.offers_new
            run.offers_updated += child.offers_updated
            run.pages_processed += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            run = session.get(CollectionRun, run.id)
            run.error_summary = f"Site officiel : {str(exc)[:150]}"
            session.commit()


def _inspect_seeds(session: Session, run: CollectionRun) -> None:
    # One public DINUM page per run advances the company universe without a bulk scan.
    try:
        import_dinum_seed_page(session)
    except Exception as exc:
        run.error_summary = f"DINUM indisponible : {str(exc)[:150]}"
        session.commit()
    now = datetime.now(timezone.utc)
    seeds = session.scalars(select(CompanyDiscoverySeed).where(
        CompanyDiscoverySeed.department_code == "94",
        (CompanyDiscoverySeed.next_inspection_at.is_(None) | (CompanyDiscoverySeed.next_inspection_at <= now)),
    ).order_by(CompanyDiscoverySeed.official_site_url.is_(None), CompanyDiscoverySeed.id).limit(100)).all()
    seen_sirens = set()
    for seed in seeds:
        if seed.siren in seen_sirens:
            continue
        seen_sirens.add(seed.siren)
        if len(seen_sirens) > 20:
            return
        if _stop(session, run) or run.offers_new >= (run.target_signal_count or 25):
            return
        try:
            new_offers, _ = inspect_seed(session, seed)
            run.offers_new += new_offers
            run.pages_processed += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            seed = session.get(CompanyDiscoverySeed, seed.id)
            seed.status = "error"
            seed.last_result = str(exc)[:1000]
            seed.last_inspected_at = datetime.now(timezone.utc)
            seed.next_inspection_at = datetime.now(timezone.utc) + timedelta(days=1)
            session.commit()


def _search_unresolved_seeds(session: Session, run: CollectionRun, settings: Settings) -> None:
    if not settings.brave_search_api_key or not run.brave_hard_cap:
        return
    usage = BraveUsageService(session, _policy(settings))
    client = BraveSearchClient(
        api_key=settings.brave_search_api_key.get_secret_value(), usage_service=usage,
        base_url=settings.brave_search_api_url, timeout_seconds=settings.brave_search_timeout_seconds,
        requests_per_second=settings.brave_search_requests_per_second, run_hard_cap=run.brave_hard_cap,
    )
    client.set_run_id(run.id)
    cached = CachedSearchClient(session, client, ttl=timedelta(days=7))
    seeds = session.scalars(select(CompanyDiscoverySeed).where(
        CompanyDiscoverySeed.status == "review_required",
        CompanyDiscoverySeed.department_code == "94",
    ).order_by(CompanyDiscoverySeed.id).limit(250)).all()
    fresh_fingerprints = set(session.scalars(select(JobDiscoveryQueryCache.query_fingerprint).where(
        JobDiscoveryQueryCache.provider == "brave_search",
        JobDiscoveryQueryCache.fresh_until >= datetime.now(timezone.utc),
    )))
    seen_sirens = set()
    for seed in seeds:
        if seed.siren in seen_sirens:
            continue
        seen_sirens.add(seed.siren)
        query = f'"{seed.company_name}" "{seed.commune or "Val-de-Marne"}" recrutement'
        fingerprint = hashlib.sha256(f"5:{query}".encode("utf-8")).hexdigest()
        if fingerprint in fresh_fingerprints:
            continue
        if _stop(session, run) or _usage(session, run.id) >= run.brave_hard_cap:
            return
        if not eligible_seed(session, seed):
            continue
        try:
            results = cached.search(query, count=5, company_key=normalize_key(seed.company_name),
                                    request_index=run.pages_processed + 1)
        except BraveSearchError as exc:
            run.error_summary = f"Brave : {exc.kind}"
            session.commit()
            return
        child = create_collection_run(session, "brave_search", "company_seed", str(seed.id))
        for result in results:
            snapshot = _signal_from_result(result)
            upsert_recruitment_signal(session, child, snapshot)
        complete_collection_run(session, child, full_scope_completed=True)
        run.signals_found += child.signals_found
        run.pages_processed += 1
        session.commit()
        try:
            verified_site = _verify_seed_site(session, seed, results)
        except Exception as exc:
            session.rollback()
            run = session.get(CollectionRun, run.id)
            run.error_summary = f"Vérification domaine : {str(exc)[:150]}"
            session.commit()
            continue
        if verified_site:
            try:
                new_offers, _ = inspect_seed(session, seed)
                run.offers_new += new_offers
                run.pages_processed += 1
                session.commit()
            except Exception as exc:
                session.rollback()
                run = session.get(CollectionRun, run.id)
                run.error_summary = f"Inspection employeur : {str(exc)[:150]}"
                session.commit()


def _verify_seed_site(session: Session, seed: CompanyDiscoverySeed, results) -> bool:
    """Reuse official-web ownership and identity checks; search hits alone never verify."""
    observed_at = datetime.now(timezone.utc)
    roots = []
    for result in results:
        parsed = urlsplit(result.url)
        domain = registrable_domain(result.url)
        if parsed.scheme != "https" or not parsed.hostname or not domain or classify_domain(parsed.hostname)[1]:
            continue
        root = f"https://{parsed.hostname}/"
        if root not in roots:
            roots.append(root)
    if not roots:
        return False
    from app.services.opportunities.company import normalize_company_key
    target = ContactTarget(
        company_key=normalize_company_key(seed.company_name) or seed.siren,
        organization_name_snapshot=seed.company_name, scope=ContactScope.COMPANY,
        siren=seed.siren, local_key=None, local_commune_snapshot=seed.commune,
        local_location_label_snapshot=None, employer_relationship_status="direct_employer",
        identity_match_status="matched_high_confidence", display_name_snapshot=seed.company_name,
        identity_location_snapshot=seed.commune,
    )
    website_seeds = tuple(WebsiteSeed(url=url, source_provider="brave_search", observed_at=observed_at) for url in roots[:5])
    candidates, _, _ = discover_website_candidates(
        target, target_fingerprint=official_web_target_fingerprint(target),
        seeds=website_seeds, brave_client=None, observed_at=observed_at,
    )
    fetcher = SecureWebFetcher()
    fetcher.set_robots_checker(RobotsTxtPolicy(fetcher).allowed)
    try:
        verified = verify_website_candidates(target, candidates, fetcher=fetcher,
                                             verified_at=observed_at, max_candidates=2,
                                             max_pages_per_domain=3)
    except Exception:
        return False
    repository = OfficialWebRepository(session)
    repository.replace_candidates(official_web_target_fingerprint(target), candidates)
    repository.persist_verified(verified, high_ttl=timedelta(days=90), review_ttl=timedelta(days=30))
    high = [site for site in verified if site.status == WebsiteVerificationStatus.HIGH_CONFIDENCE]
    if len(high) != 1:
        session.commit()
        return False
    seed.official_site_url = high[0].canonical_url
    seed.site_status = "high_confidence"
    session.commit()
    return True


def normalize_key(name: str) -> str | None:
    from app.services.opportunities.company import normalize_company_key
    return normalize_company_key(name)


def _stop(session: Session, run: CollectionRun) -> bool:
    session.refresh(run, attribute_names=["stop_requested"])
    return bool(run.stop_requested)


def _usage(session: Session, run_id: int) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(BraveUsageEvent.quantity), 0)).where(
        BraveUsageEvent.provider == "brave_search", BraveUsageEvent.run_id == run_id,
        BraveUsageEvent.counted_for_budget.is_(True),
    )) or 0)
