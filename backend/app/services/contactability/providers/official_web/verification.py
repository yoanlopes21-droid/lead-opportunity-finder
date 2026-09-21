"""Conservative, explainable verification of official website hypotheses."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Optional, Sequence
from urllib.parse import urlsplit

from app.services.contactability.contracts import ContactScope, ContactTarget
from app.services.contactability.normalization import normalize_generic
from app.services.contactability.providers.official_web.contracts import (
    SignalPolarity,
    VerifiedOfficialSite,
    WebsiteCandidate,
    WebsiteCandidateClassification,
    WebsiteVerificationSignal,
    WebsiteVerificationStatus,
)
from app.services.contactability.providers.official_web.discovery import (
    candidate_set_fingerprint,
    registrable_domain,
)
from app.services.contactability.providers.official_web.fetcher import SecureFetchError


_LEGAL_LINK_MARKERS = ("mentions-legales", "mentions_legales", "legal", "impressum")
_CONTACT_LINK_MARKERS = ("contact", "nous-contacter", "nous_contacter")
_LEGAL_SUFFIXES = {"sas", "sarl", "sa", "eurl", "sasu", "scop", "societe", "groupe", "france"}
_SIREN_CONTEXT = re.compile(r"(?i)\b(?:siren|siret|rcs)\b.{0,50}?((?:\d[ .-]?){9,14})")
_THIRD_PARTY_PROFILE_MARKERS = (
    "fiche entreprise", "annuaire", "donnees legales", "données légales",
    "informations legales", "informations légales", "entreprises similaires",
    "rechercher une entreprise", "score de solvabilite", "score de solvabilité",
)
_PROFILE_PATH_MARKERS = ("/societe/", "/entreprise/", "/company/", "/fiche/")
_OPERATOR_PATTERN = re.compile(
    r"(?is)(?:editeur|éditeur|exploite par|exploité par|propulse par|propulsé par)\s*[:\-]?\s*([^.;]{2,120})"
)
TRANSIENT_FETCH_ERROR_KINDS = frozenset({
    "dns_error", "timeout", "network", "connection_reset", "rate_limited", "server_error",
})
TECHNICAL_FETCH_ERROR_KINDS = frozenset({"response_too_large", "unsupported_content"})


class WebsiteVerificationTransportError(RuntimeError):
    """The candidate could not be assessed because transport was unavailable."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"website verification transport failed: {kind}")
        self.kind = kind


class WebsiteVerificationTechnicalError(RuntimeError):
    """The page was reached but could not be safely assessed as HTML."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"website verification technical failure: {kind}")
        self.kind = kind


def verify_website_candidates(
    target: ContactTarget,
    candidates: Sequence[WebsiteCandidate],
    *,
    fetcher: object,
    verified_at: datetime,
    max_candidates: int = 3,
    max_pages_per_domain: int = 4,
) -> tuple[VerifiedOfficialSite, ...]:
    candidate_fp = candidate_set_fingerprint(candidates)
    ordered = sorted(candidates, key=lambda item: (
        item.classification == WebsiteCandidateClassification.EXCLUDED,
        item.query_index, item.result_rank, item.registrable_domain,
    ))[:max_candidates]
    results = [
        _verify_one(
            target, item, candidate_fp, fetcher, verified_at, max_pages_per_domain,
        )
        for item in ordered
    ]
    viable = sorted(
        [item for item in results if item.status != WebsiteVerificationStatus.REJECTED],
        key=lambda item: (-item.score, item.registrable_domain),
    )
    if len(viable) >= 2 and viable[0].score - viable[1].score <= 10:
        competing = {viable[0].fingerprint, viable[1].fingerprint}
        results = [
            _replace_status(item, WebsiteVerificationStatus.AMBIGUOUS, "competing_domains")
            if item.fingerprint in competing and item.status != WebsiteVerificationStatus.REJECTED
            else item
            for item in results
        ]
    return tuple(results)


def _verify_one(
    target: ContactTarget,
    candidate: WebsiteCandidate,
    candidate_set_fp: str,
    fetcher: object,
    verified_at: datetime,
    max_pages: int,
) -> VerifiedOfficialSite:
    if candidate.classification == WebsiteCandidateClassification.EXCLUDED:
        return _result(
            target, candidate, candidate_set_fp, WebsiteVerificationStatus.REJECTED,
            0, (), candidate.rejection_reasons, verified_at,
        )
    try:
        homepage = fetcher.fetch(candidate.canonical_url, initial=True)
    except SecureFetchError as exc:
        if exc.kind in TRANSIENT_FETCH_ERROR_KINDS:
            raise WebsiteVerificationTransportError(exc.kind) from exc
        if exc.kind in TECHNICAL_FETCH_ERROR_KINDS:
            raise WebsiteVerificationTechnicalError(exc.kind) from exc
        return _result(
            target, candidate, candidate_set_fp, WebsiteVerificationStatus.REJECTED,
            0, (), (f"fetch_{exc.kind}",), verified_at,
        )
    pages = [homepage]
    followups = _verification_links(homepage.links, candidate.registrable_domain)
    for url in followups[: max(0, max_pages - 1)]:
        try:
            pages.append(fetcher.fetch(url, initial=False))
        except SecureFetchError:
            continue

    signals = _build_signals(target, candidate, pages, verified_at)
    rejection_reasons = []
    third_party_reason = _third_party_reason(target, candidate, pages)
    if third_party_reason:
        rejection_reasons.append(third_party_reason)
    if any(signal.signal_type == "siren_conflict" for signal in signals):
        rejection_reasons.append("siren_conflict")
    if any(signal.signal_type == "identity_conflict" for signal in signals):
        rejection_reasons.append("identity_conflict")
    identity_score = max(0, min(100, sum(signal.weight for signal in signals)))
    if rejection_reasons:
        status = WebsiteVerificationStatus.REJECTED
        # Signals still document why a third-party page described the target,
        # but a rejected domain has no usable official-site confidence.
        score = 0
    else:
        score = identity_score
        families = _positive_families(signals)
        exact_siren = any(signal.signal_type == "siren_exact" for signal in signals)
        # Identity facts prove that a page is about an organisation.  They do
        # not prove that the organisation operates the domain.  High confidence
        # therefore requires independent ownership/officiality evidence.
        sufficient_identity = exact_siren or "identity" in families
        if score >= 80 and "ownership" in families and sufficient_identity:
            status = WebsiteVerificationStatus.HIGH_CONFIDENCE
        elif score >= 50:
            status = WebsiteVerificationStatus.REVIEW_NEEDED
        else:
            status = WebsiteVerificationStatus.AMBIGUOUS
    return _result(
        target, candidate, candidate_set_fp, status, score, signals,
        tuple(rejection_reasons), verified_at,
    )


def _build_signals(target, candidate, pages, observed_at):
    signals: list[WebsiteVerificationSignal] = []
    combined = " ".join(page.text for page in pages)
    normalized_text = normalize_generic(combined) or ""
    sirens = _extract_sirens(combined)
    if target.siren:
        if target.siren in sirens:
            signals.append(_signal(
                "siren_exact", 70, "SIREN/SIRET exact visible sur le site.",
                _source_for_value(pages, target.siren), target.siren, target.siren, observed_at,
            ))
        conflicting = sorted(value for value in sirens if value != target.siren)
        if conflicting and target.siren not in sirens:
            signals.append(_signal(
                "siren_conflict", -100, "Identifiant juridique visible incompatible avec la cible.",
                pages[0].final_url, conflicting[0], target.siren, observed_at,
                polarity=SignalPolarity.NEGATIVE,
            ))
    official = normalize_generic(target.organization_name_snapshot) or ""
    display = normalize_generic(target.display_name_snapshot) or official
    if _domain_brand_matches(target, candidate.registrable_domain):
        signals.append(_signal(
            "domain_brand_match", 35, "Domaine lexicalement cohérent avec la marque ou raison sociale.",
            pages[0].final_url, candidate.registrable_domain,
            target.display_name_snapshot or target.organization_name_snapshot, observed_at,
        ))
    if official and official in normalized_text:
        signals.append(_signal(
            "legal_name_exact", 30, "Raison sociale exacte visible.", pages[0].final_url,
            target.organization_name_snapshot, target.organization_name_snapshot, observed_at,
        ))
    if (
        _distinctive_name_match(display, normalized_text)
        and (not official or display != official)
    ):
        signals.append(_signal(
            "distinctive_name", 20, "Nom ou marque distinctive cohérente.", pages[0].final_url,
            target.display_name_snapshot or target.organization_name_snapshot,
            target.display_name_snapshot or target.organization_name_snapshot, observed_at,
        ))
    homepage_text = normalize_generic(pages[0].text) or ""
    if _target_identity_matches(target, homepage_text):
        signals.append(_signal(
            "homepage_brand_identity", 20, "Identité de la cible visible sur la page d'accueil du domaine.",
            pages[0].final_url, target.display_name_snapshot or target.organization_name_snapshot,
            target.display_name_snapshot or target.organization_name_snapshot, observed_at,
        ))

    location = target.local_commune_snapshot or target.identity_location_snapshot
    normalized_location = normalize_generic(location)
    if normalized_location and normalized_location in normalized_text:
        signal_type = "local_location_exact" if target.scope == ContactScope.LOCAL else "geography_match"
        weight = 25 if target.scope == ContactScope.LOCAL else 20
        signals.append(_signal(
            signal_type, weight, "Localisation explicitement cohérente.",
            _source_for_value(pages, normalized_location), location, location, observed_at,
        ))
    legal_pages = [page for page in pages[1:] if _is_legal_url(page.final_url)]
    if legal_pages:
        signals.append(_signal(
            "internal_legal_page", 15, "Mentions légales internes accessibles sur le même domaine.",
            legal_pages[0].final_url, "mentions légales", "mentions légales", observed_at,
        ))
        legal_text = normalize_generic(" ".join(page.text for page in legal_pages)) or ""
        if _target_identity_matches(target, legal_text):
            signals.append(_signal(
                "legal_operator_target", 35,
                "Les mentions légales internes identifient la cible ou sa marque comme opérateur du site.",
                legal_pages[0].final_url, target.organization_name_snapshot,
                target.organization_name_snapshot, observed_at,
            ))
    return tuple(signals)


def _extract_sirens(text: str) -> set[str]:
    found = set()
    for match in _SIREN_CONTEXT.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) == 9:
            found.add(digits)
        elif len(digits) == 14:
            found.add(digits[:9])
    return found


def _distinctive_name_match(name: str, text: str) -> bool:
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", name)
        if len(token) >= 4 and token not in _LEGAL_SUFFIXES
    ]
    return bool(tokens) and (sum(token in text for token in tokens) >= min(2, len(tokens)))


def _domain_brand_matches(target: ContactTarget, domain: str) -> bool:
    compact_domain = re.sub(r"[^a-z0-9]", "", domain.casefold().split(".", 1)[0])
    for value in (target.display_name_snapshot, target.organization_name_snapshot):
        normalized = normalize_generic(value) or ""
        compact = "".join(
            token for token in re.findall(r"[a-z0-9]+", normalized)
            if token not in _LEGAL_SUFFIXES
        )
        if len(compact) >= 4 and (compact == compact_domain or compact in compact_domain):
            return True
        if any(
            len(token) >= 4 and token in compact_domain
            for token in re.findall(r"[a-z0-9]+", normalized)
            if token not in _LEGAL_SUFFIXES
        ):
            return True
    return False


def _target_identity_matches(target: ContactTarget, normalized_text: str) -> bool:
    names = (target.organization_name_snapshot, target.display_name_snapshot)
    for name in names:
        normalized = normalize_generic(name) or ""
        if normalized and normalized in normalized_text:
            return True
        if _distinctive_name_match(normalized, normalized_text):
            return True
    return False


def _third_party_reason(target, candidate, pages) -> Optional[str]:
    homepage = pages[0]
    combined = normalize_generic(" ".join(page.text for page in pages)) or ""
    path_is_profile = any(marker in urlsplit(homepage.final_url).path.casefold() for marker in _PROFILE_PATH_MARKERS)
    looks_like_directory = any(marker in combined for marker in _THIRD_PARTY_PROFILE_MARKERS)
    legal_pages = [page for page in pages[1:] if _is_legal_url(page.final_url)]
    legal_text = normalize_generic(" ".join(page.text for page in legal_pages)) or ""
    operator = _OPERATOR_PATTERN.search(legal_text)
    operator_is_target = bool(operator and _target_identity_matches(target, normalize_generic(operator.group(1)) or ""))
    if operator and not operator_is_target:
        return "third_party_directory" if looks_like_directory or path_is_profile else "third_party_profile"
    if (looks_like_directory or path_is_profile) and not _domain_brand_matches(target, candidate.registrable_domain):
        return "third_party_directory" if looks_like_directory else "third_party_profile"
    return ""


def _verification_links(links: Sequence[str], domain: str) -> tuple[str, ...]:
    legal = []
    contact = []
    for link in links:
        if registrable_domain(link) != domain:
            continue
        path = urlsplit(link).path.casefold()
        if any(marker in path for marker in _LEGAL_LINK_MARKERS):
            legal.append(link)
        elif any(marker in path for marker in _CONTACT_LINK_MARKERS):
            contact.append(link)
    return tuple(dict.fromkeys(legal + contact))


def _positive_families(signals):
    families = set()
    for signal in signals:
        if signal.weight <= 0:
            continue
        if signal.signal_type in {"legal_name_exact", "distinctive_name", "homepage_brand_identity"}:
            families.add("identity")
        elif signal.signal_type in {"domain_brand_match", "legal_operator_target"}:
            families.add("ownership")
        elif signal.signal_type == "internal_legal_page":
            families.add("legal")
        elif signal.signal_type in {"geography_match", "local_location_exact"}:
            families.add("geography")
        elif signal.signal_type == "siren_exact":
            families.add("legal_identity")
    return families


def _signal(
    signal_type, weight, reason, source_url, observed, expected, observed_at,
    polarity=SignalPolarity.POSITIVE,
):
    value = str(observed)[:500] if observed is not None else None
    return WebsiteVerificationSignal(
        signal_type=signal_type, polarity=polarity, weight=weight, reason=reason,
        source_url=source_url, excerpt=value, observed_value=value,
        expected_value=str(expected)[:500] if expected is not None else None,
        observed_at=observed_at,
    )


def _source_for_value(pages, value):
    needle = normalize_generic(str(value)) or ""
    for page in pages:
        if needle in (normalize_generic(page.text) or ""):
            return page.final_url
    return pages[0].final_url


def _is_legal_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return any(marker in path for marker in _LEGAL_LINK_MARKERS)


def _result(target, candidate, candidate_set_fp, status, score, signals, reasons, verified_at):
    warnings = []
    if target.scope == ContactScope.LOCAL:
        warnings.append("La page locale ne constitue pas une identité juridique d'établissement.")
    fingerprint = _fingerprint({
        "target": candidate.target_fingerprint,
        "candidate": candidate.candidate_fingerprint,
        "candidate_set": candidate_set_fp,
        "status": status,
    })
    return VerifiedOfficialSite(
        company_key=target.company_key, target_scope=target.scope, local_key=target.local_key,
        target_fingerprint=candidate.target_fingerprint,
        candidate_fingerprint=candidate.candidate_fingerprint,
        candidate_set_fingerprint=candidate_set_fp, provider="official_web",
        canonical_url=candidate.canonical_url, registrable_domain=candidate.registrable_domain,
        status=status, score=score, rejection_reasons=tuple(reasons),
        attribution_warnings=tuple(warnings), observed_at=candidate.observed_at,
        verified_at=verified_at, signals=tuple(signals), fingerprint=fingerprint,
    )


def _replace_status(item, status, warning):
    return VerifiedOfficialSite(
        **{
            **item.__dict__,
            "status": status,
            "attribution_warnings": item.attribution_warnings + (warning,),
            "fingerprint": _fingerprint({
                "target": item.target_fingerprint, "candidate": item.candidate_fingerprint,
                "candidate_set": item.candidate_set_fingerprint, "status": status,
            }),
        }
    )


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
