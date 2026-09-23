"""Budgeted open-web discovery that never scrapes the resulting job sites."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobDiscoveryQueryCache, RecruitmentSignal
from app.services.collection.geography import explicit_commune, is_val_de_marne
from app.services.collection.providers import ProviderPage
from app.services.persistence.offers import RecruitmentSignalSnapshot


BRAVE_DISCOVERY_PROVIDER = "brave_search"


class RecruitmentPageType:
    INDIVIDUAL_JOB_OFFER = "individual_job_offer"
    SEARCH_OR_LISTING = "search_or_listing"
    CAREER_BOARD = "career_board"
    UNKNOWN = "unknown"


PAGE_TYPE_LABELS = {
    RecruitmentPageType.INDIVIDUAL_JOB_OFFER: "Offre individuelle",
    RecruitmentPageType.SEARCH_OR_LISTING: "Page de résultats",
    RecruitmentPageType.CAREER_BOARD: "Site carrière",
    RecruitmentPageType.UNKNOWN: "Type de page inconnu",
}


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


@dataclass(frozen=True)
class OfferGeography:
    location_label: Optional[str] = None
    commune: Optional[str] = None
    department_code: Optional[str] = None
    evidence_rule: Optional[str] = None


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
    ats_domains: tuple[str, ...] = ("boards.greenhouse.io", "jobs.lever.co")
    max_queries: int = 8

    def queries(self) -> tuple[str, ...]:
        if self.max_queries < 1:
            raise ValueError("max_queries must be positive")
        employer_queries = [
            f'{intent} "{commune}"'
            for commune, intent in zip(self.communes, self.intents * len(self.communes))
        ]
        jobboard_queries = [
            f'site:{domain} emploi "Val-de-Marne"' for domain in self.site_domains
        ]
        ats_queries = [
            f'site:{domain} emploi (94 OR "Val-de-Marne")' for domain in self.ats_domains
        ]
        # Keep every small run diverse: employer pages, indexed jobboards and
        # public ATS appear before any family can consume the full cap.
        candidates = [
            *employer_queries[:2], *jobboard_queries[:2], *ats_queries,
            *jobboard_queries[2:], *employer_queries[2:],
        ]
        return tuple(candidates[: self.max_queries])


class OpenWebJobDiscoveryProvider:
    """Convert search results to clues only; structured offers need later verification."""

    provider_id = BRAVE_DISCOVERY_PROVIDER
    scope_type = "department"
    scope_value = "94"
    can_deactivate_unseen = False

    def __init__(
        self, client: SearchClient, plan: DiscoveryQueryPlan = DiscoveryQueryPlan(),
        *, results_per_query: int = 5, max_signals: Optional[int] = None,
        should_stop=lambda: False,
    ) -> None:
        if results_per_query < 1 or results_per_query > 5:
            raise ValueError("results_per_query must be between 1 and 5")
        self.client = client
        self.plan = plan
        self.results_per_query = results_per_query
        self.max_signals = max_signals
        self.should_stop = should_stop

    def iter_pages(self) -> Iterable[ProviderPage]:
        queries = self.plan.queries()
        seen: set[str] = set()
        emitted = 0
        for index, query in enumerate(queries, start=1):
            if self.should_stop():
                yield ProviderPage(page_number=index, is_last=True)
                return
            signals = []
            for result in self.client.search(
                query, count=self.results_per_query, request_index=index
            ):
                if result.url in seen:
                    continue
                seen.add(result.url)
                signals.append(_signal_from_result(result))
                emitted += 1
                if self.max_signals is not None and emitted >= self.max_signals:
                    break
            reached_target = self.max_signals is not None and emitted >= self.max_signals
            yield ProviderPage(
                page_number=index, signals=tuple(signals),
                is_last=index == len(queries) or reached_target,
            )
            if reached_target:
                return


def source_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"
    if re.search(r"(?:^|\.)indeed\.[a-z.]+$", host):
        return "indeed"
    if host == "hellowork.com" or host.endswith(".hellowork.com"):
        return "hellowork"
    if host == "leboncoin.fr" or host.endswith(".leboncoin.fr"):
        return "leboncoin"
    if host == "welcometothejungle.com" or host.endswith(".welcometothejungle.com"):
        return "welcome_to_the_jungle"
    known_jobboards = {
        "jooble.org": "jooble", "meteojob.com": "meteojob", "glassdoor.fr": "glassdoor",
        "francetravail.fr": "france_travail", "directemploi.com": "directemploi",
        "cadremploi.fr": "cadremploi", "la-mairie.com": "other_job_board",
    }
    for domain, source in known_jobboards.items():
        if host == domain or host.endswith(f".{domain}"):
            return source
    return "employer_career_site"


def _mentions_94(*values: Optional[str]) -> bool:
    return is_val_de_marne(" ".join(value for value in values if value))


def _signal_from_result(result: SearchResult) -> RecruitmentSignalSnapshot:
    host = (urlparse(result.url).hostname or "").casefold()
    source = source_from_url(result.url)
    title = normalize_index_text(result.title)
    snippet = normalize_index_text(result.description)
    page_type = classify_page_type(result.url, source=source, title=title)
    geography = extract_offer_geography(page_type, title, snippet)
    company, job_title, extraction_rule = (
        _structured_title(title)
        if page_type == RecruitmentPageType.INDIVIDUAL_JOB_OFFER
        else (None, None, None)
    )
    if page_type in {RecruitmentPageType.SEARCH_OR_LISTING, RecruitmentPageType.CAREER_BOARD}:
        confidence = 0.25
    elif page_type == RecruitmentPageType.INDIVIDUAL_JOB_OFFER:
        confidence = 0.85 if company and job_title and geography.department_code == "94" else 0.45
    else:
        confidence = 0.3
    reason = f"{PAGE_TYPE_LABELS[page_type]} indexée sur {source}"
    if geography.department_code == "94":
        reason += " ; localisation 94 liée à l’offre"
    elif page_type == RecruitmentPageType.INDIVIDUAL_JOB_OFFER:
        reason += " ; localisation 94 non prouvée"
    return RecruitmentSignalSnapshot(
        discovery_provider=BRAVE_DISCOVERY_PROVIDER,
        source=source,
        source_url=result.url,
        domain=host or None,
        page_type=page_type,
        title=title,
        snippet=snippet,
        company_name=company,
        job_title=job_title,
        location_label=geography.location_label,
        commune=geography.commune,
        department_code=geography.department_code,
        confidence=confidence,
        detection_reason=reason,
        extraction={
            "rule": extraction_rule,
            "source_domain": host,
            "page_type": page_type,
            "geography_rule": geography.evidence_rule,
        },
    )


def classify_page_type(
    url: str, *, source: Optional[str] = None, title: Optional[str] = None
) -> str:
    """Classify only URL shapes intended to be stable and publicly visible."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = unquote(parsed.path).casefold().rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    query = parse_qs(parsed.query)
    source = source or source_from_url(url)

    if source == "linkedin":
        return (
            RecruitmentPageType.INDIVIDUAL_JOB_OFFER
            if "/jobs/view/" in f"{path}/" else RecruitmentPageType.SEARCH_OR_LISTING
        )
    if source == "indeed":
        if path.endswith("/viewjob") and query.get("jk"):
            return RecruitmentPageType.INDIVIDUAL_JOB_OFFER
        return RecruitmentPageType.SEARCH_OR_LISTING
    if source == "cadremploi":
        if "liste_offres" in path or path.endswith("/emploi"):
            return RecruitmentPageType.SEARCH_OR_LISTING
        return RecruitmentPageType.UNKNOWN
    if source == "jooble":
        return (
            RecruitmentPageType.SEARCH_OR_LISTING
            if path.startswith("/emploi") else RecruitmentPageType.UNKNOWN
        )
    if host == "jobs.lever.co" or host.endswith(".jobs.lever.co"):
        return (
            RecruitmentPageType.INDIVIDUAL_JOB_OFFER
            if len(segments) >= 2 else RecruitmentPageType.CAREER_BOARD
        )
    if "greenhouse.io" in host:
        if "gh_jid" in query or re.search(r"/jobs?/[^/]+$", path):
            return RecruitmentPageType.INDIVIDUAL_JOB_OFFER
        return RecruitmentPageType.CAREER_BOARD
    if source == "hellowork":
        return (
            RecruitmentPageType.INDIVIDUAL_JOB_OFFER
            if re.search(r"/emplois/[^/]+\.html$", path) else RecruitmentPageType.SEARCH_OR_LISTING
        )
    if source == "leboncoin":
        return (
            RecruitmentPageType.INDIVIDUAL_JOB_OFFER
            if re.search(r"/ad/offres_d_emploi/[^/]+$", path) else RecruitmentPageType.SEARCH_OR_LISTING
        )
    if source == "welcome_to_the_jungle":
        return (
            RecruitmentPageType.INDIVIDUAL_JOB_OFFER
            if re.search(r"/companies/[^/]+/jobs/[^/]+$", path)
            else RecruitmentPageType.SEARCH_OR_LISTING
        )
    if source == "france_travail":
        return (
            RecruitmentPageType.INDIVIDUAL_JOB_OFFER
            if "/offres/recherche/detail/" in f"{path}/"
            else RecruitmentPageType.SEARCH_OR_LISTING
        )
    if source in {"meteojob", "glassdoor", "directemploi", "other_job_board"}:
        return RecruitmentPageType.SEARCH_OR_LISTING

    if re.search(r"/(?:jobs?|careers?|carrieres?|offres-d-emploi)/[^/]+$", path):
        return RecruitmentPageType.INDIVIDUAL_JOB_OFFER
    if path in {"/jobs", "/job", "/careers", "/career", "/carrieres", "/offres-d-emploi"}:
        return RecruitmentPageType.CAREER_BOARD
    normalized_title = (title or "").casefold()
    if re.search(r"\b(?:plus de\s+)?\d[\d\s]*\s+(?:offres? d['’]emploi|emplois?)\b", normalized_title):
        return RecruitmentPageType.SEARCH_OR_LISTING
    return RecruitmentPageType.UNKNOWN


def extract_offer_geography(
    page_type: str, title: Optional[str], snippet: Optional[str]
) -> OfferGeography:
    """Accept geography only from the indexed content of an individual offer."""
    if page_type != RecruitmentPageType.INDIVIDUAL_JOB_OFFER:
        return OfferGeography()
    text = " ".join(value for value in (title, snippet) if value)
    if not text:
        return OfferGeography()
    postal_departments = {match[:2] for match in re.findall(r"(?<!\d)\d{5}(?!\d)", text)}
    parenthesized_departments = {
        match.upper() for match in re.findall(r"\((0[1-9]|[1-8]\d|9[0-5]|2A|2B)\)", text, re.IGNORECASE)
    }
    explicit_departments = postal_departments | parenthesized_departments
    if any(department != "94" for department in explicit_departments):
        return OfferGeography(evidence_rule="conflicting_or_outside_department")
    if not is_val_de_marne(text):
        return OfferGeography(evidence_rule="no_offer_specific_94_location")
    location = _explicit_location(text)
    return OfferGeography(
        location_label=location,
        commune=explicit_commune(text),
        department_code="94",
        evidence_rule="individual_result_text",
    )


def normalize_index_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # Exactly one HTML entity decoding pass: nested/literal entity text is preserved.
    compact = " ".join(html.unescape(value).split()).strip()
    return compact or None


def reconcile_recruitment_signal_quality(session: Session) -> int:
    """Recompute derived quality fields without changing stored URL/title/snippet evidence."""
    changed = 0
    for signal in session.scalars(select(RecruitmentSignal)).all():
        snapshot = _signal_from_result(CachedSearchResult(
            url=signal.source_url, title=signal.title, description=signal.snippet,
        ))
        derived = {
            "source": snapshot.source,
            "domain": snapshot.domain,
            "page_type": snapshot.page_type,
            "company_name": snapshot.company_name,
            "job_title": snapshot.job_title,
            "location_label": snapshot.location_label,
            "commune": snapshot.commune,
            "department_code": snapshot.department_code,
            "confidence": snapshot.confidence,
            "detection_reason": snapshot.detection_reason,
            "extraction": snapshot.extraction or {},
        }
        if any(getattr(signal, name) != value for name, value in derived.items()):
            for name, value in derived.items():
                setattr(signal, name, value)
            changed += 1
    if changed:
        session.commit()
    return changed


def _structured_title(value: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not value:
        return None, None, None
    compact = " ".join(value.split())
    patterns = (
        (r"^(?P<company>.+?)\s+(?:recrute|recherche)\s+(?:(?:pour\s+)?(?:(?:un|une|des)\s+)?postes?\s+de\s+|(?:un|une)\s+)?(?P<job>.+?)(?:\s+[àa]\s+|\s+[-|]\s+)", "company_recruits_job"),
        (r"^(?P<company>.+?)\s+hiring\s+(?:a\s+)?(?P<job>.+?)\s+in\s+", "company_hiring_job"),
    )
    for pattern, rule in patterns:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if match:
            company = match.group("company").strip(" -|")
            job = match.group("job").strip(" -|")
            if company and job:
                return company[:500], job[:500], rule
    return None, None, None


def _explicit_location(value: str) -> Optional[str]:
    commune = explicit_commune(value)
    if commune:
        return commune
    postal = re.search(r"(?<!\d)(94\d{3})(?!\d)", value)
    if postal:
        return postal.group(1)
    return "Val-de-Marne" if "val de marne" in value.casefold().replace("-", " ") else None


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
