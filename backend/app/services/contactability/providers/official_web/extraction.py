"""Deliberately small official-site contact and person extraction."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Sequence
from urllib.parse import urlsplit

from app.services.contactability.contracts import (
    ContactConfidence, ContactEvidenceCandidate, ContactPointCandidate, ContactScope,
    ContactTarget, ContactType, PersonContactCandidate, PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.normalization import (
    normalize_contact_value, normalize_generic, normalize_person_name,
)
from app.services.contactability.providers.official_web.contracts import VerifiedOfficialSite
from app.services.contactability.providers.official_web.contracts import WebsiteVerificationStatus
from app.services.contactability.providers.official_web.discovery import registrable_domain
from app.services.contactability.providers.official_web.fetcher import SecureFetchError


_PAGE_MARKERS = ("contact", "nous-contacter", "mentions", "legal", "equipe", "team", "recrut", "carriere", "emploi", "magasin", "boutique", "store", "agence", "localisation", "location")
_CONTACT_MARKERS = ("contact", "nous-contacter")
_EDITORIAL_MARKERS = ("blog", "actualit", "news", "temoign", "testimonial")
_PERSON_FORBIDDEN_PAGE_MARKERS = (
    "mentions", "legal", "privacy", "confidential", "cgu", "conditions",
)
_ORGANIZATION_NAME_MARKERS = {
    "association", "company", "direction", "groupe", "legal", "legales", "mentions",
    "sarl", "sas", "sasu", "sau", "sa", "scop", "societe", "specific", "specifiques",
}
_PERSON_NAME_FORBIDDEN_TOKENS = {
    "accueil", "aller", "blog", "client", "contact", "contactez", "espace",
    "nous", "prendre", "rendez", "services", "tout", "vous",
}
_PERSON_TITLE_NAVIGATION_MARKERS = (
    "accueil", "blog", "contact", "espace client", "prendre un rendez",
    "prendre rendez", "mentions legales", "mentions légales",
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+33\s?\(?0?\)?|0)[1-9](?:[ .-]?\d{2}){4}(?!\w)")
_PERSON_AFTER = re.compile(r"\b([A-ZÀ-ÖØ-Þ][\w'’-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’-]+){1,3})\s*(?:[-–—,:|]|est)\s*([^.;]{3,100})", re.UNICODE)
_PERSON_BEFORE = re.compile(r"\b((?:directeur|directrice|responsable|président|president|gérant|gerant|rh|drh|recrutement|talent acquisition)[^.:;]{0,80})\s*[:—–-]\s*([A-ZÀ-ÖØ-Þ][\w'’-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’-]+){1,3})", re.IGNORECASE | re.UNICODE)


def extract_official_contacts(
    target: ContactTarget,
    sites: Sequence[VerifiedOfficialSite],
    *, fetcher: object,
    observed_at: datetime,
    max_pages_per_domain: int = 6,
    robots_policy: Optional[object] = None,
) -> tuple[tuple[ContactPointCandidate, ...], tuple[PersonContactCandidate, ...], int, tuple[str, ...]]:
    points: list[ContactPointCandidate] = []
    people: list[PersonContactCandidate] = []
    warnings: list[str] = []
    before = int(getattr(fetcher, "request_count", 0))
    for site in _one_site_per_domain(sites):
        try:
            homepage = fetcher.fetch(site.canonical_url, initial=True)
        except SecureFetchError as exc:
            warnings.append(f"extraction_fetch_{exc.kind}")
            continue
        pages = [homepage]
        for link in _useful_links(homepage.links, site.registrable_domain)[: max(0, max_pages_per_domain - 1)]:
            if robots_policy is None or not robots_policy.allowed(link, "LeadOpportunityFinder/1.0 (+local contact research)"):
                warnings.append("extraction_skip_robots_unverified")
                continue
            try:
                pages.append(fetcher.fetch(link, initial=False))
            except SecureFetchError as exc:
                warnings.append(f"extraction_skip_{exc.kind}")
        for page in pages:
            if not _scope_is_explicit(target, page.text):
                continue
            evidence = _evidence(page.final_url, page.text, observed_at, "official_page_contact")
            for value in _EMAIL.findall(page.text):
                points.append(_point(target, ContactType.EMAIL, value, evidence))
            for value in _PHONE.findall(page.text):
                points.append(_point(target, ContactType.PHONE, value, evidence))
            if _is_contact_url(page.final_url):
                points.append(_point(target, ContactType.PROFESSIONAL_URL, page.final_url, evidence))
            if page is homepage:
                points.append(_point(target, ContactType.WEBSITE, site.canonical_url, evidence))
            if _is_person_source_url(page.final_url):
                people.extend(_people_from_page(target, page.final_url, page.text, observed_at))
    calls = int(getattr(fetcher, "request_count", before)) - before
    return _dedupe_points(points), _dedupe_people(people), max(calls, 0), tuple(dict.fromkeys(warnings))


def _one_site_per_domain(sites):
    seen, result = set(), []
    for site in sites:
        # This is intentionally duplicated with the repository query: direct
        # callers of the extractor must not be able to bypass verification.
        if site.status != WebsiteVerificationStatus.HIGH_CONFIDENCE:
            continue
        if site.registrable_domain not in seen:
            seen.add(site.registrable_domain)
            result.append(site)
    return result


def _useful_links(links, domain):
    selected = []
    for link in links:
        if registrable_domain(link) != domain or _is_editorial_url(link):
            continue
        path = urlsplit(link).path.casefold()
        if any(marker in path for marker in _PAGE_MARKERS):
            selected.append(link)
    return tuple(dict.fromkeys(selected))


def _scope_is_explicit(target, text):
    if target.scope != ContactScope.LOCAL:
        return True
    normalized = normalize_generic(text) or ""
    place = normalize_generic(target.local_commune_snapshot)
    if place and place in normalized:
        return True
    label = normalize_generic(target.local_location_label_snapshot) or ""
    tokens = [token for token in re.findall(r"[\w]+", label) if len(token) >= 4]
    return len(tokens) >= 2 and all(token in normalized for token in tokens[:2])


def _point(target, contact_type, value, evidence):
    normalized = normalize_contact_value(contact_type, value)
    if not normalized:
        return None
    return ContactPointCandidate(
        company_key=target.company_key, organization_name_snapshot=target.organization_name_snapshot,
        scope=target.scope, local_key=target.local_key, siren=target.siren,
        local_commune_snapshot=target.local_commune_snapshot,
        local_location_label_snapshot=target.local_location_label_snapshot,
        contact_type=contact_type, value=value.strip(), normalized_value=normalized,
        confidence_level=ContactConfidence.HIGH_CONFIDENCE,
        verification_status=VerificationStatus.SOURCE_VERIFIED,
        attribution_reason="Coordonnée publique présentée sur un domaine officiel vérifié.", evidence=(evidence,),
    )


def _people_from_page(target, url, text, observed_at):
    result = []
    pairs = [(match.group(1), match.group(2)) for match in _PERSON_AFTER.finditer(text)]
    pairs += [(match.group(2), match.group(1)) for match in _PERSON_BEFORE.finditer(text)]
    for name, title in pairs:
        role = _role(title)
        normalized = normalize_person_name(name)
        if role is None or not _looks_like_person_name(name, target) or not _looks_like_person_title(title):
            continue
        excerpt = _excerpt(text, name)
        evidence = ContactEvidenceCandidate(
            provider="official_web", source_name="Official website", source_url=url,
            observed_at=observed_at, evidence_reason="official_page_person_role", excerpt=excerpt,
        )
        result.append(PersonContactCandidate(
            company_key=target.company_key, organization_name_snapshot=target.organization_name_snapshot,
            scope=target.scope, local_key=target.local_key, siren=target.siren,
            local_commune_snapshot=target.local_commune_snapshot,
            local_location_label_snapshot=target.local_location_label_snapshot,
            full_name=name.strip(), normalized_name=normalized, job_title=title.strip()[:255],
            relevance_role=role, confidence_level=ContactConfidence.HIGH_CONFIDENCE,
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            attribution_reason="Personne et fonction explicitement présentées sur le site officiel.", evidence=(evidence,),
        ))
    return result


def _role(title):
    value = normalize_generic(title) or ""
    if any(key in value for key in ("ressources humaines", "drh", " rh ")):
        return PersonRelevanceRole.HR
    if any(key in value for key in ("recrut", "talent acquisition")):
        return PersonRelevanceRole.RECRUITMENT
    if any(key in value for key in ("president", "président", "gerant", "gérant", "direction generale", "direction générale", "directeur general", "directrice generale")):
        return PersonRelevanceRole.DIRECTOR
    if any(key in value for key in ("responsable", "directeur", "directrice", "manager")):
        return PersonRelevanceRole.MANAGER
    if any(key in value for key in ("fondateur", "fondatrice", "associé", "associe")):
        return PersonRelevanceRole.OTHER
    return None


def _evidence(url, text, observed_at, reason):
    return ContactEvidenceCandidate(provider="official_web", source_name="Official website", source_url=url,
        observed_at=observed_at, evidence_reason=reason, excerpt=_excerpt(text, "@") or _excerpt(text, "0"))


def _excerpt(text, needle):
    index = text.casefold().find(needle.casefold())
    if index < 0:
        return text[:300].strip() or None
    return text[max(0, index - 120): index + 180].strip()[:500] or None


def _is_contact_url(url): return any(marker in urlsplit(url).path.casefold() for marker in _CONTACT_MARKERS)
def _is_editorial_url(url): return any(marker in urlsplit(url).path.casefold() for marker in _EDITORIAL_MARKERS)
def _is_person_source_url(url):
    path = urlsplit(url).path.casefold()
    return not _is_editorial_url(url) and not any(marker in path for marker in _PERSON_FORBIDDEN_PAGE_MARKERS)


def _looks_like_person_name(value, target):
    parts = value.strip().split()
    if not 2 <= len(parts) <= 4 or any(len(part) < 2 or len(part) > 40 for part in parts):
        return False
    # The role-before regex is case-insensitive for role matching, so validate
    # the captured surface form here.  This rejects navigation/text fragments
    # such as "nous Nous apportons" before normalisation erases that signal.
    if any(not re.fullmatch(r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]{1,39}", part) for part in parts):
        return False
    normalized_parts = [part.casefold() for part in parts]
    if any(part in _ORGANIZATION_NAME_MARKERS or part in _PERSON_NAME_FORBIDDEN_TOKENS for part in normalized_parts):
        return False
    organization = normalize_person_name(target.organization_name_snapshot) or ""
    organization_parts = [part for part in organization.split() if len(part) >= 3]
    return not (len(organization_parts) >= 2 and all(part in normalized_parts for part in organization_parts))


def _looks_like_person_title(value):
    normalized = normalize_generic(value) or ""
    if not normalized or len(normalized.split()) > 12:
        return False
    return not any(marker in normalized for marker in _PERSON_TITLE_NAVIGATION_MARKERS)


def _dedupe_points(items): return tuple({(x.contact_type, x.normalized_value, x.scope, x.local_key): x for x in items if x}.values())
def _dedupe_people(items): return tuple({(x.normalized_name, x.scope, x.local_key): x for x in items}.values())
