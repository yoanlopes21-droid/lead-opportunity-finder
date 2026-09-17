"""Conservative, local-only contact candidates extracted from offer descriptions."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import replace
from typing import Iterable, Optional, Sequence

from sqlalchemy.orm import Session

from app.models import ContactPoint
from app.services.contactability.contracts import (
    ContactConfidence,
    ContactEvidenceCandidate,
    ContactEvidenceInput,
    ContactPointCandidate,
    ContactPointInput,
    ContactProviderResult,
    ContactProviderStatus,
    ContactScope,
    ContactTarget,
    ContactType,
    OfferContactSource,
    VerificationStatus,
)
from app.services.contactability.normalization import (
    normalize_contact_value,
    normalize_generic,
    normalize_url,
)
from app.services.contactability.persistence import add_contact_evidence, upsert_contact_point
from app.services.scoring.company import EmployerRelationshipStatus


_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+)(?![\w.-])",
    re.IGNORECASE,
)
_PHONE_PATTERN = re.compile(
    r"(?<!\d)((?:\+33\s*(?:\(0\)\s*)?|0)\s*[1-9](?:[ .-]?\d{2}){4})(?!\d)"
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_RESERVED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "invalid"}
_EXCERPT_MAX_LENGTH = 240


class OfferDescriptionContactProvider:
    """Extract low-confidence candidates without asserting an employer contact."""

    name = "offer_description"

    def discover(
        self, target: ContactTarget, offers: Sequence[OfferContactSource]
    ) -> ContactProviderResult:
        candidates: "OrderedDict[tuple[str, str, str, Optional[str]], ContactPointCandidate]" = OrderedDict()
        for offer in offers:
            description = offer.description
            if not isinstance(description, str) or not description.strip():
                continue
            for contact_type, raw_value, start, end in _extract_values(description):
                normalized_value = normalize_contact_value(contact_type, raw_value)
                if normalized_value is None:
                    continue
                if contact_type == ContactType.EMAIL and _reserved_example_email(normalized_value):
                    continue
                if contact_type in {ContactType.WEBSITE, ContactType.PROFESSIONAL_URL}:
                    offer_url = normalize_url(offer.source_url)
                    if offer_url is not None and normalized_value == offer_url:
                        continue
                scope, confidence, reason, local_target = _attribution(target, description, start, end)
                candidate = ContactPointCandidate(
                    company_key=target.company_key,
                    organization_name_snapshot=target.organization_name_snapshot,
                    scope=scope,
                    contact_type=contact_type,
                    value=raw_value,
                    normalized_value=normalized_value,
                    confidence_level=confidence,
                    verification_status=VerificationStatus.UNVERIFIED,
                    attribution_reason=reason,
                    local_key=local_target.local_key if local_target else None,
                    siren=(
                        target.siren
                        if scope in {ContactScope.COMPANY, ContactScope.INTERMEDIARY}
                        else None
                    ),
                    local_commune_snapshot=(
                        local_target.local_commune_snapshot if local_target else None
                    ),
                    local_location_label_snapshot=(
                        local_target.local_location_label_snapshot if local_target else None
                    ),
                    evidence=(ContactEvidenceCandidate(
                        provider=self.name,
                        source_name="France Travail offer description",
                        source_url=offer.source_url,
                        source_identifier=offer.source_offer_id,
                        observed_at=offer.last_seen_at,
                        evidence_reason=f"offer_description_{contact_type}",
                        excerpt=_short_excerpt(description, start, end),
                    ),),
                )
                key = (contact_type, normalized_value, scope, candidate.local_key)
                existing = candidates.get(key)
                if existing is None:
                    candidates[key] = candidate
                else:
                    candidates[key] = replace(
                        existing,
                        evidence=_distinct_evidence(existing.evidence + candidate.evidence),
                    )
        status = ContactProviderStatus.COMPLETED if candidates else ContactProviderStatus.NOT_FOUND
        return ContactProviderResult(
            provider=self.name,
            status=status,
            candidates=tuple(candidates.values()),
            warnings=_provider_warnings(target),
        )


def persist_offer_description_candidates(
    session: Session, result: ContactProviderResult
) -> tuple[ContactPoint, ...]:
    """Persist already-reviewed provider output through generic contactability services."""
    if result.provider != OfferDescriptionContactProvider.name:
        raise ValueError("result does not belong to offer_description provider")
    persisted: list[ContactPoint] = []
    for candidate in result.candidates:
        point = upsert_contact_point(session, ContactPointInput(
            company_key=candidate.company_key,
            organization_name_snapshot=candidate.organization_name_snapshot,
            scope=candidate.scope,
            contact_type=candidate.contact_type,
            value=candidate.value,
            confidence_level=candidate.confidence_level,
            verification_status=candidate.verification_status,
            observed_at=max(evidence.observed_at for evidence in candidate.evidence),
            local_key=candidate.local_key,
            siren=candidate.siren,
            local_commune_snapshot=candidate.local_commune_snapshot,
            local_location_label_snapshot=candidate.local_location_label_snapshot,
            attribution_reason=candidate.attribution_reason,
        ))
        for evidence in candidate.evidence:
            add_contact_evidence(session, ContactEvidenceInput(
                provider=evidence.provider,
                source_name=evidence.source_name,
                observed_at=evidence.observed_at,
                source_url=evidence.source_url,
                source_identifier=evidence.source_identifier,
                evidence_reason=evidence.evidence_reason,
                excerpt=evidence.excerpt,
            ), contact_point_id=point.id)
        persisted.append(point)
    return tuple(persisted)


def merge_offer_description_results(
    results: Iterable[ContactProviderResult],
) -> ContactProviderResult:
    """Merge target-level output without losing distinct offer-level evidence."""
    candidates: "OrderedDict[tuple[str, str, str, Optional[str]], ContactPointCandidate]" = OrderedDict()
    warnings: list[str] = []
    for result in results:
        if result.provider != OfferDescriptionContactProvider.name:
            raise ValueError("result does not belong to offer_description provider")
        warnings.extend(result.warnings)
        for candidate in result.candidates:
            key = (
                candidate.contact_type,
                candidate.normalized_value,
                candidate.scope,
                candidate.local_key,
            )
            existing = candidates.get(key)
            candidates[key] = candidate if existing is None else replace(
                existing,
                evidence=_distinct_evidence(existing.evidence + candidate.evidence),
            )
    return ContactProviderResult(
        provider=OfferDescriptionContactProvider.name,
        status=(ContactProviderStatus.COMPLETED if candidates else ContactProviderStatus.NOT_FOUND),
        candidates=tuple(candidates.values()),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _extract_values(description: str) -> Iterable[tuple[str, str, int, int]]:
    for match in _EMAIL_PATTERN.finditer(description):
        yield ContactType.EMAIL, match.group(1), match.start(1), match.end(1)
    for match in _PHONE_PATTERN.finditer(description):
        yield ContactType.PHONE, match.group(1), match.start(1), match.end(1)
    for match in _URL_PATTERN.finditer(description):
        value = match.group(0).rstrip(".,;:!?")
        if value:
            yield ContactType.WEBSITE, value, match.start(0), match.start(0) + len(value)


def _attribution(
    target: ContactTarget, description: str, start: int, end: int
) -> tuple[str, str, str, Optional[ContactTarget]]:
    relationship = target.employer_relationship_status
    if relationship == EmployerRelationshipStatus.INTERMEDIARY:
        return (
            ContactScope.INTERMEDIARY,
            ContactConfidence.REVIEW_NEEDED,
            "offer_description_contact_for_intermediary",
            None,
        )
    if relationship == EmployerRelationshipStatus.INTERMEDIARY_SUSPECTED:
        return (
            ContactScope.UNKNOWN,
            ContactConfidence.AMBIGUOUS,
            "offer_description_contact_intermediary_suspected",
            None,
        )
    if target.scope == ContactScope.LOCAL and _has_explicit_local_reference(target, description, start, end):
        return (
            ContactScope.LOCAL,
            ContactConfidence.REVIEW_NEEDED,
            "offer_description_contact_explicit_local_reference",
            target,
        )
    if target.scope == ContactScope.COMPANY and _has_explicit_company_reference(target, description, start, end):
        return (
            ContactScope.COMPANY,
            ContactConfidence.REVIEW_NEEDED,
            "offer_description_contact_explicit_company_reference",
            None,
        )
    return (
        ContactScope.UNKNOWN,
        ContactConfidence.AMBIGUOUS,
        "offer_description_contact_unresolved_attribution",
        None,
    )


def _has_explicit_company_reference(
    target: ContactTarget, description: str, start: int, end: int
) -> bool:
    organization = normalize_generic(target.organization_name_snapshot)
    if not organization or len(organization) < 4:
        return False
    context = normalize_generic(_short_excerpt(description, start, end, radius=180)) or ""
    return organization in context


def _has_explicit_local_reference(
    target: ContactTarget, description: str, start: int, end: int
) -> bool:
    label = normalize_generic(target.local_location_label_snapshot)
    if not label:
        return False
    meaningful_tokens = [
        token for token in re.findall(r"\w+", label)
        if len(token) >= 4 and not token.isdigit()
    ]
    if not meaningful_tokens:
        return False
    context = normalize_generic(_short_excerpt(description, start, end, radius=180)) or ""
    local_cue = re.search(r"\b(?:agence|magasin|site|bureau|adresse|contact)\b", context)
    return bool(local_cue and all(token in context for token in meaningful_tokens))


def _reserved_example_email(value: str) -> bool:
    return value.rsplit("@", 1)[-1] in _RESERVED_EMAIL_DOMAINS


def _short_excerpt(description: str, start: int, end: int, radius: int = 100) -> str:
    left = max(0, start - radius)
    right = min(len(description), end + radius)
    excerpt = " ".join(description[left:right].split())
    if left:
        excerpt = f"…{excerpt}"
    if right < len(description):
        excerpt = f"{excerpt}…"
    return excerpt[:_EXCERPT_MAX_LENGTH]


def _distinct_evidence(
    evidence: tuple[ContactEvidenceCandidate, ...]
) -> tuple[ContactEvidenceCandidate, ...]:
    deduplicated: OrderedDict[tuple[Optional[str], Optional[str], Optional[str]], ContactEvidenceCandidate] = OrderedDict()
    for item in evidence:
        key = (item.source_identifier, item.source_url, item.excerpt)
        deduplicated.setdefault(key, item)
    return tuple(deduplicated.values())


def _provider_warnings(target: ContactTarget) -> tuple[str, ...]:
    warnings = list(target.warnings)
    warnings.append("Les valeurs extraites d'une description d'offre ne sont pas des contacts confirmés.")
    return tuple(dict.fromkeys(warnings))
