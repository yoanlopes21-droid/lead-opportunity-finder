"""Optional Societe.com API Pro provider; no name-based identity resolution."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ContactPoint, PersonContact
from app.services.company_enrichment.contracts import MatchStatus
from app.services.contactability.contracts import (
    ContactConfidence,
    ContactEvidenceCandidate,
    ContactEvidenceInput,
    ContactPointCandidate,
    ContactPointInput,
    ContactProviderAttemptMetadata,
    ContactProviderResult,
    ContactProviderStatus,
    ContactScope,
    ContactTarget,
    ContactType,
    OfferContactSource,
    PersonContactCandidate,
    PersonContactInput,
    PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.normalization import (
    normalize_email,
    normalize_generic,
    normalize_person_name,
    normalize_phone,
    normalize_url,
)
from app.services.contactability.persistence import (
    add_contact_evidence,
    upsert_contact_point,
    upsert_person_contact,
)


DEFAULT_BASE_URL = "https://api.societe.com/api/v1"
PROVIDER_NAME = "societe_com"
_SIREN_PATTERN = re.compile(r"^\d{9}$")
_DIRECTOR_MARKERS = (
    "president", "gerant", "directeur general", "directrice generale",
    "managing director",
)


class SocieteComClientError(RuntimeError):
    """Sanitized client failure which never includes credentials or raw payloads."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class SocieteComPolicy:
    contact_ttl: timedelta = timedelta(days=30)
    directors_ttl: timedelta = timedelta(days=90)


@dataclass(frozen=True)
class PersistedSocieteComResult:
    contact_points: tuple[ContactPoint, ...]
    person_contacts: tuple[PersonContact, ...]


class SocieteComClient:
    """Small authenticated client limited to the two contactability routes."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        requests_per_second: float = 1.0,
        requester: Optional[Callable[..., Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Societe.com API token is required")
        if timeout_seconds <= 0 or requests_per_second <= 0:
            raise ValueError("timeout and rate limit must be positive")
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._minimum_interval = 1.0 / requests_per_second
        self._requester = requester or httpx.get
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._last_request_at: Optional[float] = None

    def fetch_contact(self, numid: str) -> Optional[Mapping[str, Any]]:
        return self._get(f"/entreprise/{_validated_numid(numid)}/contact")

    def fetch_directors(self, numid: str) -> Optional[Any]:
        return self._get(f"/entreprise/{_validated_numid(numid)}/dirigeants")

    def logical_url(self, numid: str, route: str) -> str:
        return f"{self._base_url}/entreprise/{_validated_numid(numid)}/{route}"

    def _get(self, route: str) -> Optional[Any]:
        self._respect_rate_limit()
        try:
            response = self._requester(
                f"{self._base_url}{route}",
                headers={"X-Authorization": f"socapi {self._token}"},
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SocieteComClientError("timeout", "Societe.com request timed out") from exc
        except httpx.HTTPError as exc:
            raise SocieteComClientError("network", "Societe.com request failed") from exc
        finally:
            self._last_request_at = self._monotonic()

        status_code = int(getattr(response, "status_code", 0))
        if status_code in {204, 404}:
            return None
        if status_code == 429:
            raise SocieteComClientError("rate_limited", "Societe.com rate limit reached")
        if 500 <= status_code <= 599:
            raise SocieteComClientError("server_error", "Societe.com service error")
        if not 200 <= status_code <= 299:
            raise SocieteComClientError("http_error", "Societe.com request was rejected")
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise SocieteComClientError("invalid_response", "Societe.com returned invalid JSON") from exc

    def _respect_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self._minimum_interval - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleeper(remaining)


class SocieteComContactProvider:
    """Map structured API facts only for an already-confirmed company identity."""

    name = PROVIDER_NAME
    resources = ("contact", "directors")

    def __init__(
        self,
        *,
        token: Optional[str],
        client: Optional[SocieteComClient] = None,
        policy: SocieteComPolicy = SocieteComPolicy(),
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        requests_per_second: float = 1.0,
    ) -> None:
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._client = client
        if self._token and self._client is None:
            self._client = SocieteComClient(
                token=self._token,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                requests_per_second=requests_per_second,
            )
        self.policy = policy
        self._now = now
        self._base_url = base_url.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return self._token is not None

    def target_fingerprint(self, target: ContactTarget) -> str:
        return societe_com_target_fingerprint(target)

    def inapplicability_reason(self, target: ContactTarget) -> Optional[str]:
        return societe_com_inapplicability_reason(target)

    def resource_ttl(self, resource: str, status: str) -> timedelta:
        if status == ContactProviderStatus.NOT_FOUND:
            return timedelta(days=30)
        if resource == "contact":
            return self.policy.contact_ttl
        if resource == "directors":
            return self.policy.directors_ttl
        raise ValueError("unknown Societe.com contact resource")

    def discover_resource(
        self, target: ContactTarget, resource: str,
    ) -> ContactProviderResult:
        """Discover exactly one paid resource so the batch can cache it independently."""
        if resource not in self.resources:
            raise ValueError("unknown Societe.com contact resource")
        attempted_at = self._now()
        fingerprint = self.target_fingerprint(target)
        if not self.is_configured:
            return _empty_result(
                ContactProviderStatus.NOT_CONFIGURED, fingerprint, attempted_at,
                warning="societe_com_token_not_configured",
            )
        reason = self.inapplicability_reason(target)
        if reason is not None:
            return _empty_result(
                ContactProviderStatus.NOT_APPLICABLE, fingerprint, attempted_at,
                warning=reason,
            )
        assert self._client is not None and target.siren is not None
        try:
            payload = (
                self._client.fetch_contact(target.siren)
                if resource == "contact"
                else self._client.fetch_directors(target.siren)
            )
        except SocieteComClientError as exc:
            return ContactProviderResult(
                provider=self.name,
                status=ContactProviderStatus.ERROR,
                warnings=(f"societe_com_{exc.kind}",),
                metadata=ContactProviderAttemptMetadata(
                    target_fingerprint=fingerprint,
                    attempted_at=attempted_at,
                    request_count=1,
                    error_type=exc.kind,
                ),
            )
        if resource == "contact":
            candidates, warnings = self._contact_candidates(
                target, _unwrap_record(payload), attempted_at,
            )
            return ContactProviderResult(
                provider=self.name,
                status=(ContactProviderStatus.COMPLETED if candidates else ContactProviderStatus.NOT_FOUND),
                candidates=candidates,
                warnings=tuple(warnings),
                metadata=ContactProviderAttemptMetadata(
                    target_fingerprint=fingerprint, attempted_at=attempted_at, request_count=1,
                ),
            )
        persons, warnings = self._director_candidates(target, payload, attempted_at)
        return ContactProviderResult(
            provider=self.name,
            status=(ContactProviderStatus.COMPLETED if persons else ContactProviderStatus.NOT_FOUND),
            person_candidates=persons,
            warnings=tuple(warnings),
            metadata=ContactProviderAttemptMetadata(
                target_fingerprint=fingerprint, attempted_at=attempted_at, request_count=1,
            ),
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, client: Optional[SocieteComClient] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> "SocieteComContactProvider":
        token = (
            settings.societe_com_api_token.get_secret_value()
            if settings.societe_com_api_token is not None else None
        )
        return cls(
            token=token,
            client=client,
            now=now,
            base_url=settings.societe_com_api_url,
            timeout_seconds=settings.societe_com_timeout_seconds,
            requests_per_second=settings.societe_com_requests_per_second,
            policy=SocieteComPolicy(
                contact_ttl=timedelta(days=settings.societe_com_contact_ttl_days),
                directors_ttl=timedelta(days=settings.societe_com_directors_ttl_days),
            ),
        )

    def discover(
        self, target: ContactTarget, offers: Sequence[OfferContactSource] = (),
    ) -> ContactProviderResult:
        del offers
        attempted_at = self._now()
        fingerprint = societe_com_target_fingerprint(target)
        if self._token is None:
            return _empty_result(
                ContactProviderStatus.NOT_CONFIGURED, fingerprint, attempted_at,
                warning="societe_com_token_not_configured",
            )
        reason = societe_com_inapplicability_reason(target)
        if reason is not None:
            return _empty_result(
                ContactProviderStatus.NOT_APPLICABLE, fingerprint, attempted_at,
                warning=reason,
            )
        assert self._client is not None and target.siren is not None
        request_count = 0
        try:
            request_count += 1
            contact_payload = self._client.fetch_contact(target.siren)
            request_count += 1
            directors_payload = self._client.fetch_directors(target.siren)
        except SocieteComClientError as exc:
            return ContactProviderResult(
                provider=self.name,
                status=ContactProviderStatus.ERROR,
                warnings=(f"societe_com_{exc.kind}",),
                metadata=ContactProviderAttemptMetadata(
                    target_fingerprint=fingerprint,
                    attempted_at=attempted_at,
                    request_count=request_count,
                    error_type=exc.kind,
                ),
            )

        contact_record = _unwrap_record(contact_payload)
        points, contact_warnings = self._contact_candidates(
            target, contact_record, attempted_at,
        )
        persons, director_warnings = self._director_candidates(
            target, directors_payload, attempted_at,
        )
        status = (
            ContactProviderStatus.COMPLETED
            if points or persons else ContactProviderStatus.NOT_FOUND
        )
        return ContactProviderResult(
            provider=self.name,
            status=status,
            candidates=points,
            person_candidates=persons,
            warnings=tuple(dict.fromkeys(contact_warnings + director_warnings)),
            metadata=ContactProviderAttemptMetadata(
                target_fingerprint=fingerprint,
                attempted_at=attempted_at,
                request_count=2,
            ),
        )

    def _contact_candidates(
        self, target: ContactTarget, record: Optional[Mapping[str, Any]], observed_at: datetime,
    ) -> tuple[tuple[ContactPointCandidate, ...], list[str]]:
        if not record:
            return (), []
        if _explicit_false(record.get("diffusible")) or _explicit_false(record.get("actif")):
            return (), ["societe_com_contact_not_diffusible_or_inactive"]
        identity = _identity_assessment(target, record)
        if identity.reject:
            return (), [identity.warning]
        candidates: "OrderedDict[tuple[str, str], ContactPointCandidate]" = OrderedDict()
        fields = (
            ("siteweb", ContactType.WEBSITE, _normalized_website),
            ("tel", ContactType.PHONE, normalize_phone),
            ("email", ContactType.EMAIL, normalize_email),
        )
        for field, contact_type, normalizer in fields:
            raw = _text(record.get(field))
            normalized = normalizer(raw)
            if raw is None or normalized is None:
                continue
            evidence = ContactEvidenceCandidate(
                provider=self.name,
                source_name="Societe.com API Pro",
                source_url=self._logical_url(target.siren, "contact"),
                source_identifier=target.siren,
                observed_at=observed_at,
                evidence_reason=f"societe_com_contact_{field}",
            )
            candidate = ContactPointCandidate(
                company_key=target.company_key,
                organization_name_snapshot=target.organization_name_snapshot,
                scope=ContactScope.COMPANY,
                contact_type=contact_type,
                value=normalized if contact_type == ContactType.WEBSITE else raw,
                normalized_value=normalized,
                confidence_level=identity.confidence,
                verification_status=VerificationStatus.SOURCE_VERIFIED,
                attribution_reason=identity.reason,
                siren=target.siren,
                evidence=(evidence,),
            )
            candidates[(contact_type, normalized)] = candidate
        return tuple(candidates.values()), ([identity.warning] if identity.warning else [])

    def _director_candidates(
        self, target: ContactTarget, payload: Optional[Any], observed_at: datetime,
    ) -> tuple[tuple[PersonContactCandidate, ...], list[str]]:
        records, container = _unwrap_directors(payload)
        root_identity = _identity_assessment(target, container) if container else None
        if root_identity is not None and root_identity.reject:
            return (), [root_identity.warning]
        people: "OrderedDict[str, PersonContactCandidate]" = OrderedDict()
        warnings: list[str] = []
        for record in records:
            entry_identity = _identity_assessment(target, record)
            if entry_identity.reject:
                warnings.append(entry_identity.warning)
                continue
            if not _is_physical_person(record):
                continue
            full_name = _director_name(record)
            normalized_name = normalize_person_name(full_name)
            if full_name is None or normalized_name is None:
                continue
            job_title = _first_text(record, "fonction", "qualite", "mandat")
            confidence = (
                ContactConfidence.HIGH_CONFIDENCE
                if (root_identity is None or root_identity.confidence == ContactConfidence.HIGH_CONFIDENCE)
                and entry_identity.confidence == ContactConfidence.HIGH_CONFIDENCE
                else ContactConfidence.REVIEW_NEEDED
            )
            evidence = ContactEvidenceCandidate(
                provider=self.name,
                source_name="Societe.com API Pro",
                source_url=self._logical_url(target.siren, "dirigeants"),
                source_identifier=target.siren,
                observed_at=observed_at,
                evidence_reason="societe_com_company_director",
            )
            people[normalized_name] = PersonContactCandidate(
                company_key=target.company_key,
                organization_name_snapshot=target.organization_name_snapshot,
                scope=ContactScope.COMPANY,
                full_name=full_name,
                normalized_name=normalized_name,
                job_title=job_title,
                relevance_role=_director_role(job_title),
                confidence_level=confidence,
                verification_status=VerificationStatus.SOURCE_VERIFIED,
                attribution_reason="societe_com_director_for_confirmed_siren",
                siren=target.siren,
                evidence=(evidence,),
            )
        return tuple(people.values()), warnings

    def _logical_url(self, siren: Optional[str], route: str) -> str:
        assert siren is not None
        if self._client is not None:
            return self._client.logical_url(siren, route)
        return f"{self._base_url}/entreprise/{siren}/{route}"


@dataclass(frozen=True)
class _IdentityAssessment:
    confidence: str
    reason: str
    reject: bool = False
    warning: str = ""


def societe_com_inapplicability_reason(target: ContactTarget) -> Optional[str]:
    if target.scope != ContactScope.COMPANY:
        return "societe_com_requires_company_scope"
    if not isinstance(target.siren, str) or not _SIREN_PATTERN.fullmatch(target.siren):
        return "societe_com_requires_confirmed_siren"
    if target.identity_match_status != MatchStatus.HIGH_CONFIDENCE:
        return "societe_com_requires_high_confidence_identity"
    return None


def societe_com_target_fingerprint(target: ContactTarget) -> str:
    payload = {
        "provider": PROVIDER_NAME,
        "company_key": normalize_generic(target.company_key),
        "organization_name": normalize_generic(target.organization_name_snapshot),
        "scope": target.scope,
        "siren": target.siren,
        "identity_match_status": target.identity_match_status,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def persist_societe_com_result(
    session: Session, result: ContactProviderResult,
) -> PersistedSocieteComResult:
    """Persist explicit provider output; discovery itself never writes."""
    if result.provider != PROVIDER_NAME:
        raise ValueError("result does not belong to societe_com provider")
    points: list[ContactPoint] = []
    persons: list[PersonContact] = []
    for candidate in result.person_candidates:
        observed_at = max(evidence.observed_at for evidence in candidate.evidence)
        person = upsert_person_contact(session, PersonContactInput(
            company_key=candidate.company_key,
            organization_name_snapshot=candidate.organization_name_snapshot,
            scope=candidate.scope,
            full_name=candidate.full_name,
            relevance_role=candidate.relevance_role,
            confidence_level=candidate.confidence_level,
            verification_status=candidate.verification_status,
            observed_at=observed_at,
            local_key=candidate.local_key,
            siren=candidate.siren,
            local_commune_snapshot=candidate.local_commune_snapshot,
            local_location_label_snapshot=candidate.local_location_label_snapshot,
            job_title=candidate.job_title,
            attribution_reason=candidate.attribution_reason,
        ))
        _persist_evidence(session, candidate.evidence, person_contact_id=person.id)
        persons.append(person)
    for candidate in result.candidates:
        observed_at = max(evidence.observed_at for evidence in candidate.evidence)
        point = upsert_contact_point(session, ContactPointInput(
            company_key=candidate.company_key,
            organization_name_snapshot=candidate.organization_name_snapshot,
            scope=candidate.scope,
            contact_type=candidate.contact_type,
            value=candidate.value,
            confidence_level=candidate.confidence_level,
            verification_status=candidate.verification_status,
            observed_at=observed_at,
            local_key=candidate.local_key,
            siren=candidate.siren,
            local_commune_snapshot=candidate.local_commune_snapshot,
            local_location_label_snapshot=candidate.local_location_label_snapshot,
            attribution_reason=candidate.attribution_reason,
        ))
        _persist_evidence(session, candidate.evidence, contact_point_id=point.id)
        points.append(point)
    return PersistedSocieteComResult(tuple(points), tuple(persons))


def _persist_evidence(
    session: Session, evidence_items: Sequence[ContactEvidenceCandidate],
    *, contact_point_id: Optional[int] = None, person_contact_id: Optional[int] = None,
) -> None:
    for evidence in evidence_items:
        add_contact_evidence(session, ContactEvidenceInput(
            provider=evidence.provider,
            source_name=evidence.source_name,
            observed_at=evidence.observed_at,
            source_url=evidence.source_url,
            source_identifier=evidence.source_identifier,
            evidence_reason=evidence.evidence_reason,
            excerpt=evidence.excerpt,
        ), contact_point_id=contact_point_id, person_contact_id=person_contact_id)


def _identity_assessment(
    target: ContactTarget, record: Mapping[str, Any],
) -> _IdentityAssessment:
    returned_siren = _returned_siren(record)
    if returned_siren is not None and returned_siren != target.siren:
        return _IdentityAssessment(
            confidence=ContactConfidence.AMBIGUOUS,
            reason="societe_com_identifier_mismatch",
            reject=True,
            warning="societe_com_identifier_mismatch",
        )
    returned_name = _first_text(record, "raisonsociale", "denomination", "nom_entreprise")
    target_name = normalize_generic(target.organization_name_snapshot)
    if returned_name and normalize_generic(returned_name) != target_name:
        return _IdentityAssessment(
            confidence=ContactConfidence.AMBIGUOUS,
            reason="societe_com_name_requires_review",
            warning="societe_com_organization_name_mismatch",
        )
    if returned_siren == target.siren and returned_name:
        return _IdentityAssessment(
            confidence=ContactConfidence.HIGH_CONFIDENCE,
            reason="societe_com_contact_matches_confirmed_siren_and_name",
        )
    return _IdentityAssessment(
        confidence=ContactConfidence.REVIEW_NEEDED,
        reason="societe_com_contact_matches_route_but_identity_fields_are_incomplete",
        warning="societe_com_identity_fields_incomplete",
    )


def _returned_siren(record: Mapping[str, Any]) -> Optional[str]:
    siren = re.sub(r"\D", "", str(record.get("siren", "")))
    if len(siren) == 9:
        return siren
    siret = re.sub(r"\D", "", str(record.get("siret", "")))
    return siret[:9] if len(siret) == 14 else None


def _unwrap_record(payload: Optional[Any]) -> Optional[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return None
    for key in ("data", "result", "entreprise", "contact"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            return nested
    return payload


def _unwrap_directors(
    payload: Optional[Any],
) -> tuple[tuple[Mapping[str, Any], ...], Optional[Mapping[str, Any]]]:
    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, Mapping)), None
    if not isinstance(payload, Mapping):
        return (), None
    for key in ("dirigeants", "mandataires"):
        values = payload.get(key)
        if isinstance(values, list):
            return tuple(item for item in values if isinstance(item, Mapping)), payload
    for key in ("data", "result"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return tuple(item for item in nested if isinstance(item, Mapping)), payload
        if isinstance(nested, Mapping):
            records, _ = _unwrap_directors(nested)
            return records, payload
    return (), payload


def _is_physical_person(record: Mapping[str, Any]) -> bool:
    kind = normalize_generic(_first_text(record, "type", "type_personne", "nature"))
    if kind:
        if kind in {"pp", "personne physique", "physique"} or "personne physique" in kind:
            return True
        if "morale" in kind or kind in {"pm", "personne morale"}:
            return False
    return bool(
        _first_text(record, "prenom", "first_name")
        and _first_text(record, "nom", "last_name")
        and not _first_text(record, "raisonsociale", "denomination", "societe")
    )


def _director_name(record: Mapping[str, Any]) -> Optional[str]:
    first_name = _first_text(record, "prenom", "first_name")
    last_name = _first_text(record, "nom", "last_name")
    if first_name and last_name:
        return f"{first_name} {last_name}"
    return _first_text(record, "nom_complet", "identite", "full_name")


def _director_role(job_title: Optional[str]) -> str:
    normalized = normalize_person_name(job_title) or ""
    return (
        PersonRelevanceRole.DIRECTOR
        if any(marker in normalized for marker in _DIRECTOR_MARKERS)
        else PersonRelevanceRole.OTHER
    )


def _normalized_website(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = normalize_url(value)
    if normalized is not None:
        return normalized
    candidate = value.strip()
    if re.fullmatch(r"(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?", candidate):
        return normalize_url(f"https://{candidate}")
    return None


def _first_text(record: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = _text(record.get(key))
        if value:
            return value
    return None


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _explicit_false(value: Any) -> bool:
    return value is False or (isinstance(value, str) and value.strip().casefold() in {"false", "0", "non"})


def _validated_numid(value: str) -> str:
    normalized = re.sub(r"\D", "", value) if isinstance(value, str) else ""
    if len(normalized) not in {9, 14}:
        raise ValueError("numid must be a SIREN or SIRET")
    return normalized


def _empty_result(
    status: str, fingerprint: str, attempted_at: datetime, *, warning: str,
) -> ContactProviderResult:
    return ContactProviderResult(
        provider=PROVIDER_NAME,
        status=status,
        warnings=(warning,),
        metadata=ContactProviderAttemptMetadata(
            target_fingerprint=fingerprint,
            attempted_at=attempted_at,
        ),
    )
