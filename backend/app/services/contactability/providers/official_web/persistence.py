"""Persistence for minimal official-web artifacts, never fetched page bodies."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import (
    ContactEvidence,
    ContactPoint,
    VerifiedWebsiteRecord,
    WebsiteCandidateRecord,
    WebsiteVerificationSignalRecord,
)
from app.services.contactability.contracts import ContactTarget, ContactType
from app.services.contactability.providers.official_web.contracts import (
    VerifiedOfficialSite,
    WebsiteCandidate,
    WebsiteCandidateClassification,
    WebsiteSeed,
    WebsiteVerificationStatus,
)


def ensure_official_web_schema(engine: Engine) -> None:
    WebsiteCandidateRecord.__table__.create(bind=engine, checkfirst=True)
    VerifiedWebsiteRecord.__table__.create(bind=engine, checkfirst=True)
    WebsiteVerificationSignalRecord.__table__.create(bind=engine, checkfirst=True)


class OfficialWebRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_seeds(self, target: ContactTarget) -> tuple[WebsiteSeed, ...]:
        seeds: list[WebsiteSeed] = []
        verified = self.session.scalars(select(VerifiedWebsiteRecord).where(
            VerifiedWebsiteRecord.target_fingerprint == _target_reference(target),
            VerifiedWebsiteRecord.status == WebsiteVerificationStatus.HIGH_CONFIDENCE,
        )).all()
        for item in verified:
            seeds.append(WebsiteSeed(
                url=item.canonical_url, source_provider="official_web_cache",
                observed_at=item.verified_at, structured=True,
            ))
        points = self.session.scalars(select(ContactPoint).where(
            ContactPoint.company_key == target.company_key,
            ContactPoint.contact_type == ContactType.WEBSITE,
            ContactPoint.is_active.is_(True),
        )).all()
        for point in points:
            provider = self.session.scalar(select(ContactEvidence.provider).where(
                ContactEvidence.contact_point_id == point.id,
            ).order_by(ContactEvidence.id))
            if provider not in {"societe_com", "offer_description"}:
                continue
            if point.scope != target.scope or point.local_key != target.local_key:
                continue
            seeds.append(WebsiteSeed(
                url=point.normalized_value, source_provider=provider,
                observed_at=point.last_observed_at,
                structured=provider == "societe_com",
            ))
        return tuple(seeds)

    def replace_candidates(
        self, target_fingerprint: str, candidates: Sequence[WebsiteCandidate],
    ) -> None:
        existing = self.session.scalars(select(WebsiteCandidateRecord).where(
            WebsiteCandidateRecord.target_fingerprint == target_fingerprint,
        )).all()
        by_fingerprint = {item.candidate_fingerprint: item for item in existing}
        for item in existing:
            item.is_active = False
        for candidate in candidates:
            record = by_fingerprint.get(candidate.candidate_fingerprint)
            if record is None:
                record = WebsiteCandidateRecord(candidate_fingerprint=candidate.candidate_fingerprint)
                self.session.add(record)
            _copy_candidate(record, candidate)
            record.is_active = True
        self.session.flush()

    def candidates(self, target_fingerprint: str) -> tuple[WebsiteCandidate, ...]:
        records = self.session.scalars(select(WebsiteCandidateRecord).where(
            WebsiteCandidateRecord.target_fingerprint == target_fingerprint,
            WebsiteCandidateRecord.is_active.is_(True),
        ).order_by(
            WebsiteCandidateRecord.query_index,
            WebsiteCandidateRecord.result_rank,
            WebsiteCandidateRecord.canonical_url,
        )).all()
        return tuple(_candidate_from_record(item) for item in records)

    def persist_verified(
        self, results: Sequence[VerifiedOfficialSite], *, high_ttl: timedelta, review_ttl: timedelta,
    ) -> None:
        for result in results:
            record = self.session.scalar(select(VerifiedWebsiteRecord).where(
                VerifiedWebsiteRecord.fingerprint == result.fingerprint,
            ))
            if record is None:
                record = VerifiedWebsiteRecord(fingerprint=result.fingerprint)
                self.session.add(record)
            ttl = high_ttl if result.status == WebsiteVerificationStatus.HIGH_CONFIDENCE else review_ttl
            _copy_verified(record, result, fresh_until=result.verified_at + ttl)
            self.session.flush()
            for signal in result.signals:
                fingerprint = _fingerprint({
                    "verified": result.fingerprint, "type": signal.signal_type,
                    "url": signal.source_url, "observed": signal.observed_value,
                    "expected": signal.expected_value,
                })
                evidence = self.session.scalar(select(WebsiteVerificationSignalRecord).where(
                    WebsiteVerificationSignalRecord.fingerprint == fingerprint,
                ))
                if evidence is None:
                    evidence = WebsiteVerificationSignalRecord(
                        verified_website_id=record.id, fingerprint=fingerprint,
                        signal_type=signal.signal_type, polarity=signal.polarity,
                        weight=signal.weight, reason=signal.reason,
                        source_url=signal.source_url, excerpt=signal.excerpt,
                        observed_value=signal.observed_value, expected_value=signal.expected_value,
                        observed_at=signal.observed_at,
                    )
                    self.session.add(evidence)
        self.session.flush()


def _copy_candidate(record, item):
    for field in (
        "company_key", "target_scope", "local_key", "target_fingerprint",
        "query_fingerprint", "search_provider", "query_index", "result_rank", "url",
        "canonical_url", "registrable_domain", "title", "snippet", "observed_at",
        "classification",
    ):
        setattr(record, field, getattr(item, field))
    record.rejection_reasons = list(item.rejection_reasons)


def _copy_verified(record, item, *, fresh_until):
    for field in (
        "company_key", "target_scope", "local_key", "target_fingerprint",
        "candidate_fingerprint", "candidate_set_fingerprint", "provider", "canonical_url",
        "registrable_domain", "status", "score", "observed_at", "verified_at",
    ):
        setattr(record, field, getattr(item, field))
    record.rejection_reasons = list(item.rejection_reasons)
    record.attribution_warnings = list(item.attribution_warnings)
    record.fresh_until = fresh_until


def _candidate_from_record(item):
    return WebsiteCandidate(
        company_key=item.company_key, target_scope=item.target_scope, local_key=item.local_key,
        target_fingerprint=item.target_fingerprint, query_fingerprint=item.query_fingerprint,
        search_provider=item.search_provider, query_index=item.query_index,
        result_rank=item.result_rank, url=item.url, canonical_url=item.canonical_url,
        registrable_domain=item.registrable_domain, title=item.title, snippet=item.snippet,
        observed_at=item.observed_at, classification=item.classification,
        rejection_reasons=tuple(item.rejection_reasons or ()),
        candidate_fingerprint=item.candidate_fingerprint,
    )


def _target_reference(target: ContactTarget) -> str:
    # Kept local to avoid importing a provider and accidentally coupling execution.
    return _fingerprint({
        "provider": "official_web", "company_key": target.company_key,
        "scope": target.scope, "local_key": target.local_key, "siren": target.siren,
        "organization": target.organization_name_snapshot,
        "display_name": target.display_name_snapshot,
        "identity_location": target.identity_location_snapshot,
    })


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
