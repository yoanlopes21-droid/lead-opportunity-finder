"""Pure normalisation and identity fingerprints for contactability facts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


def normalize_generic(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized) or None


def normalize_person_name(value: Optional[str]) -> Optional[str]:
    normalized = normalize_generic(value)
    if not normalized:
        return None
    without_accents = "".join(
        character
        for character in unicodedata.normalize("NFKD", normalized)
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", without_accents)).strip() or None


def normalize_email(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.count("@") != 1 or any(character.isspace() for character in candidate):
        return None
    local_part, domain = candidate.split("@")
    if not local_part or not domain or "." not in domain:
        return None
    return f"{local_part.casefold()}@{domain.casefold()}"


def normalize_phone(value: Optional[str], country_code: str = "33") -> Optional[str]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    prefix = "+" if raw.startswith("+") else ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("00"):
        return f"+{digits[2:]}" if len(digits) > 2 else None
    if prefix:
        if digits.startswith(f"{country_code}0"):
            return f"+{country_code}{digits[len(country_code) + 1:]}"
        return f"+{digits}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"+{country_code}{digits[1:]}"
    return digits


def normalize_url(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold()
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def contact_point_fingerprint(
    *, company_key: str, scope: str, local_key: Optional[str], contact_type: str,
    normalized_value: str, person_contact_id: Optional[int],
) -> str:
    return _fingerprint({
        "company_key": normalize_generic(company_key),
        "scope": scope,
        "local_key": normalize_generic(local_key),
        "contact_type": contact_type,
        "normalized_value": normalized_value,
        "person_contact_id": person_contact_id,
    })


def person_contact_fingerprint(
    *, company_key: str, scope: str, local_key: Optional[str], normalized_name: str,
) -> str:
    return _fingerprint({
        "company_key": normalize_generic(company_key),
        "scope": scope,
        "local_key": normalize_generic(local_key),
        "normalized_name": normalized_name,
    })


def contact_evidence_fingerprint(
    *, target_kind: str, target_id: int, provider: str, source_name: str,
    source_url: Optional[str], source_identifier: Optional[str],
    evidence_reason: Optional[str], excerpt: Optional[str],
) -> str:
    return _fingerprint({
        "target_kind": target_kind,
        "target_id": target_id,
        "provider": normalize_generic(provider),
        "source_name": normalize_generic(source_name),
        "source_url": normalize_url(source_url) or normalize_generic(source_url),
        "source_identifier": normalize_generic(source_identifier),
        "evidence_reason": normalize_generic(evidence_reason),
        "excerpt": normalize_generic(excerpt),
    })


def normalize_contact_value(contact_type: str, value: Optional[str]) -> Optional[str]:
    if contact_type == "email":
        return normalize_email(value)
    if contact_type == "phone":
        return normalize_phone(value)
    if contact_type in {"website", "professional_url"}:
        return normalize_url(value)
    return normalize_generic(value)


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
