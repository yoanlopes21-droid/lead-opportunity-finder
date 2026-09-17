"""Pure local commercial-exclusion rules and a future-import input contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence

from app.models import CommercialExclusion
from app.services.opportunities.company import normalize_company_key
from sqlalchemy.orm import Session


class ExclusionType:
    CURRENT_CLIENT = "current_client"
    RECENT_PROSPECT = "recent_prospect"
    MANUAL_EXCLUSION = "manual_exclusion"


VALID_EXCLUSION_TYPES = {
    ExclusionType.CURRENT_CLIENT,
    ExclusionType.RECENT_PROSPECT,
    ExclusionType.MANUAL_EXCLUSION,
}


@dataclass(frozen=True)
class CommercialExclusionInput:
    company_key: str
    company_name_snapshot: str
    exclusion_type: str
    siren: Optional[str] = None
    reason: Optional[str] = None
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class CommercialExclusionRecord:
    id: int
    company_key: str
    siren: Optional[str]
    company_name_snapshot: str
    exclusion_type: str
    reason: Optional[str]
    starts_at: datetime
    expires_at: Optional[datetime]
    created_at: Optional[datetime]

    @classmethod
    def from_model(cls, row: CommercialExclusion) -> "CommercialExclusionRecord":
        return cls(
            id=row.id,
            company_key=row.company_key,
            siren=row.siren,
            company_name_snapshot=row.company_name_snapshot,
            exclusion_type=row.exclusion_type,
            reason=row.reason,
            starts_at=row.starts_at,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )


@dataclass(frozen=True)
class ExclusionTarget:
    company_key: str
    siren: Optional[str] = None


@dataclass(frozen=True)
class ExclusionDecision:
    is_eligible: bool
    exclusion: Optional[CommercialExclusionRecord] = None


def evaluate_eligibility(
    target: ExclusionTarget,
    exclusions: Sequence[CommercialExclusionRecord],
    now: Optional[datetime] = None,
) -> ExclusionDecision:
    """Apply exact SIREN-first matching without mutating the source data."""
    observed_at = _as_utc(now or datetime.now(timezone.utc))
    if target.siren:
        matching = [item for item in exclusions if item.siren == target.siren]
    else:
        matching = [item for item in exclusions if item.company_key == target.company_key]
    active = [item for item in matching if _is_active(item, observed_at)]
    if not active:
        return ExclusionDecision(is_eligible=True)
    return ExclusionDecision(is_eligible=False, exclusion=min(active, key=_priority_key))


def parse_exclusion_rows(
    rows: Iterable[Mapping[str, object]],
    starts_at: Optional[datetime] = None,
) -> tuple[CommercialExclusionInput, ...]:
    """Validate a future CSV/XLSX adapter's simple, provider-neutral rows.

    Expected columns are company_name, optional siren, exclusion_type, optional
    expires_at and optional reason. This parser neither reads files nor persists.
    """
    parsed: list[CommercialExclusionInput] = []
    for row in rows:
        name = _required_string(row.get("company_name"), "company_name")
        company_key = normalize_company_key(name)
        if company_key is None:
            raise ValueError("company_name must produce a non-empty company key")
        exclusion_type = _required_string(row.get("exclusion_type"), "exclusion_type")
        if exclusion_type not in VALID_EXCLUSION_TYPES:
            raise ValueError("unsupported exclusion_type")
        parsed.append(CommercialExclusionInput(
            company_key=company_key,
            company_name_snapshot=name,
            exclusion_type=exclusion_type,
            siren=_optional_string(row.get("siren")),
            reason=_optional_string(row.get("reason")),
            starts_at=starts_at,
            expires_at=_parse_optional_datetime(row.get("expires_at")),
        ))
    return tuple(parsed)


def create_commercial_exclusion(
    session: Session,
    item: CommercialExclusionInput,
    now: Optional[datetime] = None,
) -> CommercialExclusion:
    """Persist one validated local exclusion; callers choose when to commit."""
    if item.exclusion_type not in VALID_EXCLUSION_TYPES:
        raise ValueError("unsupported exclusion_type")
    expected_key = normalize_company_key(item.company_name_snapshot)
    if expected_key is None or item.company_key != expected_key:
        raise ValueError("company_key must exactly match the normalized company name")
    starts_at = _as_utc(item.starts_at or now or datetime.now(timezone.utc))
    if item.expires_at is not None and _as_utc(item.expires_at) <= starts_at:
        raise ValueError("expires_at must be after starts_at")
    row = CommercialExclusion(
        company_key=item.company_key,
        siren=item.siren,
        company_name_snapshot=item.company_name_snapshot,
        exclusion_type=item.exclusion_type,
        reason=item.reason,
        starts_at=starts_at,
        expires_at=_as_utc(item.expires_at) if item.expires_at else None,
    )
    session.add(row)
    session.flush()
    return row


def _is_active(item: CommercialExclusionRecord, now: datetime) -> bool:
    if _as_utc(item.starts_at) > now:
        return False
    if item.exclusion_type == ExclusionType.CURRENT_CLIENT:
        return True
    return item.expires_at is None or _as_utc(item.expires_at) > now


def _priority_key(item: CommercialExclusionRecord) -> tuple[int, datetime, int]:
    priority = {
        ExclusionType.CURRENT_CLIENT: 0,
        ExclusionType.RECENT_PROSPECT: 1,
        ExclusionType.MANUAL_EXCLUSION: 2,
    }.get(item.exclusion_type, 3)
    return (priority, _as_utc(item.starts_at), item.id)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _required_string(value: object, field_name: str) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_string(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_optional_datetime(value: object) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("expires_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("expires_at must be an ISO-8601 string") from error
    return _as_utc(parsed)
