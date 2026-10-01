"""Bounded company seeds and source collection; seeds never become needs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompanyDiscoverySeed, CompanySeedCursor, CommercialExclusion, CommercialRelationship, JobSourceBoard, VerifiedWebsiteRecord
from app.services.collection.official_site import OfficialSiteJobProvider
from app.services.collection.providers import JobOfferProviderCollector
from app.services.job_source_boards import create_board, run_board_refresh, create_board_refresh_run, DuplicateBoardError
from app.services.opportunities.company import normalize_company_key
from app.services.contactability.providers.official_web.discovery import classify_domain
from urllib.parse import urlsplit


DINUM_SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"


def import_dinum_seed_page(session: Session, *, requester=httpx.get, page: int | None = None) -> int:
    """Import one public geographic page, then persist a cursor; never bulk scan."""
    cursor = session.scalar(select(CompanySeedCursor).where(CompanySeedCursor.query_key == "active_94_pme"))
    if cursor is None:
        cursor = CompanySeedCursor(query_key="active_94_pme", next_page=1)
        session.add(cursor)
        session.flush()
    requested_page = page or cursor.next_page
    response = requester(DINUM_SEARCH_URL, params={
        "departement": "94", "etat_administratif": "A", "categorie_entreprise": "PME",
        "per_page": 25, "page": requested_page,
    }, headers={"Accept": "application/json", "User-Agent": "LeadOpportunityFinder/0.1"}, timeout=10)
    if response.status_code != 200:
        raise ValueError(f"DINUM seed page failed ({response.status_code})")
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("DINUM seed page is invalid")
    imported = 0
    seen_sirens = {value for (value,) in session.execute(select(CompanyDiscoverySeed.siren))}
    for row in payload["results"]:
        if not isinstance(row, dict) or row.get("etat_administratif") not in {None, "A"}:
            continue
        siren = row.get("siren")
        name = row.get("nom_complet") or row.get("nom_raison_sociale")
        if not isinstance(siren, str) or len(siren) != 9 or not isinstance(name, str) or not name.strip():
            continue
        if siren in seen_sirens or row.get("categorie_entreprise") != "PME":
            continue
        if row.get("est_entrepreneur_individuel") is True:
            continue
        establishments = row.get("matching_etablissements")
        establishments = establishments if isinstance(establishments, list) else []
        if isinstance(row.get("siege"), dict):
            establishments.append(row["siege"])
        for establishment in establishments[:5]:
            if not isinstance(establishment, dict):
                continue
            siret = establishment.get("siret")
            postcode = establishment.get("code_postal")
            if not isinstance(siret, str) or len(siret) != 14 or not isinstance(postcode, str) or not postcode.startswith("94"):
                continue
            if establishment.get("etat_administratif") not in {None, "A"}:
                continue
            if session.scalar(select(CompanyDiscoverySeed.id).where(CompanyDiscoverySeed.siret == siret)):
                continue
            session.add(CompanyDiscoverySeed(
                siren=siren, siret=siret, company_name=name.strip()[:500],
                commune=establishment.get("libelle_commune") if isinstance(establishment.get("libelle_commune"), str) else None,
                employee_range=row.get("tranche_effectif_salarie") if isinstance(row.get("tranche_effectif_salarie"), str) else None,
                naf_code=row.get("activite_principale") if isinstance(row.get("activite_principale"), str) else None,
                origin="dinum", status="new",
            ))
            imported += 1
            seen_sirens.add(siren)
            break
    cursor.next_page = requested_page + 1
    cursor.last_requested_at = datetime.now(timezone.utc)
    session.commit()
    return imported


def eligible_seed(session: Session, seed: CompanyDiscoverySeed) -> bool:
    company_key = normalize_company_key(seed.company_name)
    if not company_key:
        return False
    excluded = session.scalar(select(CommercialExclusion.id).where(
        (CommercialExclusion.company_key == company_key) | (CommercialExclusion.siren == seed.siren)
    ))
    relationship = session.scalar(select(CommercialRelationship.id).where(
        CommercialRelationship.is_active.is_(True),
        (CommercialRelationship.company_key == company_key) | (CommercialRelationship.siren == seed.siren),
        CommercialRelationship.status == "client",
    ))
    return excluded is None and relationship is None


def verified_site_for_seed(session: Session, seed: CompanyDiscoverySeed) -> str | None:
    if (seed.site_status == "high_confidence" and seed.official_site_url
            and not classify_domain(urlsplit(seed.official_site_url).hostname or "")[1]):
        return seed.official_site_url
    company_key = normalize_company_key(seed.company_name)
    if company_key is None:
        return None
    records = session.scalars(select(VerifiedWebsiteRecord).where(
        VerifiedWebsiteRecord.company_key == company_key,
        VerifiedWebsiteRecord.status == "high_confidence",
        VerifiedWebsiteRecord.target_scope == "company",
        VerifiedWebsiteRecord.fresh_until >= datetime.now(timezone.utc),
    ).order_by(VerifiedWebsiteRecord.verified_at.desc()).limit(10)).all()
    return next((record.canonical_url for record in records
                 if not classify_domain(urlsplit(record.canonical_url).hostname or "")[1]), None)


def inspect_seed(session: Session, seed: CompanyDiscoverySeed) -> tuple[int, int]:
    """Use a verified domain only. A candidate domain can never self-verify."""
    now = datetime.now(timezone.utc)
    if not eligible_seed(session, seed):
        seed.status = "excluded"
        seed.last_result = "Exclusion ou client actif"
        seed.last_inspected_at = now
        session.commit()
        return 0, 0
    site = verified_site_for_seed(session, seed)
    if not site:
        seed.status = "review_required"
        seed.last_result = "Domaine officiel à confirmer"
        seed.last_inspected_at = now
        seed.next_inspection_at = now + timedelta(days=30)
        session.commit()
        return 0, 0
    provider = OfficialSiteJobProvider(seed.company_name, site, known_pages=tuple(seed.career_pages or []))
    result = JobOfferProviderCollector(provider).collect(session)
    seed.official_site_url = site
    seed.site_status = "high_confidence"
    seed.career_pages = list(provider.career_pages)
    seed.offer_diagnostics = dict(provider.jobposting_stats)
    ats_rows, ats_new = attach_ats_candidates(session, seed.company_name, provider.ats_candidates)
    seed.ats_candidates = ats_rows
    seed.status = "inspected"
    seed.last_inspected_at = now
    seed.next_inspection_at = now + timedelta(days=7)
    seed.last_result = f"{result.offers_received} offre(s) directes ; {len(provider.ats_candidates)} ATS candidat(s)"
    session.commit()
    return result.offers_new + ats_new, len(provider.ats_candidates)


def attach_ats_candidates(session: Session, company_name: str,
                          candidates: tuple[tuple[str, str], ...]) -> tuple[list[dict], int]:
    ats_rows = []
    ats_new = 0
    provider_counts = {source: sum(1 for item in candidates if item[0] == source)
                       for source in {item[0] for item in candidates}}
    for source, identifier in candidates:
        # A unique link on the verified employer site is direct association
        # evidence, even when the ATS slug abbreviates the legal name.
        high = provider_counts[source] == 1
        status = "high_confidence" if high else "review_required"
        ats_rows.append({"provider": source, "identifier": identifier, "status": status})
        if high:
            try:
                board = create_board(session, provider_id=source, display_name=f"{company_name} · {source}",
                                     board_identifier=identifier, company_name_hint=company_name)
                child, created = create_board_refresh_run(session, board)
                if created:
                    run_board_refresh(session, board.id, child.id)
                    session.refresh(child)
                    ats_new += child.offers_new if child.status == "completed" else 0
                    if child.status != "completed":
                        ats_rows[-1]["status"] = "review_required"
            except DuplicateBoardError:
                pass
    return ats_rows, ats_new
