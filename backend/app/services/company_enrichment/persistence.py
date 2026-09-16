"""Generic persistence and run lifecycle for company enrichments."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompanyEnrichment, EnrichmentRun
from app.services.company_enrichment.contracts import (
    MatchStatus,
    ProviderEnrichmentResult,
    VALID_MATCH_STATUSES,
    VALID_SECTOR_TYPES,
)
from app.services.opportunities.company import CompanyOpportunity, normalize_company_key


class EnrichmentRunStatus:
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


def create_enrichment_run(
    session: Session,
    provider: str,
    selected_count: int,
    now: Optional[datetime] = None,
) -> EnrichmentRun:
    if selected_count < 0:
        raise ValueError("selected_count must not be negative")
    run = EnrichmentRun(
        provider=provider,
        status=EnrichmentRunStatus.RUNNING,
        selected_count=selected_count,
        started_at=now or _utc_now(),
    )
    session.add(run)
    session.flush()
    return run


def finish_enrichment_run(
    session: Session,
    run: EnrichmentRun,
    status: str,
    now: Optional[datetime] = None,
) -> EnrichmentRun:
    if run.status != EnrichmentRunStatus.RUNNING:
        raise ValueError("enrichment run is not running")
    if status not in {
        EnrichmentRunStatus.COMPLETED,
        EnrichmentRunStatus.COMPLETED_WITH_ERRORS,
        EnrichmentRunStatus.FAILED,
    }:
        raise ValueError("invalid terminal enrichment run status")
    run.status = status
    run.finished_at = now or _utc_now()
    session.flush()
    return run


def upsert_company_enrichment(
    session: Session,
    run: EnrichmentRun,
    opportunity: CompanyOpportunity,
    result: ProviderEnrichmentResult,
    input_fingerprint: str,
    attempts: int = 1,
    now: Optional[datetime] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> CompanyEnrichment:
    """Store a latest-state result without promoting suggestions to identity."""
    if run.status != EnrichmentRunStatus.RUNNING:
        raise ValueError("enrichment run is not running")
    if result.status not in VALID_MATCH_STATUSES:
        raise ValueError("invalid enrichment match status")
    if result.entity_sector_type not in VALID_SECTOR_TYPES:
        raise ValueError("invalid entity sector type")
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if result.status == MatchStatus.HIGH_CONFIDENCE and result.confirmed_identity is None:
        raise ValueError("high-confidence enrichment requires a confirmed identity")

    attempted_at = now or _utc_now()
    row = session.scalar(
        select(CompanyEnrichment).where(
            CompanyEnrichment.company_key == opportunity.company_key,
            CompanyEnrichment.provider == run.provider,
        )
    )
    if row is None:
        row = CompanyEnrichment(
            company_key=opportunity.company_key,
            provider=run.provider,
            source_company_name=opportunity.company_name,
            match_status=result.status,
            entity_sector_type=result.entity_sector_type,
            last_attempt_at=attempted_at,
            attempt_count=0,
            input_fingerprint=input_fingerprint,
        )
        session.add(row)

    row.source_company_name = opportunity.company_name
    row.match_status = result.status
    row.confidence_score = result.confidence_score
    row.entity_sector_type = result.entity_sector_type
    row.last_attempt_at = attempted_at
    row.attempt_count += attempts
    row.provider_source = result.provider_source
    row.input_fingerprint = input_fingerprint
    row.last_run_id = run.id
    row.last_error_type = error_type if result.status == MatchStatus.ERROR else None
    row.last_error_message = _safe_error_message(error_message) if result.status == MatchStatus.ERROR else None
    row.enriched_at = None if result.status == MatchStatus.ERROR else attempted_at

    _clear_confirmed_identity(row)
    _clear_suggestion(row)
    if result.status == MatchStatus.HIGH_CONFIDENCE:
        _set_confirmed_identity(row, result.confirmed_identity)
    elif result.status in {MatchStatus.REVIEW_NEEDED, MatchStatus.AMBIGUOUS}:
        _set_suggestion(row, result.suggested_identity, result.confidence_score)

    session.flush()
    return row


def compute_input_fingerprint(opportunity: CompanyOpportunity) -> str:
    """Hash only deterministic identity inputs; offer volume is intentionally excluded."""
    payload = {
        "company_key": opportunity.company_key,
        "company_name": normalize_company_key(opportunity.company_name),
        "department_code": opportunity.department_code,
        "communes": sorted(set(opportunity.communes)),
        "location_labels": sorted(
            {" ".join(value.casefold().split()) for value in opportunity.location_labels}
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_fresh_reusable_high_confidence(
    enrichment: Optional[CompanyEnrichment],
    input_fingerprint: str,
    now: datetime,
    ttl: timedelta,
) -> bool:
    if not enrichment or enrichment.match_status != MatchStatus.HIGH_CONFIDENCE:
        return False
    if enrichment.input_fingerprint != input_fingerprint or enrichment.enriched_at is None:
        return False
    enriched_at = enrichment.enriched_at
    if enriched_at.tzinfo is None:
        enriched_at = enriched_at.replace(tzinfo=timezone.utc)
    return now - enriched_at <= ttl


def find_company_enrichment(
    session: Session, company_key: str, provider: str
) -> Optional[CompanyEnrichment]:
    return session.scalar(
        select(CompanyEnrichment).where(
            CompanyEnrichment.company_key == company_key,
            CompanyEnrichment.provider == provider,
        )
    )


def _set_confirmed_identity(row: CompanyEnrichment, identity) -> None:
    row.siren = identity.siren
    row.siret = identity.siret
    row.official_name = identity.official_name
    row.address = identity.address
    row.postal_code = identity.postal_code
    row.commune = identity.commune
    row.naf_code = identity.naf_code
    row.activity_label = identity.activity_label
    row.legal_nature = identity.legal_nature
    row.employee_range = identity.employee_range
    row.administrative_status = identity.administrative_status


def _clear_confirmed_identity(row: CompanyEnrichment) -> None:
    for field_name in (
        "siren", "siret", "official_name", "address", "postal_code", "commune",
        "naf_code", "activity_label", "legal_nature", "employee_range",
        "administrative_status",
    ):
        setattr(row, field_name, None)


def _set_suggestion(row: CompanyEnrichment, identity, score: Optional[float]) -> None:
    if identity is None:
        return
    row.suggested_siren = identity.siren
    row.suggested_name = identity.official_name
    row.suggested_score = score


def _clear_suggestion(row: CompanyEnrichment) -> None:
    row.suggested_siren = None
    row.suggested_name = None
    row.suggested_score = None


def _safe_error_message(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    return " ".join(message.split())[:500]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
