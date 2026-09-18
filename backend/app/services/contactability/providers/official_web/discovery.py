"""Deterministic candidate discovery without treating search results as proof."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.services.contactability.contracts import ContactScope, ContactTarget
from app.services.brave_usage import BraveBudgetExceeded
from app.services.contactability.normalization import normalize_generic, normalize_url
from app.services.contactability.providers.official_web.contracts import (
    BraveSearchResult,
    WebsiteCandidate,
    WebsiteCandidateClassification,
    WebsiteSeed,
)


_EXCLUDED_DOMAINS = {
    "facebook.com": "social_network",
    "instagram.com": "social_network",
    "linkedin.com": "social_network",
    "x.com": "social_network",
    "twitter.com": "social_network",
    "youtube.com": "video_platform",
    "google.com": "search_engine",
    "bing.com": "search_engine",
    "indeed.com": "jobboard",
    "hellowork.com": "jobboard",
    "francetravail.fr": "jobboard",
    "pole-emploi.fr": "jobboard",
    "glassdoor.fr": "jobboard",
    "societe.com": "directory",
    "pappers.fr": "directory",
    "verif.com": "directory",
    "pagesjaunes.fr": "directory",
    "annuaire-entreprises.data.gouv.fr": "directory",
}
_THIRD_PARTY_DOMAIN_MARKERS = {
    "annuaire": "directory", "rubypayeur": "financial_directory", "pappers": "legal_data",
    "societe": "legal_data", "verif": "legal_data", "manageo": "company_scoring",
    "infogreffe": "legal_data", "score": "company_scoring", "profile": "profile_marketplace",
    "actualite": "press", "news": "press", "media": "press",
}
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_COMMON_NAME_TOKENS = {
    "sas", "sarl", "sa", "eurl", "societe", "groupe", "france", "entreprise",
    "et", "de", "la", "le", "les", "des", "du",
}
_MULTIPART_SUFFIXES = {"co.uk", "com.au", "co.nz"}


def discover_website_candidates(
    target: ContactTarget,
    *,
    target_fingerprint: str,
    seeds: Sequence[WebsiteSeed],
    brave_client: Optional[object],
    observed_at: datetime,
) -> tuple[tuple[WebsiteCandidate, ...], int, tuple[str, ...]]:
    candidates = [
        _candidate_from_seed(target, target_fingerprint, seed, index)
        for index, seed in enumerate(seeds)
    ]
    search_calls = 0
    warnings: list[str] = []
    candidates = list(_deduplicate_candidates(candidates))
    if _has_serious_candidate(candidates, target):
        return tuple(candidates), search_calls, tuple(warnings)
    if target.scope == ContactScope.LOCAL:
        warnings.append("brave_search_not_used_for_local_target")
        return tuple(candidates), search_calls, tuple(warnings)
    if brave_client is None:
        warnings.append("brave_search_not_configured")
        return tuple(candidates), search_calls, tuple(warnings)

    for query_index, query in enumerate(discovery_queries(target), start=1):
        search_calls += 1
        try:
            results = brave_client.search(
                query, count=5, company_key=target.company_key, request_index=query_index,
            )
        except BraveBudgetExceeded as exc:
            warnings.append(exc.kind)
            break
        for rank, result in enumerate(results, start=1):
            candidate = _candidate_from_search(
                target, target_fingerprint, query, query_index, rank, result, observed_at,
            )
            if candidate is not None:
                candidates.append(candidate)
        candidates = list(_deduplicate_candidates(candidates))
        if _has_serious_candidate(candidates, target):
            break
        if search_calls >= 2:
            break
    return tuple(candidates), search_calls, tuple(warnings)


def discovery_queries(target: ContactTarget) -> tuple[str, str]:
    official_name = target.organization_name_snapshot.strip()
    display_name = (target.display_name_snapshot or official_name).strip()
    location = (
        target.local_commune_snapshot
        or ("France" if target.scope == ContactScope.COMPANY and target.is_multi_local else target.identity_location_snapshot)
        or target.local_location_label_snapshot
        or "France"
    ).strip()
    # A SIREN is a strong verification fact, not a discovery keyword: it pulls
    # company-data directories ahead of the organisation's own domain.
    first = " ".join(part for part in (f'"{display_name}"', f'"{location}"', '"site officiel"') if part)
    second = f'"{display_name}" contact'
    return first, second


def canonicalize_candidate_url(value: str) -> Optional[str]:
    normalized = normalize_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    query = urlencode([
        (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def registrable_domain(value: str) -> Optional[str]:
    try:
        hostname = (urlsplit(value).hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    if not hostname:
        return None
    labels = hostname.split(".")
    if len(labels) <= 2:
        return hostname
    tail = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if tail in _MULTIPART_SUFFIXES else tail


def classify_domain(domain: str) -> tuple[str, tuple[str, ...]]:
    domain = domain.casefold()
    for blocked, category in _EXCLUDED_DOMAINS.items():
        if domain == blocked or domain.endswith(f".{blocked}"):
            return WebsiteCandidateClassification.EXCLUDED, (category,)
    for marker, category in _THIRD_PARTY_DOMAIN_MARKERS.items():
        if marker in domain:
            return WebsiteCandidateClassification.EXCLUDED, (category,)
    if any(token in domain for token in ("tracking", "redirect", "clickserve")):
        return WebsiteCandidateClassification.EXCLUDED, ("tracking_domain",)
    return WebsiteCandidateClassification.POTENTIAL_OFFICIAL, ()


def candidate_set_fingerprint(candidates: Sequence[WebsiteCandidate]) -> str:
    return _fingerprint({"candidates": sorted(item.candidate_fingerprint for item in candidates)})


def _candidate_from_seed(
    target: ContactTarget, target_fingerprint: str, seed: WebsiteSeed, index: int,
) -> WebsiteCandidate:
    canonical = canonicalize_candidate_url(seed.url)
    domain = registrable_domain(canonical or "")
    if not canonical or not domain:
        canonical = seed.url.strip()[:2048]
        domain = "invalid"
        classification, reasons = WebsiteCandidateClassification.EXCLUDED, ("invalid_url",)
    else:
        classification, reasons = classify_domain(domain)
        if classification != WebsiteCandidateClassification.EXCLUDED:
            classification = (
                WebsiteCandidateClassification.STRUCTURED_SOURCE
                if seed.structured else WebsiteCandidateClassification.POTENTIAL_OFFICIAL
            )
    query_fp = _fingerprint({"source": seed.source_provider, "url": canonical})
    return _build_candidate(
        target, target_fingerprint, query_fp, seed.source_provider, 0, index + 1,
        seed.url, canonical, domain, seed.title, seed.snippet, seed.observed_at,
        classification, reasons,
    )


def _candidate_from_search(
    target: ContactTarget,
    target_fingerprint: str,
    query: str,
    query_index: int,
    rank: int,
    result: BraveSearchResult,
    observed_at: datetime,
) -> Optional[WebsiteCandidate]:
    canonical = canonicalize_candidate_url(result.url)
    domain = registrable_domain(canonical or "")
    if not canonical or not domain:
        return None
    classification, reasons = classify_domain(domain)
    return _build_candidate(
        target, target_fingerprint, _fingerprint({"query": query}), "brave_search",
        query_index, rank, result.url, canonical, domain, result.title,
        result.description, observed_at, classification, reasons,
    )


def _build_candidate(
    target: ContactTarget, target_fingerprint: str, query_fingerprint: str,
    provider: str, query_index: int, rank: int, url: str, canonical: str,
    domain: str, title: Optional[str], snippet: Optional[str], observed_at: datetime,
    classification: str, reasons: tuple[str, ...],
) -> WebsiteCandidate:
    fingerprint = _fingerprint({
        "target": target_fingerprint, "canonical_url": canonical,
        "scope": target.scope, "local_key": target.local_key,
    })
    return WebsiteCandidate(
        company_key=target.company_key, target_scope=target.scope, local_key=target.local_key,
        target_fingerprint=target_fingerprint, query_fingerprint=query_fingerprint,
        search_provider=provider, query_index=query_index, result_rank=rank,
        url=url[:2048], canonical_url=canonical, registrable_domain=domain,
        title=_short(title, 500), snippet=_short(snippet, 1000), observed_at=observed_at,
        classification=classification, rejection_reasons=reasons,
        candidate_fingerprint=fingerprint,
    )


def _deduplicate_candidates(candidates: Sequence[WebsiteCandidate]) -> tuple[WebsiteCandidate, ...]:
    ordered = sorted(candidates, key=lambda item: (
        item.classification == WebsiteCandidateClassification.EXCLUDED,
        item.query_index, item.result_rank, item.canonical_url,
    ))
    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    kept = []
    for item in ordered:
        if item.canonical_url in seen_urls or item.registrable_domain in seen_domains:
            continue
        seen_urls.add(item.canonical_url)
        seen_domains.add(item.registrable_domain)
        kept.append(item)
    return tuple(kept)


def _has_serious_candidate(candidates: Sequence[WebsiteCandidate], target: ContactTarget) -> bool:
    name = target.display_name_snapshot or target.organization_name_snapshot
    tokens = _distinctive_tokens(name)
    for item in candidates:
        if item.classification in {
            WebsiteCandidateClassification.STRUCTURED_SOURCE,
            WebsiteCandidateClassification.CACHED_VERIFIED,
        }:
            return True
        haystack = normalize_generic(" ".join(filter(None, (item.registrable_domain, item.title, item.snippet)))) or ""
        if tokens and any(token in haystack for token in tokens):
            return True
    return False


def _distinctive_tokens(value: str) -> tuple[str, ...]:
    normalized = normalize_generic(value) or ""
    return tuple(token for token in re.findall(r"[a-z0-9]+", normalized) if len(token) >= 4 and token not in _COMMON_NAME_TOKENS)


def _short(value: Optional[str], limit: int) -> Optional[str]:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:limit] or None


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
