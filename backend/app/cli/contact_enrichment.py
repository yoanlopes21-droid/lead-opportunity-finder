"""Run or resume durable, optional contact-provider enrichment batches."""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Sequence
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import CompanyEnrichment, ContactEnrichmentRun, ObservedJobOffer
from app.services.company_enrichment.contracts import MatchStatus
from app.services.commercial_leads.service import CommercialLeadQuery, list_commercial_leads
from app.services.contactability.batch import (
    ContactEnrichmentBatchOrchestrator,
    ContactEnrichmentRunStatus,
    ensure_contact_enrichment_schema,
)
from app.services.contactability.providers.societe_com import (
    SocieteComContactProvider,
    societe_com_inapplicability_reason,
)
from app.services.contactability.providers.official_web.contracts import WebsiteSeed
from app.services.contactability.providers.official_web.fetcher import SecureWebFetcher
from app.services.contactability.providers.official_web.persistence import (
    OfficialWebRepository,
    ensure_official_web_schema,
)
from app.services.contactability.providers.official_web.provider import OfficialWebProvider
from app.services.contactability.targets import ContactIdentityContext, build_contact_targets
from app.services.contactability.contracts import ContactScope, ContactTarget
from app.services.contactability.persistence import ensure_contactability_schema
from app.services.brave_usage import BraveBudgetPolicy, BraveUsageService, ensure_brave_usage_schema
from app.services.contactability.providers.official_web.brave_client import BraveSearchClient
from app.services.opportunities.company import normalize_company_key


_OFFER_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def select_societe_com_targets(
    session: Session, department: str, limit: int,
) -> tuple:
    """Return only deterministic, confirmed company targets for Societe.com."""
    if limit < 1:
        raise ValueError("limit must be positive")
    leads = list_commercial_leads(
        session,
        CommercialLeadQuery(department_code=department, include_excluded=True, limit=None),
    ).items
    targets = []
    for lead in leads:
        identity = ContactIdentityContext(
            match_status=MatchStatus.HIGH_CONFIDENCE if lead.siren else None,
        )
        targets.extend(
            target for target in build_contact_targets(lead, identity)
            if societe_com_inapplicability_reason(target) is None
        )
    return tuple(sorted(
        targets,
        key=lambda target: (target.company_key.casefold(), target.siren or ""),
    )[:limit])


def select_official_web_targets(
    session: Session, company_keys: Sequence[str], limit: int, *,
    offer_description_only: bool = False,
) -> tuple[ContactTarget, ...]:
    """Select explicit non-local targets for official-web discovery.

    An explicit company selection is sufficient for Brave discovery: an offer
    description URL is an optional seed, not an eligibility condition.  The
    former seed-only selection remains available for controlled legacy runs.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    requested = tuple(dict.fromkeys(key.strip() for key in company_keys if key.strip()))
    if not requested:
        raise ValueError("official_web requires at least one explicit company key")
    leads = {
        item.company_key: item
        for item in list_commercial_leads(
            session, CommercialLeadQuery(department_code="94", include_excluded=True, limit=None),
        ).items
    }
    selected: list[ContactTarget] = []
    for key in requested:
        lead = leads.get(key)
        if lead is None:
            raise ValueError("official_web company key is not an active local lead")
        enrichment = session.scalar(select(CompanyEnrichment).where(
            CompanyEnrichment.company_key == key, CompanyEnrichment.provider == "dinum",
        ))
        targets = build_contact_targets(
            lead, ContactIdentityContext(match_status=enrichment.match_status if enrichment else None),
        )
        for target in targets:
            if target.scope not in {ContactScope.COMPANY, ContactScope.INTERMEDIARY}:
                continue
            if offer_description_only and not offer_description_website_seeds(session, target):
                continue
            selected.append(target)
    if len(selected) > limit:
        raise ValueError("official_web explicit selection exceeds limit")
    return tuple(selected)


def offer_description_website_seeds(
    session: Session, target: ContactTarget,
) -> tuple[WebsiteSeed, ...]:
    """Read existing offer-description URLs only; no search provider is involved."""
    seeds: dict[str, WebsiteSeed] = {}
    offers = session.scalars(select(ObservedJobOffer).where(
        ObservedJobOffer.is_active.is_(True), ObservedJobOffer.department_code == "94",
    ))
    for offer in offers:
        if normalize_company_key(offer.company_name) != target.company_key or not offer.description:
            continue
        for match in _OFFER_URL_PATTERN.finditer(offer.description):
            url = match.group(0).rstrip(".,;:!?)\"")
            if url:
                seeds.setdefault(url, WebsiteSeed(
                    url=url, source_provider="offer_description", observed_at=offer.last_seen_at,
                    source_url=offer.source_url,
                ))
    return tuple(seeds.values())


def official_web_provider(session: Session, *, run_hard_cap: Optional[int] = None) -> OfficialWebProvider:
    """Build the provider with protected HTTP and budget-guarded optional Brave."""
    settings = get_settings()
    fetcher = SecureWebFetcher(
        timeout_seconds=settings.official_web_fetch_timeout_seconds,
        requests_per_second_per_domain=settings.official_web_fetch_requests_per_second,
        max_response_bytes=settings.official_web_max_response_bytes,
    )
    policy = BraveBudgetPolicy(
        monthly_request_budget=settings.brave_search_monthly_request_budget,
        default_run_hard_cap=settings.brave_search_default_run_hard_cap,
        estimated_price_per_1000_usd=settings.brave_search_estimated_price_per_1000_usd,
        estimated_monthly_free_credit_usd=settings.brave_search_estimated_monthly_free_credit_usd,
    )
    brave_client = None
    if settings.brave_search_api_key:
        brave_client = BraveSearchClient(
            api_key=settings.brave_search_api_key.get_secret_value(),
            base_url=settings.brave_search_api_url, timeout_seconds=settings.brave_search_timeout_seconds,
            requests_per_second=settings.brave_search_requests_per_second,
            usage_service=BraveUsageService(session, policy),
            run_hard_cap=(policy.default_run_hard_cap if run_hard_cap is None else run_hard_cap),
        )
    return OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=fetcher, brave_client=brave_client,
        seed_loader=lambda target: offer_description_website_seeds(session, target),
    )


def execute_cli(
    argv: Optional[Sequence[str]] = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    provider=None,
    output: Callable[[str], None] = print,
    prepare_schema: bool = True,
    target_selector: Callable[[Session, str, int], tuple] = select_societe_com_targets,
) -> int:
    parser = argparse.ArgumentParser(description="Durable optional contact enrichment batch")
    commands = parser.add_subparsers(dest="command", required=True)
    new_parser = commands.add_parser("new", help="Create and run a persisted selection")
    new_parser.add_argument("--provider", choices=["societe_com", "official_web"], required=True)
    new_parser.add_argument("--department", default="94")
    new_parser.add_argument("--limit", type=int, default=50)
    new_parser.add_argument("--company-key", action="append", default=[])
    new_parser.add_argument(
        "--offer-description-only", action="store_true",
        help="Restrict official_web to targets that already have an offer-description URL seed.",
    )
    resume_parser = commands.add_parser("resume", help="Resume a persisted selection")
    resume_parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)

    if prepare_schema:
        ensure_contact_enrichment_schema(engine)
        if args.command == "new" and args.provider == "official_web":
            ensure_contactability_schema(engine)
            ensure_official_web_schema(engine)
            ensure_brave_usage_schema(engine)

    def progress(run) -> None:
        output(_progress_line(run))

    with session_factory() as session:
        try:
            selected_provider = provider
            if selected_provider is None:
                is_official_web = (
                    args.command == "new" and args.provider == "official_web"
                ) or (
                    args.command == "resume"
                    and session.scalar(select(ContactEnrichmentRun.provider).where(
                        ContactEnrichmentRun.id == args.run_id,
                    )) == "official_web"
                )
                selected_provider = (
                    official_web_provider(session)
                    if is_official_web
                    else SocieteComContactProvider.from_settings(get_settings())
                )
            if not selected_provider.is_configured:
                output("Contact provider is not configured; no run was created.")
                return 4
            runner = ContactEnrichmentBatchOrchestrator(selected_provider, progress_callback=progress)
            if args.command == "new":
                targets = (
                    select_official_web_targets(
                        session, args.company_key, args.limit,
                        offer_description_only=args.offer_description_only,
                    )
                    if args.provider == "official_web"
                    else target_selector(session, args.department, args.limit)
                )
                run = runner.run(session, targets)
            else:
                run = runner.resume(session, args.run_id)
        except ValueError:
            output("Contact enrichment batch could not be started or resumed safely.")
            return 4
        output(f"run={run.id} status={run.status} {_progress_line(run)}")
        if run.status == ContactEnrichmentRunStatus.COMPLETED:
            return 0
        if run.status == ContactEnrichmentRunStatus.COMPLETED_WITH_ERRORS:
            return 2
        return 3


def _progress_line(run) -> str:
    return (
        f"processed={run.processed_count}/{run.selected_count} "
        f"cached={run.cached_count} completed={run.completed_count} "
        f"not_found={run.not_found_count} not_applicable={run.not_applicable_count} "
        f"errors={run.error_count} calls={run.external_call_count} retries={run.retry_count}"
    )


def main() -> int:
    return execute_cli()


if __name__ == "__main__":
    raise SystemExit(main())
