"""Budgeted open-web discovery that never scrapes the resulting job sites."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol, Sequence
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobDiscoveryQueryCache
from app.services.collection.providers import ProviderPage
from app.services.persistence.offers import RecruitmentSignalSnapshot


BRAVE_DISCOVERY_PROVIDER = "brave_search"


class SearchResult(Protocol):
    url: str
    title: Optional[str]
    description: Optional[str]


class SearchClient(Protocol):
    def search(
        self, query: str, *, count: int = 5, company_key: Optional[str] = None,
        request_index: Optional[int] = None,
    ) -> Sequence[SearchResult]: ...


@dataclass(frozen=True)
class CachedSearchResult:
    url: str
    title: Optional[str]
    description: Optional[str]


class CachedSearchClient:
    """Persistent TTL cache in front of the existing ledger-aware Brave client."""

    def __init__(
        self, session: Session, delegate: SearchClient, *, ttl: timedelta = timedelta(hours=24),
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        if ttl.total_seconds() <= 0:
            raise ValueError("cache TTL must be positive")
        self.session = session
        self.delegate = delegate
        self.ttl = ttl
        self.now = now

    def search(
        self, query: str, *, count: int = 5, company_key: Optional[str] = None,
        request_index: Optional[int] = None,
    ) -> tuple[CachedSearchResult, ...]:
        observed_at = _utc(self.now())
        fingerprint = hashlib.sha256(f"{count}:{query}".encode("utf-8")).hexdigest()
        cached = self.session.scalar(select(JobDiscoveryQueryCache).where(
            JobDiscoveryQueryCache.provider == BRAVE_DISCOVERY_PROVIDER,
            JobDiscoveryQueryCache.query_fingerprint == fingerprint,
        ))
        if cached is not None and _utc(cached.fresh_until) >= observed_at:
            return tuple(_cached_result(item) for item in cached.results)
        results = tuple(self.delegate.search(
            query, count=count, company_key=company_key, request_index=request_index,
        ))
        serialized = [
            {"url": item.url, "title": item.title, "description": item.description}
            for item in results
        ]
        if cached is None:
            cached = JobDiscoveryQueryCache(
                provider=BRAVE_DISCOVERY_PROVIDER, query_fingerprint=fingerprint,
                results=serialized, cached_at=observed_at,
                fresh_until=observed_at + self.ttl,
            )
            self.session.add(cached)
        else:
            cached.results = serialized
            cached.cached_at = observed_at
            cached.fresh_until = observed_at + self.ttl
        self.session.commit()
        return tuple(CachedSearchResult(item.url, item.title, item.description) for item in results)


@dataclass(frozen=True)
class DiscoveryQueryPlan:
    """Small deterministic query set; the client enforces the shared Brave ledger."""

    communes: tuple[str, ...] = ("Créteil", "Vitry-sur-Seine", "Ivry-sur-Seine")
    intents: tuple[str, ...] = ('"offre d\'emploi"', '"nous recrutons"')
    site_domains: tuple[str, ...] = (
        "linkedin.com/jobs", "indeed.com", "hellowork.com", "leboncoin.fr",
    )
    max_queries: int = 8

    def queries(self) -> tuple[str, ...]:
        if self.max_queries < 1:
            raise ValueError("max_queries must be positive")
        candidates = [
            f'{intent} "{commune}" "Val-de-Marne"'
            for commune in self.communes for intent in self.intents
        ]
        candidates.extend(
            f'site:{domain} emploi "Val-de-Marne"' for domain in self.site_domains
        )
        return tuple(candidates[: self.max_queries])


class OpenWebJobDiscoveryProvider:
    """Convert search results to clues only; structured offers need later verification."""

    provider_id = BRAVE_DISCOVERY_PROVIDER
    scope_type = "department"
    scope_value = "94"
    can_deactivate_unseen = False

    def __init__(
        self, client: SearchClient, plan: DiscoveryQueryPlan = DiscoveryQueryPlan(),
        *, results_per_query: int = 5,
    ) -> None:
        if results_per_query < 1 or results_per_query > 5:
            raise ValueError("results_per_query must be between 1 and 5")
        self.client = client
        self.plan = plan
        self.results_per_query = results_per_query

    def iter_pages(self) -> Iterable[ProviderPage]:
        queries = self.plan.queries()
        seen: set[str] = set()
        for index, query in enumerate(queries, start=1):
            signals = []
            for result in self.client.search(
                query, count=self.results_per_query, request_index=index
            ):
                if result.url in seen:
                    continue
                seen.add(result.url)
                signals.append(RecruitmentSignalSnapshot(
                    discovery_provider=BRAVE_DISCOVERY_PROVIDER,
                    source=source_from_url(result.url),
                    source_url=result.url,
                    title=result.title,
                    snippet=result.description,
                    department_code="94" if _mentions_94(result.title, result.description) else None,
                ))
            yield ProviderPage(
                page_number=index, signals=tuple(signals),
                is_last=index == len(queries),
            )


def source_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith("linkedin.com"):
        return "linkedin"
    if host.endswith("indeed.com") or host.endswith("indeed.fr"):
        return "indeed"
    if host.endswith("hellowork.com"):
        return "hellowork"
    if host.endswith("leboncoin.fr"):
        return "leboncoin"
    if host.endswith("welcometothejungle.com"):
        return "welcome_to_the_jungle"
    return "employer_career_site"


def _mentions_94(*values: Optional[str]) -> bool:
    text = " ".join(value for value in values if value).casefold()
    return "val-de-marne" in text or "val de marne" in text or any(
        token in text for token in ("créteil", "creteil", "vitry-sur-seine", "ivry-sur-seine")
    )


def _cached_result(value) -> CachedSearchResult:
    return CachedSearchResult(
        url=str(value.get("url", "")),
        title=value.get("title") if isinstance(value.get("title"), str) else None,
        description=(
            value.get("description") if isinstance(value.get("description"), str) else None
        ),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
