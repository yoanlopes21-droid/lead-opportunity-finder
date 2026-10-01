"""One bounded manual source run over boards, verified sites and company seeds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BraveUsageEvent, CollectionRun, CompanyDiscoverySeed, JobSourceBoard, VerifiedWebsiteRecord, WebsiteCandidateRecord, CompanyEnrichment, CommercialExclusion, CommercialRelationship
from app.services.brave_usage import BraveUsageService, BraveBudgetExceeded
from app.services.collection.open_web import CachedSearchClient, _signal_from_result
from app.services.company_first import import_dinum_seed_page, inspect_seed, eligible_seed, verified_site_for_seed, attach_ats_candidates
from app.services.collection.official_site import OfficialSiteJobProvider
from app.services.collection.official_site import ats_link
from app.services.contactability.normalization import normalize_generic
from app.services.job_source_boards import create_board, DuplicateBoardError
from app.services.collection.providers import JobOfferProviderCollector
from app.services.contactability.providers.official_web.brave_client import BraveSearchClient, BraveSearchError
from app.services.contactability.contracts import ContactTarget, ContactScope
from app.services.contactability.providers.official_web.contracts import WebsiteSeed, WebsiteVerificationStatus
from app.services.contactability.providers.official_web.discovery import discover_website_candidates, classify_domain, registrable_domain
from app.services.contactability.providers.official_web.fetcher import SecureWebFetcher, SecureFetchError
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy
from app.services.contactability.providers.official_web.verification import verify_website_candidates, _replace_status
from app.services.contactability.providers.official_web.persistence import OfficialWebRepository
from app.services.contactability.providers.official_web.provider import official_web_target_fingerprint
from app.services.inpi_rne import InpiRneClient, InpiUnavailable, RneNotFound, RneNotReusable
from app.services.job_source_boards import create_board_refresh_run, run_board_refresh
from app.services.open_web_runs import _policy
from app.services.persistence.offers import create_collection_run, complete_collection_run, upsert_recruitment_signal


RUN_SOURCE = "non_ft_discovery"


def _count(run: CollectionRun, key: str, amount: int = 1) -> None:
    stats = dict(run.funnel_stats or {})
    stats[key] = stats.get(key, 0) + amount
    run.funnel_stats = stats


def _seed_outcome(run: CollectionRun, seed: CompanyDiscoverySeed, new_offers: int) -> None:
    if seed.site_status == "high_confidence":
        _count(run, "domains_validated")
    if seed.career_pages:
        _count(run, "seeds_with_career_page")
        _count(run, "career_pages", len(seed.career_pages))
    if seed.ats_candidates:
        _count(run, "ats_candidates", len(seed.ats_candidates))
        for row in seed.ats_candidates:
            if row.get("status") == "high_confidence":
                _count(run, "ats_confirmed")
                _count(run, "ats_confirmed_" + row["provider"])
    for key, value in (seed.offer_diagnostics or {}).items():
        _count(run, key, value)
    if new_offers:
        _count(run, "offers_94_confirmed", new_offers)


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
    run.funnel_stats = {
        "seeds_available": int(session.scalar(select(func.count()).select_from(CompanyDiscoverySeed).where(CompanyDiscoverySeed.department_code == "94")) or 0),
        "known_domains_available": int(session.scalar(select(func.count(func.distinct(VerifiedWebsiteRecord.registrable_domain))).where(
            VerifiedWebsiteRecord.status == "high_confidence", VerifiedWebsiteRecord.target_scope == "company")) or 0),
    }
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
            _resolve_rne_seeds(session, run, settings)
        if not _stop(session, run) and run.offers_new < (run.target_signal_count or 25):
            _resolve_cached_seeds(session, run)
        if not _stop(session, run) and run.offers_new < (run.target_signal_count or 25):
            _search_unresolved_seeds(session, run, settings)
        run.funnel_stats = {**(run.funnel_stats or {}), "brave_requests": max(0, _usage(session, run_id) - before)}
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
            ats_rows, ats_new = attach_ats_candidates(session, name, provider.ats_candidates)
            run.offers_new += child.offers_new
            run.offers_new += ats_new
            run.offers_updated += child.offers_updated
            run.pages_processed += 1
            _count(run, "known_domains_inspected")
            if provider.career_pages:
                _count(run, "seeds_with_career_page")
                _count(run, "career_pages", len(provider.career_pages))
            _count(run, "ats_candidates", len(ats_rows))
            for row in ats_rows:
                if row["status"] == "high_confidence":
                    _count(run, "ats_confirmed")
                    _count(run, "ats_confirmed_" + row["provider"])
            for key, value in provider.jobposting_stats.items():
                _count(run, key, value)
            session.commit()
        except Exception as exc:
            session.rollback()
            run = session.get(CollectionRun, run.id)
            if isinstance(exc, SecureFetchError) and exc.kind == "robots_disallowed":
                _count(run, "known_sites_robots_disallowed")
            else:
                run.error_summary = f"Site officiel : {str(exc)[:150]}"
            session.commit()


def _inspect_seeds(session: Session, run: CollectionRun) -> None:
    # One public DINUM page per run advances the company universe without a bulk scan.
    try:
        import_dinum_seed_page(session)
    except Exception as exc:
        run.error_summary = f"DINUM indisponible : {str(exc)[:150]}"
        session.commit()
    run.funnel_stats = {**(run.funnel_stats or {}), "seeds_available": int(session.scalar(
        select(func.count()).select_from(CompanyDiscoverySeed).where(CompanyDiscoverySeed.department_code == "94")) or 0)}
    now = datetime.now(timezone.utc)
    seeds = session.scalars(select(CompanyDiscoverySeed).where(
        CompanyDiscoverySeed.department_code == "94",
        (CompanyDiscoverySeed.next_inspection_at.is_(None) | (CompanyDiscoverySeed.next_inspection_at <= now)),
    ).order_by(CompanyDiscoverySeed.official_site_url.is_(None),
               CompanyDiscoverySeed.employee_range.in_(("00", "NN", "53", None)),
               CompanyDiscoverySeed.id).limit(100)).all()
    seen_sirens = set()
    for seed in seeds:
        if seed.siren in seen_sirens:
            continue
        seen_sirens.add(seed.siren)
        if len(seen_sirens) > 20:
            return
        if _stop(session, run) or run.offers_new >= (run.target_signal_count or 25):
            return
        if seed.employee_range == "00":
            _count(run, "seeds_excluded_no_employees")
            session.commit()
            continue
        if not eligible_seed(session, seed):
            _count(run, "seeds_excluded_before_network")
            session.commit()
            continue
        try:
            if seed.official_site_url and seed.site_status == "high_confidence":
                _count(run, "domains_already_known")
            new_offers, _ = inspect_seed(session, seed)
            _seed_outcome(run, seed, new_offers)
            _count(run, "seeds_inspected")
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


def _resolve_rne_seeds(session: Session, run: CollectionRun, settings: Settings) -> None:
    if not settings.inpi_username or not settings.inpi_password:
        _count(run, "inpi_unconfigured")
        session.commit()
        return
    client = InpiRneClient(settings.inpi_username.get_secret_value(), settings.inpi_password.get_secret_value())
    seeds = session.scalars(select(CompanyDiscoverySeed).where(
        CompanyDiscoverySeed.department_code == "94",
        CompanyDiscoverySeed.status == "review_required",
    ).order_by(CompanyDiscoverySeed.employee_range.in_(("00", "NN", "53", None)), CompanyDiscoverySeed.id).limit(250)).all()
    seen = set()
    requests_attempted = 0
    for seed in seeds:
        if seed.siren in seen or _stop(session, run):
            continue
        seen.add(seed.siren)
        if seed.employee_range == "00" or not eligible_seed(session, seed):
            _count(run, "seeds_excluded_before_network")
            continue
        if verified_site_for_seed(session, seed):
            _count(run, "domains_already_known")
            continue
        evidence = seed.domain_evidence or {}
        if (evidence.get("source") == "inpi_rne" and evidence.get("checked_at")
                and (evidence.get("status") != "absent" or evidence.get("schema_version") == 2)):
            try:
                checked = datetime.fromisoformat(evidence["checked_at"])
                ttl = timedelta(days=1 if evidence.get("status") == "error" else 30)
                if datetime.now(timezone.utc) - checked < ttl:
                    _count(run, "rne_cached")
                    continue
            except (TypeError, ValueError):
                pass
        if requests_attempted >= 20:
            break
        requests_attempted += 1
        _count(run, "rne_requests_attempted")
        try:
            declared = client.domains_for_siren(seed.siren)
        except (RneNotFound, RneNotReusable) as exc:
            status = "not_found" if isinstance(exc, RneNotFound) else "not_reusable"
            _count(run, "rne_" + status)
            reason = {
                "RNE dissemination restricted": "dissemination_restricted",
                "RNE commercial reuse opposed or unknown": "commercial_reuse_unavailable",
                "RNE legal entity unavailable": "legal_entity_unavailable",
            }.get(str(exc))
            if reason:
                _count(run, "rne_" + reason)
            seed.domain_evidence = {"source": "inpi_rne", "status": status, "siren": seed.siren,
                                    "checked_at": datetime.now(timezone.utc).isoformat(), "schema_version": 2,
                                    "reason": reason}
            session.commit()
            continue
        except InpiUnavailable as exc:
            _count(run, "inpi_errors")
            seed.domain_evidence = {"source": "inpi_rne", "status": "error", "siren": seed.siren,
                                    "checked_at": datetime.now(timezone.utc).isoformat(), "schema_version": 2}
            session.commit()
            if str(exc) in {"RNE authentication failed", "RNE response status 401",
                            "RNE response status 403", "RNE response status 429"}:
                return
            continue
        _count(run, "rne_seeds_checked")
        seed.domain_evidence = {
            "source": "inpi_rne", "status": "candidate" if declared else "absent",
            "schema_version": 2,
            "siren": seed.siren, "siret": seed.siret,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "declarations": [{"domain": item.domain, "effective_date": item.effective_date,
                              "source_path": item.source_path, "source_updated_at": item.source_updated_at}
                             for item in declared[:3]],
        }
        if not declared:
            _count(run, "rne_domain_absent")
        for item in declared[:3]:
            _count(run, "rne_domain_candidates")
            if seed.official_site_url and registrable_domain(seed.official_site_url) != item.domain:
                _count(run, "domain_conflicts")
                seed.domain_evidence = {"status": "conflict", "source": "inpi_rne", "siren": seed.siren,
                                        "declared_domain": item.domain, "effective_date": item.effective_date,
                                        "checked_at": datetime.now(timezone.utc).isoformat()}
                continue
            try:
                if _verify_seed_site(session, seed, [f"https://{item.domain}/"], source="inpi_rne",
                                     run=run, effective_date=item.effective_date, source_path=item.source_path,
                                     source_updated_at=item.source_updated_at):
                    _count(run, "rne_domains_validated")
                    new_offers, _ = inspect_seed(session, seed)
                    _seed_outcome(run, seed, new_offers)
                    run.offers_new += new_offers
                    break
            except Exception:
                session.rollback()
                _count(run, "rne_inspection_errors")
                session.commit()
                continue
        session.commit()


def _resolve_cached_seeds(session: Session, run: CollectionRun) -> None:
    """Recheck previously observed official-site links without a search request."""
    seeds = session.scalars(select(CompanyDiscoverySeed).where(
        CompanyDiscoverySeed.department_code == "94",
        CompanyDiscoverySeed.status == "review_required",
    ).order_by(CompanyDiscoverySeed.employee_range.in_(("00", "NN", "53", None)),
               CompanyDiscoverySeed.id).limit(250)).all()
    seen = set()
    inspected = 0
    for seed in seeds:
        if seed.siren in seen or _stop(session, run):
            continue
        seen.add(seed.siren)
        if seed.employee_range == "00" or not eligible_seed(session, seed) or verified_site_for_seed(session, seed):
            continue
        evidence = seed.domain_evidence or {}
        if evidence.get("cache_checked_at"):
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(evidence["cache_checked_at"]) < timedelta(days=30):
                    _count(run, "cache_recently_checked")
                    continue
            except (TypeError, ValueError):
                pass
        candidates = session.scalars(select(WebsiteCandidateRecord).where(
            WebsiteCandidateRecord.company_key == normalize_key(seed.company_name),
            WebsiteCandidateRecord.is_active.is_(True),
            WebsiteCandidateRecord.classification != "excluded",
        ).order_by(WebsiteCandidateRecord.observed_at.desc()).limit(4)).all()
        if not candidates:
            continue
        if inspected >= 20:
            break
        inspected += 1
        _count(run, "cache_seeds_checked")
        _count(run, "cache_domain_candidates", len(candidates))
        try:
            validated = _verify_seed_site(session, seed, [row.canonical_url for row in candidates],
                                          source="official_web_cache", run=run)
            if validated:
                _count(run, "domains_from_cache")
                seed.domain_evidence = {**(seed.domain_evidence or {}),
                                        "cache_origin_sources": sorted({row.search_provider for row in candidates})}
                new_offers, _ = inspect_seed(session, seed)
                _seed_outcome(run, seed, new_offers)
                run.offers_new += new_offers
            else:
                seed.domain_evidence = {**(seed.domain_evidence or {}),
                                        "cache_checked_at": datetime.now(timezone.utc).isoformat()}
            session.commit()
        except Exception:
            session.rollback()
            _count(run, "cache_inspection_errors")
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
    ).order_by(CompanyDiscoverySeed.employee_range.in_(("00", "NN", "53", None)), CompanyDiscoverySeed.id).limit(250)).all()
    seen_sirens = set()
    for seed in seeds:
        if seed.siren in seen_sirens:
            continue
        seen_sirens.add(seed.siren)
        if seed.employee_range == "00" or not eligible_seed(session, seed):
            _count(run, "seeds_excluded_before_network")
            continue
        if verified_site_for_seed(session, seed):
            _count(run, "domains_already_known")
            continue
        query = f'"{seed.company_name}" "{seed.commune or "Val-de-Marne"}" site officiel'
        if _stop(session, run) or _usage(session, run.id) >= run.brave_hard_cap:
            return
        try:
            results = cached.search(query, count=5, company_key=normalize_key(seed.company_name),
                                    request_index=run.pages_processed + 1)
        except (BraveSearchError, BraveBudgetExceeded) as exc:
            run.error_summary = f"Brave : {getattr(exc, 'kind', 'budget')}"
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
            verified_site = _verify_seed_site(session, seed, results, source="brave_search", run=run)
        except Exception as exc:
            session.rollback()
            run = session.get(CollectionRun, run.id)
            run.error_summary = f"Vérification domaine : {str(exc)[:150]}"
            session.commit()
            continue
        if verified_site:
            _count(run, "brave_domains_validated")
            try:
                new_offers, _ = inspect_seed(session, seed)
                _seed_outcome(run, seed, new_offers)
                run.offers_new += new_offers
                run.pages_processed += 1
                session.commit()
            except Exception as exc:
                session.rollback()
                run = session.get(CollectionRun, run.id)
                run.error_summary = f"Inspection employeur : {str(exc)[:150]}"
                session.commit()
        elif _usage(session, run.id) < run.brave_hard_cap:
            _search_ats_fallback(session, run, seed, cached)


_ATS_SEARCH_HOSTS = ("jobs.lever.co", "boards.greenhouse.io", "jobs.ashbyhq.com", "apply.workable.com")


def _search_ats_fallback(session: Session, run: CollectionRun, seed: CompanyDiscoverySeed,
                         cached: CachedSearchClient) -> None:
    for host in _ATS_SEARCH_HOSTS:
        if _stop(session, run) or _usage(session, run.id) >= (run.brave_hard_cap or 0):
            return
        query = f'"{seed.company_name}" site:{host}'
        try:
            results = cached.search(query, count=5, company_key=normalize_key(seed.company_name),
                                    request_index=run.pages_processed + 1)
        except (BraveSearchError, BraveBudgetExceeded):
            _count(run, "ats_search_errors")
            session.commit()
            return
        _count(run, "ats_search_queries")
        run.pages_processed += 1
        candidates = {ats_link(result.url) for result in results}
        candidates.discard(None)
        candidates = {item for item in candidates if item[0] == _ATS_HOST_PROVIDER[host]}
        _count(run, "ats_web_candidates", len(candidates))
        if len(candidates) != 1:
            if len(candidates) > 1:
                _count(run, "ats_ambiguous")
            session.commit()
            continue
        source, identifier = next(iter(candidates))
        if not _verify_ats_search_board(seed.company_name, host, identifier):
            _count(run, "ats_identity_rejected")
            session.commit()
            continue
        try:
            board = create_board(session, provider_id=source,
                                 display_name=f"{seed.company_name} · {source}",
                                 board_identifier=identifier, company_name_hint=seed.company_name)
        except DuplicateBoardError:
            return
        child, created = create_board_refresh_run(session, board)
        if created:
            run_board_refresh(session, board.id, child.id)
        session.refresh(child)
        if child.status != "completed":
            board.enabled = False
            _count(run, "ats_provider_rejected")
            session.commit()
            return
        seed.ats_candidates = [{"provider": source, "identifier": identifier,
                                "status": "high_confidence", "discovered_by": "brave_ats",
                                "evidence": f"https://{host}/{identifier}"}]
        _count(run, "ats_confirmed")
        _count(run, "ats_confirmed_" + source)
        run.offers_new += child.offers_new
        if child.offers_new:
            _count(run, "offers_94_confirmed", child.offers_new)
        session.commit()
        return


_ATS_HOST_PROVIDER = {"jobs.lever.co": "lever", "boards.greenhouse.io": "greenhouse",
                      "jobs.ashbyhq.com": "ashby", "apply.workable.com": "workable"}


def _verify_ats_search_board(company_name: str, host: str, identifier: str) -> bool:
    fetcher = SecureWebFetcher()
    fetcher.set_robots_checker(RobotsTxtPolicy(fetcher).allowed)
    try:
        page = fetcher.fetch(f"https://{host}/{identifier}", initial=False)
    except Exception:
        return False
    if urlsplit(page.final_url).hostname != host:
        return False
    name = normalize_generic(company_name) or ""
    text = normalize_generic(page.text) or ""
    return len(name) >= 6 and name in text


def _verify_seed_site(session: Session, seed: CompanyDiscoverySeed, results, *, source: str,
                      run: CollectionRun | None = None, effective_date: str | None = None,
                      source_path: str | None = None, source_updated_at: str | None = None) -> bool:
    """Reuse official-web ownership and identity checks; search hits alone never verify."""
    observed_at = datetime.now(timezone.utc)
    roots = []
    for result in results:
        url = result if isinstance(result, str) else result.url
        parsed = urlsplit(url)
        domain = registrable_domain(url)
        if parsed.scheme != "https" or not parsed.hostname or not domain or classify_domain(parsed.hostname)[1]:
            if run:
                _count(run, "domain_rejected_classification")
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
    website_seeds = tuple(WebsiteSeed(url=url, source_provider=source, observed_at=observed_at,
                                      structured=source == "inpi_rne") for url in roots[:4])
    candidates, _, _ = discover_website_candidates(
        target, target_fingerprint=official_web_target_fingerprint(target),
        seeds=website_seeds, brave_client=None, observed_at=observed_at,
    )
    fetcher = SecureWebFetcher()
    fetcher.set_robots_checker(RobotsTxtPolicy(fetcher).allowed)
    verified = []
    for candidate in candidates[:4]:
        if run:
            _count(run, "domain_candidates")
        try:
            assessed = verify_website_candidates(target, (candidate,), fetcher=fetcher,
                                                 verified_at=observed_at, max_candidates=1,
                                                 max_pages_per_domain=3)
            verified.extend(assessed)
            if run and assessed and not any(reason.startswith("fetch_") for reason in assessed[0].rejection_reasons):
                _count(run, "homepages_accessible")
        except Exception:
            if run:
                _count(run, "domain_rejected_fetch")
            continue
    high = [site for site in verified if site.status == WebsiteVerificationStatus.HIGH_CONFIDENCE]
    if len(high) > 1:
        verified = [_replace_status(site, WebsiteVerificationStatus.AMBIGUOUS, "competing_domains")
                    if site in high else site for site in verified]
        high = []
    elif high and seed.official_site_url and registrable_domain(seed.official_site_url) != high[0].registrable_domain:
        verified = [_replace_status(site, WebsiteVerificationStatus.AMBIGUOUS, "existing_domain_conflict")
                    if site in high else site for site in verified]
        high = []
        if run:
            _count(run, "domain_conflicts")
    repository = OfficialWebRepository(session)
    repository.replace_candidates(official_web_target_fingerprint(target), candidates)
    repository.persist_verified(verified, high_ttl=timedelta(days=90), review_ttl=timedelta(days=30))
    if run:
        for site in verified:
            _count(run, "domain_" + site.status)
            for reason in site.rejection_reasons:
                _count(run, "domain_rejection_" + reason)
    if len(high) != 1:
        session.commit()
        return False
    seed.official_site_url = high[0].canonical_url
    seed.site_status = "high_confidence"
    seed.domain_evidence = {"discovered_by": source, "evidence": high[0].fingerprint,
                            "confidence": high[0].score, "verification_status": high[0].status,
                            "siren": seed.siren, "siret": seed.siret,
                            "declared_domain": roots[0] if source == "inpi_rne" else None,
                            "effective_date": effective_date, "source_path": source_path,
                            "source_updated_at": source_updated_at,
                            "verified_at": observed_at.isoformat()}
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
