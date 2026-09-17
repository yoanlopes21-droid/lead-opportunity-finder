"""Run or resume durable, optional contact-provider enrichment batches."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import SessionLocal, engine
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
from app.services.contactability.targets import ContactIdentityContext, build_contact_targets


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
    new_parser.add_argument("--provider", choices=["societe_com"], required=True)
    new_parser.add_argument("--department", default="94")
    new_parser.add_argument("--limit", type=int, default=50)
    resume_parser = commands.add_parser("resume", help="Resume a persisted selection")
    resume_parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)

    selected_provider = provider or SocieteComContactProvider.from_settings(get_settings())
    if not selected_provider.is_configured:
        output("Contact provider is not configured; no run was created.")
        return 4
    if prepare_schema:
        ensure_contact_enrichment_schema(engine)

    def progress(run) -> None:
        output(_progress_line(run))

    runner = ContactEnrichmentBatchOrchestrator(selected_provider, progress_callback=progress)
    with session_factory() as session:
        try:
            if args.command == "new":
                run = runner.run(session, target_selector(session, args.department, args.limit))
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
