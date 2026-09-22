"""Pure local commercial-exclusion rules and import contracts."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence

from app.models import CommercialExclusion
from app.services.opportunities.company import normalize_company_key
from sqlalchemy import select
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

EXCLUSION_TYPE_ALIASES = {
    "current_client": ExclusionType.CURRENT_CLIENT,
    "client actuel": ExclusionType.CURRENT_CLIENT,
    "client": ExclusionType.CURRENT_CLIENT,
    "recent_prospect": ExclusionType.RECENT_PROSPECT,
    "prospect récent": ExclusionType.RECENT_PROSPECT,
    "prospect recent": ExclusionType.RECENT_PROSPECT,
    "manual_exclusion": ExclusionType.MANUAL_EXCLUSION,
    "exclusion manuelle": ExclusionType.MANUAL_EXCLUSION,
    "manuel": ExclusionType.MANUAL_EXCLUSION,
}

CSV_HEADER_ALIASES = {
    "company_name": "company_name",
    "entreprise": "company_name",
    "nom_entreprise": "company_name",
    "nom entreprise": "company_name",
    "exclusion_type": "exclusion_type",
    "type": "exclusion_type",
    "type_exclusion": "exclusion_type",
    "type exclusion": "exclusion_type",
    "siren": "siren",
    "reason": "reason",
    "raison": "reason",
    "starts_at": "starts_at",
    "date_debut": "starts_at",
    "date début": "starts_at",
    "expires_at": "expires_at",
    "date_expiration": "expires_at",
    "date expiration": "expires_at",
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
class CommercialExclusionImportRow:
    line_number: int
    values: Mapping[str, object]
    item: Optional[CommercialExclusionInput]
    errors: tuple[str, ...] = ()


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
        exclusion_type = _normalize_exclusion_type(row.get("exclusion_type"))
        if exclusion_type is None:
            raise ValueError("unsupported exclusion_type")
        siren = _optional_string(row.get("siren"))
        validate_siren(siren)
        parsed.append(CommercialExclusionInput(
            company_key=company_key,
            company_name_snapshot=name,
            exclusion_type=exclusion_type,
            siren=siren,
            reason=_optional_string(row.get("reason")),
            starts_at=_parse_optional_datetime(row.get("starts_at")) or starts_at,
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
    validate_siren(item.siren)
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


def parse_exclusion_csv(content: str) -> tuple[CommercialExclusionImportRow, ...]:
    """Parse a small CSV with tolerant French/English headers, one result per line."""
    if not content.strip():
        raise ValueError("Le fichier CSV est vide.")
    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), dialect=dialect)
        if reader.fieldnames is None:
            raise ValueError("Le CSV doit contenir une ligne d’en-têtes.")
        mapped_headers = {
            header: CSV_HEADER_ALIASES.get(header.strip().casefold())
            for header in reader.fieldnames if header is not None
        }
        recognized = {value for value in mapped_headers.values() if value}
        if not {"company_name", "exclusion_type"}.issubset(recognized):
            raise ValueError("Le CSV doit contenir les colonnes company_name et exclusion_type (ou leurs équivalents français).")
        results: list[CommercialExclusionImportRow] = []
        for line_number, raw in enumerate(reader, start=2):
            values = {
                target: raw.get(source)
                for source, target in mapped_headers.items()
                if target is not None
            }
            if not any(isinstance(value, str) and value.strip() for value in values.values()):
                continue
            try:
                item = parse_exclusion_rows((values,))[0]
                _validate_dates(item)
                results.append(CommercialExclusionImportRow(line_number, values, item))
            except ValueError as error:
                results.append(CommercialExclusionImportRow(
                    line_number, values, None, (_translate_validation_error(str(error)),),
                ))
    except csv.Error as error:
        raise ValueError("Le contenu du fichier CSV est invalide.") from error
    if not results:
        raise ValueError("Le CSV ne contient aucune ligne de données.")
    return tuple(results)


def find_duplicate_exclusion(
    session: Session, item: CommercialExclusionInput,
) -> Optional[CommercialExclusion]:
    """Find the same business exclusion without fuzzy matching."""
    candidates = session.scalars(select(CommercialExclusion).where(
        CommercialExclusion.exclusion_type == item.exclusion_type,
        CommercialExclusion.siren == item.siren,
        CommercialExclusion.company_key == item.company_key,
    ))
    expected_start = _as_utc(item.starts_at) if item.starts_at else None
    expected_expiry = _as_utc(item.expires_at) if item.expires_at else None
    expected_reason = item.reason or None
    for row in candidates:
        expiry = _as_utc(row.expires_at) if row.expires_at else None
        start_matches = expected_start is None or _as_utc(row.starts_at) == expected_start
        if start_matches and expiry == expected_expiry and (row.reason or None) == expected_reason:
            return row
    return None


def is_exclusion_active(item: CommercialExclusionRecord, now: Optional[datetime] = None) -> bool:
    return _is_active(item, _as_utc(now or datetime.now(timezone.utc)))


def validate_siren(value: Optional[str]) -> None:
    if value is not None and (len(value) != 9 or not value.isdigit()):
        raise ValueError("siren must contain exactly 9 digits")


def _validate_dates(item: CommercialExclusionInput) -> None:
    starts_at = _as_utc(item.starts_at or datetime.now(timezone.utc))
    if item.exclusion_type != ExclusionType.RECENT_PROSPECT and item.expires_at is not None:
        raise ValueError("expires_at is only supported for recent_prospect")
    if item.expires_at is not None and _as_utc(item.expires_at) <= starts_at:
        raise ValueError("expires_at must be after starts_at")


def _normalize_exclusion_type(value: object) -> Optional[str]:
    normalized = _optional_string(value)
    return EXCLUSION_TYPE_ALIASES.get(normalized.casefold()) if normalized else None


def _translate_validation_error(message: str) -> str:
    translations = {
        "company_name is required": "Le nom de l’entreprise est obligatoire.",
        "company_name must produce a non-empty company key": "Le nom de l’entreprise est invalide.",
        "unsupported exclusion_type": "Le type d’exclusion est invalide.",
        "siren must contain exactly 9 digits": "Le SIREN doit contenir exactement 9 chiffres.",
        "expires_at must be an ISO-8601 string": "La date d’expiration est invalide.",
        "expires_at is only supported for recent_prospect": "Une date d’expiration n’est permise que pour un prospect récent.",
        "expires_at must be after starts_at": "La date d’expiration doit être postérieure à la date de début.",
    }
    return translations.get(message, message)


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
