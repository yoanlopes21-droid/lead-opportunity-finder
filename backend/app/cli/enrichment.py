"""Run or resume durable company enrichment batches from a local terminal."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from app.database import SessionLocal, engine
from app.services.company_enrichment.batch import CompanyEnrichmentBatchOrchestrator
from app.services.company_enrichment.dinum import DinumCompanySearchClient
from app.services.company_enrichment.dinum_adapter import DinumCompanyEnrichmentProvider
from app.services.company_enrichment.persistence import (
    EnrichmentRunStatus,
    ensure_enrichment_schema,
)
from app.services.opportunities.company import (
    CompanyOpportunity,
    aggregate_active_company_opportunities,
)


def select_deterministic_batch(
    opportunities: Sequence[CompanyOpportunity], limit: int
) -> tuple[CompanyOpportunity, ...]:
    """Interleave high, medium and low hiring volumes deterministically."""
    if limit < 1:
        raise ValueError("limit must be positive")
    ordered = sorted(opportunities, key=lambda item: (item.company_name.casefold(), item.company_key))
    bands = (
        [item for item in ordered if item.active_offer_count >= 10],
        [item for item in ordered if 2 <= item.active_offer_count < 10],
        [item for item in ordered if item.active_offer_count == 1],
    )
    selected: list[CompanyOpportunity] = []
    position = 0
    while len(selected) < limit and any(position < len(band) for band in bands):
        for band in bands:
            if position < len(band) and len(selected) < limit:
                selected.append(band[position])
        position += 1
    return tuple(selected)


def execute_cli(
    argv: Optional[Sequence[str]] = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    provider=None,
    output: Callable[[str], None] = print,
    prepare_schema: bool = True,
) -> int:
    parser = argparse.ArgumentParser(description="Durable local company enrichment batch")
    commands = parser.add_subparsers(dest="command", required=True)
    new_parser = commands.add_parser("new", help="Create and run a durable selection")
    new_parser.add_argument("--limit", type=int, default=120)
    new_parser.add_argument("--department", default="94")
    resume_parser = commands.add_parser("resume", help="Resume a stored run")
    resume_parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)

    if prepare_schema:
        ensure_enrichment_schema(engine)
    selected_provider = provider or DinumCompanyEnrichmentProvider(DinumCompanySearchClient())

    def progress(run) -> None:
        output(_progress_line(run))

    runner = CompanyEnrichmentBatchOrchestrator(
        selected_provider, progress_callback=progress
    )
    with session_factory() as session:
        try:
            if args.command == "new":
                aggregation = aggregate_active_company_opportunities(
                    session, department_code=args.department
                )
                selection = select_deterministic_batch(aggregation.opportunities, args.limit)
                run = runner.run(session, selection)
            else:
                run = runner.resume(session, args.run_id)
        except ValueError:
            output("Batch could not be started or resumed safely.")
            return 4
        output(f"run={run.id} status={run.status} {_progress_line(run)}")
        if run.status == EnrichmentRunStatus.COMPLETED:
            return 0
        if run.status == EnrichmentRunStatus.COMPLETED_WITH_ERRORS:
            return 2
        return 3


def _progress_line(run) -> str:
    return (
        f"processed={run.processed_count}/{run.selected_count} "
        f"high={run.high_confidence_count} review={run.review_needed_count} "
        f"ambiguous={run.ambiguous_count} generic={run.generic_count} "
        f"not_found={run.not_found_count} errors={run.error_count}"
    )


def main() -> int:
    return execute_cli()


if __name__ == "__main__":
    raise SystemExit(main())
