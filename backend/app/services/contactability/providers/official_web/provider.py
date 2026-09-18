"""Batch-compatible orchestration for official web discovery and verification."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from sqlalchemy.orm import Session

from app.services.contactability.contracts import (
    ContactProviderAttemptMetadata,
    ContactProviderResult,
    ContactProviderStatus,
    ContactTarget,
)
from app.services.contactability.providers.official_web.brave_client import BraveSearchError
from app.services.contactability.providers.official_web.contracts import (
    VerifiedOfficialSite,
    WebsiteCandidate,
    WebsiteSeed,
    WebsiteVerificationStatus,
)
from app.services.contactability.providers.official_web.discovery import (
    candidate_set_fingerprint,
    discover_website_candidates,
)
from app.services.contactability.providers.official_web.extraction import extract_official_contacts
from app.services.contactability.providers.official_web.persistence import OfficialWebRepository
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy
from app.services.contactability.providers.official_web.verification import (
    WebsiteVerificationTechnicalError,
    WebsiteVerificationTransportError,
    verify_website_candidates,
)


PROVIDER_NAME = "official_web"


class OfficialWebProvider:
    """Uses only injected/cached seeds and optional Brave; never invokes Societe.com."""

    name = PROVIDER_NAME
    resources = ("discovery", "verification", "extraction")

    def __init__(
        self,
        *,
        repository: OfficialWebRepository,
        fetcher: object,
        brave_client: Optional[object] = None,
        seed_loader: Callable[[ContactTarget], Sequence[WebsiteSeed]] = lambda target: (),
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        discovery_ttl: timedelta = timedelta(days=30),
        verification_high_ttl: timedelta = timedelta(days=90),
        verification_review_ttl: timedelta = timedelta(days=30),
        extraction_contacts_ttl: timedelta = timedelta(days=30),
        extraction_people_ttl: timedelta = timedelta(days=45),
        extraction_not_found_ttl: timedelta = timedelta(days=14),
        extraction_policy_version: int = 1,
        robots_policy: Optional[object] = None,
    ) -> None:
        self.repository = repository
        self.fetcher = fetcher
        self.brave_client = brave_client
        self.seed_loader = seed_loader
        self.now = now
        self.discovery_ttl = discovery_ttl
        self.verification_high_ttl = verification_high_ttl
        self.verification_review_ttl = verification_review_ttl
        self.extraction_contacts_ttl = extraction_contacts_ttl
        self.extraction_people_ttl = extraction_people_ttl
        self.extraction_not_found_ttl = extraction_not_found_ttl
        self.extraction_policy_version = extraction_policy_version
        self.robots_policy = robots_policy
        if self.robots_policy is None and hasattr(fetcher, "set_robots_checker"):
            self.robots_policy = RobotsTxtPolicy(fetcher)
            fetcher.set_robots_checker(self.robots_policy.allowed)

    @property
    def is_configured(self) -> bool:
        # Core verification works from local/structured seeds without Brave.
        return True

    def target_fingerprint(self, target: ContactTarget) -> str:
        return official_web_target_fingerprint(target)

    def inapplicability_reason(self, target: ContactTarget) -> Optional[str]:
        return None

    def resource_ttl(self, resource: str, status: str) -> timedelta:
        if resource == "discovery":
            return self.discovery_ttl
        if resource == "verification":
            return self.verification_review_ttl
        if resource == "extraction":
            return self.extraction_not_found_ttl if status == ContactProviderStatus.NOT_FOUND else self.extraction_contacts_ttl
        raise ValueError("unknown official_web resource")

    def resource_ttl_for_result(
        self, resource: str, result: ContactProviderResult,
    ) -> timedelta:
        if resource == "verification" and any(
            isinstance(item, VerifiedOfficialSite)
            and item.status == WebsiteVerificationStatus.HIGH_CONFIDENCE
            for item in result.artifacts
        ):
            return self.verification_high_ttl
        if resource == "extraction" and result.person_candidates:
            return self.extraction_people_ttl
        return self.resource_ttl(resource, result.status)

    def resource_input_fingerprint(self, target: ContactTarget, resource: str) -> str:
        if resource == "discovery":
            seeds = tuple(self.repository.load_seeds(target)) + tuple(self.seed_loader(target))
            return _fingerprint({
                "target": self.target_fingerprint(target),
                "resource": resource,
                "seeds": sorted((seed.source_provider, seed.url) for seed in seeds),
                "brave_available": self.brave_client is not None,
                "policy": 1,
            })
        if resource == "verification":
            candidates = self.repository.candidates(self.target_fingerprint(target))
            return _fingerprint({
                "target": self.target_fingerprint(target),
                "resource": resource,
                "candidate_set": candidate_set_fingerprint(candidates),
                "policy": 1,
            })
        if resource == "extraction":
            sites = self.repository.verified_sites_for_extraction(self.target_fingerprint(target))
            return _fingerprint({
                "target": self.target_fingerprint(target), "resource": resource,
                "verified_domains": sorted((site.registrable_domain, site.fingerprint, site.status) for site in sites),
                "policy": self.extraction_policy_version,
            })
        raise ValueError("unknown official_web resource")

    def discover_resource(self, target: ContactTarget, resource: str) -> ContactProviderResult:
        attempted_at = self.now()
        target_fp = self.target_fingerprint(target)
        if resource == "discovery":
            seeds = tuple(self.repository.load_seeds(target)) + tuple(self.seed_loader(target))
            try:
                candidates, calls, warnings = discover_website_candidates(
                    target, target_fingerprint=target_fp, seeds=seeds,
                    brave_client=self.brave_client, observed_at=attempted_at,
                )
            except BraveSearchError as exc:
                return _result(
                    ContactProviderStatus.ERROR, target_fp, attempted_at,
                    request_count=1, error_type=exc.kind,
                    warnings=("brave_search_failed",),
                )
            if not candidates and self.brave_client is None:
                return _result(
                    ContactProviderStatus.NOT_CONFIGURED, target_fp, attempted_at,
                    warnings=("brave_search_not_configured",),
                )
            status = ContactProviderStatus.COMPLETED if candidates else ContactProviderStatus.NOT_FOUND
            return _result(
                status, target_fp, attempted_at, request_count=calls,
                warnings=warnings, artifacts=tuple(candidates),
            )
        if resource == "verification":
            candidates = self.repository.candidates(target_fp)
            if not candidates:
                return _result(ContactProviderStatus.NOT_FOUND, target_fp, attempted_at)
            before = int(getattr(self.fetcher, "request_count", 0))
            try:
                verified = verify_website_candidates(
                    target, candidates, fetcher=self.fetcher, verified_at=attempted_at,
                )
            except (WebsiteVerificationTransportError, WebsiteVerificationTechnicalError) as exc:
                calls = int(getattr(self.fetcher, "request_count", before)) - before
                return _result(
                    ContactProviderStatus.ERROR, target_fp, attempted_at,
                    request_count=max(calls, 0), error_type=exc.kind,
                    warnings=(
                        "website_verification_transport_error"
                        if isinstance(exc, WebsiteVerificationTransportError)
                        else "website_verification_technical_error",
                    ),
                )
            calls = int(getattr(self.fetcher, "request_count", before)) - before
            return _result(
                ContactProviderStatus.COMPLETED, target_fp, attempted_at,
                request_count=max(calls, 0), artifacts=verified,
            )
        if resource == "extraction":
            sites = self.repository.verified_sites_for_extraction(target_fp)
            if not sites:
                return _result(ContactProviderStatus.NOT_FOUND, target_fp, attempted_at)
            points, people, calls, warnings = extract_official_contacts(
                target, sites, fetcher=self.fetcher, observed_at=attempted_at,
                robots_policy=self.robots_policy,
            )
            return ContactProviderResult(
                provider=PROVIDER_NAME,
                status=ContactProviderStatus.COMPLETED if (points or people) else ContactProviderStatus.NOT_FOUND,
                candidates=points, person_candidates=people, warnings=warnings,
                metadata=ContactProviderAttemptMetadata(
                    target_fingerprint=target_fp, attempted_at=attempted_at, request_count=calls,
                ),
            )
        raise ValueError("unknown official_web resource")

    def persist_resource_result(
        self, session: Session, target: ContactTarget, resource: str,
        result: ContactProviderResult,
    ) -> None:
        if resource == "discovery":
            candidates = tuple(item for item in result.artifacts if isinstance(item, WebsiteCandidate))
            self.repository.replace_candidates(self.target_fingerprint(target), candidates)
            return
        if resource == "verification":
            verified = tuple(item for item in result.artifacts if isinstance(item, VerifiedOfficialSite))
            self.repository.persist_verified(
                verified, high_ttl=self.verification_high_ttl,
                review_ttl=self.verification_review_ttl,
            )
            return
        if resource == "extraction":
            from app.services.contactability.persistence import persist_contact_provider_result
            persist_contact_provider_result(session, result)
            return
        raise ValueError("unknown official_web resource")


def official_web_target_fingerprint(target: ContactTarget) -> str:
    return _fingerprint({
        "provider": PROVIDER_NAME,
        "company_key": target.company_key,
        "scope": target.scope,
        "local_key": target.local_key,
        "siren": target.siren,
        "organization": target.organization_name_snapshot,
        "display_name": target.display_name_snapshot,
        "identity_location": target.identity_location_snapshot,
    })


def _result(
    status, target_fingerprint, attempted_at, *, request_count=0, error_type=None,
    warnings=(), artifacts=(),
):
    return ContactProviderResult(
        provider=PROVIDER_NAME, status=status, warnings=tuple(warnings),
        artifacts=tuple(artifacts),
        metadata=ContactProviderAttemptMetadata(
            target_fingerprint=target_fingerprint, attempted_at=attempted_at,
            request_count=request_count, error_type=error_type,
        ),
    )


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
