"""Aggregate active source-independent offers into descriptive company opportunities."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ObservedJobOffer
from app.services.opportunities.intermediary import (
    IntermediaryDescriptionEvidence,
    analyze_intermediary_descriptions,
)


@dataclass(frozen=True)
class OpportunitySignal:
    """An objective, explainable observation; it is intentionally not a score."""

    name: str
    active: bool
    explanation: str


@dataclass(frozen=True)
class LocalOpportunity:
    """A descriptive, non-juridical grouping of one company's active offers."""

    local_key: str
    commune: Optional[str]
    location_label: Optional[str]
    department_code: str
    active_offer_count: int
    distinct_job_title_count: int
    representative_job_titles: tuple[str, ...]
    oldest_offer_created_at: Optional[str]
    newest_offer_created_at: Optional[str]
    source_offer_ids: tuple[str, ...]
    source_urls: tuple[str, ...]
    signals: tuple[OpportunitySignal, ...]


@dataclass(frozen=True)
class ActiveJobOffer:
    """A compact, display-safe active offer; no source payload or description."""

    offer_id: str
    title: str
    commune: Optional[str]
    location_label: Optional[str]
    display_location: Optional[str]
    published_at: Optional[str]
    updated_at: Optional[str]
    contract_type: Optional[str]
    salary: Optional[str]
    source: str
    source_url: Optional[str]
    sources: tuple[str, ...]
    source_urls: tuple[str, ...]
    source_offer_ids: tuple[str, ...]
    evidence: tuple["JobOfferEvidence", ...]
    local_key: str
    age_days: Optional[int]
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int


@dataclass(frozen=True)
class JobOfferEvidence:
    source: str
    source_offer_id: str
    source_url: Optional[str]
    discovery_provider: Optional[str]


@dataclass(frozen=True)
class _CanonicalOffer:
    """In-memory commercial need; every persisted source observation is retained."""

    observations: tuple[ObservedJobOffer, ...]

    @property
    def representative(self) -> ObservedJobOffer:
        return self.observations[0]


@dataclass(frozen=True)
class CompanyOpportunity:
    company_key: str
    company_name: str
    department_code: str
    active_offer_count: int
    distinct_job_title_count: int
    distinct_source_count: int
    sources: tuple[str, ...]
    oldest_offer_created_at: Optional[str]
    newest_offer_created_at: Optional[str]
    oldest_offer_age_days: Optional[int]
    newest_offer_age_days: Optional[int]
    contract_types: tuple[str, ...]
    communes: tuple[str, ...]
    location_labels: tuple[str, ...]
    offer_ids: tuple[str, ...]
    job_titles: tuple[str, ...]
    cdi_offer_count: int
    cdd_offer_count: int
    other_contract_offer_count: int
    offers_over_21_days: int
    offers_over_45_days: int
    offers_over_90_days: int
    average_offer_age_days: Optional[float]
    median_offer_age_days: Optional[float]
    signals: tuple[OpportunitySignal, ...]
    active_job_offers: tuple[ActiveJobOffer, ...] = ()
    intermediary_description_evidence: IntermediaryDescriptionEvidence = field(
        default_factory=IntermediaryDescriptionEvidence
    )
    local_opportunities: tuple[LocalOpportunity, ...] = ()


@dataclass(frozen=True)
class CompanyOpportunityAggregation:
    opportunities: tuple[CompanyOpportunity, ...]
    active_offers_analyzed: int
    unattributed_offer_count: int
    unattributed_offer_ids: tuple[str, ...]


def normalize_company_key(company_name: Optional[str]) -> Optional[str]:
    """Conservatively normalize trivial spelling differences, never legal meaning."""
    if not isinstance(company_name, str):
        return None
    normalized = unicodedata.normalize("NFKC", company_name).casefold().strip()
    if not normalized:
        return None
    # Punctuation and spacing alone do not identify a different organization.
    # Words (including legal suffixes such as SAS or SARL) are retained.
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def aggregate_active_company_opportunities(
    session: Session,
    department_code: str = "94",
    now: Optional[datetime] = None,
) -> CompanyOpportunityAggregation:
    """Read active offers for one department without mutating their source records."""
    observed_at = now or datetime.now(timezone.utc)
    offers = tuple(
        session.scalars(
            select(ObservedJobOffer)
            .where(
                ObservedJobOffer.is_active.is_(True),
                ObservedJobOffer.department_code == department_code,
            )
            .order_by(ObservedJobOffer.id)
        )
    )
    grouped: dict[str, list[ObservedJobOffer]] = {}
    unattributed_offer_ids: list[str] = []
    for offer in offers:
        company_key = normalize_company_key(offer.company_name)
        qualified_id = _qualified_offer_id(offer)
        if company_key is None:
            unattributed_offer_ids.append(qualified_id)
            continue
        grouped.setdefault(company_key, []).append(offer)

    opportunities = tuple(
        _build_opportunity(company_key, group, department_code, observed_at)
        for company_key, group in grouped.items()
    )
    return CompanyOpportunityAggregation(
        opportunities=tuple(sorted(opportunities, key=lambda item: (-item.active_offer_count, item.company_name.casefold()))),
        active_offers_analyzed=len(offers),
        unattributed_offer_count=len(unattributed_offer_ids),
        unattributed_offer_ids=tuple(unattributed_offer_ids),
    )


def _build_opportunity(
    company_key: str,
    offers: Sequence[ObservedJobOffer],
    department_code: str,
    observed_at: datetime,
) -> CompanyOpportunity:
    original_name = next(offer.company_name.strip() for offer in offers if offer.company_name)
    canonical_offers = _canonicalize_offers(offers)
    canonical_dates = [_canonical_date(item) for item in canonical_offers]
    ages = [max((observed_at - created_at).total_seconds() / 86400, 0) for created_at in canonical_dates if created_at]
    dates = [created_at for created_at in canonical_dates if created_at]
    sources = _sorted_distinct(offer.source for offer in offers)
    titles = _representative_distinct(
        (item.representative.title, _normalize_role_key(item.representative.title))
        for item in canonical_offers
    )
    contracts = _sorted_distinct(offer.contract_type for offer in offers)
    communes = _sorted_distinct(offer.commune for offer in offers)
    labels = _sorted_distinct(offer.location_label for offer in offers)
    distinct_locations = _sorted_distinct(
        offer.commune if offer.commune else offer.location_label for offer in offers
    )
    canonical_contracts = [_canonical_value(item, "contract_type") for item in canonical_offers]
    cdi_count = sum(1 for value in canonical_contracts if _normalize_text_key(value) == "cdi")
    cdd_count = sum(1 for value in canonical_contracts if _normalize_text_key(value) == "cdd")
    other_count = len(canonical_offers) - cdi_count - cdd_count
    oldest = min(dates) if dates else None
    newest = max(dates) if dates else None
    rounded_ages = [int(age) for age in ages]
    signals = (
        OpportunitySignal("hiring_volume_signal", len(canonical_offers) >= 2, f"{len(canonical_offers)} besoin(s) de recrutement actif(s)."),
        OpportunitySignal("role_diversity_signal", len(titles) >= 2, f"{len(titles)} intitulé(s) distinct(s)."),
        OpportunitySignal("persistent_need_signal", any(age > 21 for age in ages), f"{sum(age > 21 for age in ages)} offre(s) active(s) de plus de 21 jours."),
        OpportunitySignal("multi_location_signal", len(distinct_locations) >= 2, f"{len(distinct_locations)} lieu(x) distinct(s)."),
        OpportunitySignal("recurrent_observation_signal", any(offer.observation_count >= 2 for offer in offers), f"{sum(offer.observation_count >= 2 for offer in offers)} offre(s) observée(s) dans plusieurs runs."),
    )
    intermediary_description_evidence = analyze_intermediary_descriptions(offers)
    local_opportunities = _build_local_opportunities(canonical_offers, department_code)
    return CompanyOpportunity(
        company_key=company_key,
        company_name=original_name,
        department_code=department_code,
        active_offer_count=len(canonical_offers),
        distinct_job_title_count=len(titles),
        distinct_source_count=len(sources),
        sources=sources,
        oldest_offer_created_at=_format_datetime(oldest),
        newest_offer_created_at=_format_datetime(newest),
        oldest_offer_age_days=max(rounded_ages) if rounded_ages else None,
        newest_offer_age_days=min(rounded_ages) if rounded_ages else None,
        contract_types=contracts,
        communes=communes,
        location_labels=labels,
        offer_ids=tuple(_qualified_offer_id(offer) for offer in offers),
        job_titles=titles,
        cdi_offer_count=cdi_count,
        cdd_offer_count=cdd_count,
        other_contract_offer_count=other_count,
        offers_over_21_days=sum(age > 21 for age in ages),
        offers_over_45_days=sum(age > 45 for age in ages),
        offers_over_90_days=sum(age > 90 for age in ages),
        average_offer_age_days=round(sum(ages) / len(ages), 1) if ages else None,
        median_offer_age_days=round(float(median(ages)), 1) if ages else None,
        signals=signals,
        active_job_offers=_active_job_offers(canonical_offers, department_code, observed_at),
        intermediary_description_evidence=intermediary_description_evidence,
        local_opportunities=local_opportunities,
    )


def _active_job_offers(
    offers: Sequence[_CanonicalOffer], department_code: str, observed_at: datetime,
) -> tuple[ActiveJobOffer, ...]:
    rows = []
    for canonical in offers:
        offer = canonical.representative
        published_at = _canonical_date(canonical)
        local_key, _, _ = _local_bucket(offer, department_code)
        commune = _clean_location_value(offer.commune)
        location_label = _clean_location_value(offer.location_label)
        evidence = tuple(JobOfferEvidence(
            source=item.source,
            source_offer_id=item.source_offer_id,
            source_url=item.source_url,
            discovery_provider=item.discovery_provider,
        ) for item in canonical.observations)
        urls = _sorted_distinct(item.source_url for item in canonical.observations)
        ids = tuple(sorted(_qualified_offer_id(item) for item in canonical.observations))
        sources = _sorted_distinct(item.source for item in canonical.observations)
        rows.append((offer, published_at, ActiveJobOffer(
            offer_id=offer.source_offer_id,
            title=offer.title,
            commune=commune,
            location_label=location_label,
            display_location=_display_location(commune, location_label),
            published_at=_format_datetime(published_at),
            updated_at=_format_datetime(_canonical_updated_date(canonical)),
            contract_type=_clean_location_value(_canonical_value(canonical, "contract_type")),
            salary=_usable_salary(_canonical_value(canonical, "salary")),
            source=offer.source,
            source_url=offer.source_url,
            sources=sources,
            source_urls=urls,
            source_offer_ids=ids,
            evidence=evidence,
            local_key=local_key,
            age_days=(max(int((observed_at - published_at).total_seconds() / 86400), 0) if published_at else None),
            first_seen_at=min(_as_utc(item.first_seen_at) for item in canonical.observations),
            last_seen_at=max(_as_utc(item.last_seen_at) for item in canonical.observations),
            observation_count=sum(item.observation_count for item in canonical.observations),
        )))
    return tuple(item[2] for item in sorted(
        rows,
        key=lambda item: (
            item[1] is None, -(item[1].timestamp()) if item[1] else 0,
            item[0].title.casefold(), item[0].source.casefold(), item[0].source_offer_id,
        ),
    ))


def _build_local_opportunities(
    offers: Sequence[_CanonicalOffer], department_code: str
) -> tuple[LocalOpportunity, ...]:
    grouped: dict[str, list[_CanonicalOffer]] = {}
    locations: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for canonical in offers:
        offer = canonical.representative
        local_key, commune, location_label = _local_bucket(offer, department_code)
        grouped.setdefault(local_key, []).append(canonical)
        locations.setdefault(local_key, (commune, location_label))

    opportunities: list[LocalOpportunity] = []
    for local_key, local_offers in grouped.items():
        commune, fallback_label = locations[local_key]
        observations = tuple(
            observation for item in local_offers for observation in item.observations
        )
        location_label = next(
            (_clean_location_value(offer.location_label) for offer in observations if _clean_location_value(offer.location_label)),
            fallback_label,
        )
        titles = _representative_distinct(
            (item.representative.title, _normalize_role_key(item.representative.title))
            for item in local_offers
        )
        dates = [
            created_at for item in local_offers
            if (created_at := _canonical_date(item)) is not None
        ]
        source_offer_ids = tuple(sorted(_qualified_offer_id(offer) for offer in observations))
        source_urls = _sorted_distinct(offer.source_url for offer in observations)
        opportunities.append(
            LocalOpportunity(
                local_key=local_key,
                commune=commune,
                location_label=location_label,
                department_code=department_code,
                active_offer_count=len(local_offers),
                distinct_job_title_count=len(titles),
                representative_job_titles=titles,
                oldest_offer_created_at=_format_datetime(min(dates)) if dates else None,
                newest_offer_created_at=_format_datetime(max(dates)) if dates else None,
                source_offer_ids=source_offer_ids,
                source_urls=source_urls,
                signals=_local_signals(local_offers, observations, titles),
            )
        )
    return tuple(
        sorted(
            opportunities,
            key=lambda item: (
                -item.active_offer_count,
                (item.commune or item.location_label or "").casefold(),
                item.local_key,
            ),
        )
    )


def _local_bucket(
    offer: ObservedJobOffer, department_code: str
) -> tuple[str, Optional[str], Optional[str]]:
    commune = _clean_location_value(offer.commune)
    if commune:
        return (
            f"commune:{department_code}:{_normalize_location_key(commune)}",
            commune,
            None,
        )
    location_label = _clean_location_value(offer.location_label)
    if location_label:
        return (
            f"location:{department_code}:{_normalize_location_key(location_label)}",
            None,
            location_label,
        )
    return (f"unknown_location:{department_code}", None, None)


def _local_signals(
    offers: Sequence[_CanonicalOffer], observations: Sequence[ObservedJobOffer],
    titles: tuple[str, ...]
) -> tuple[OpportunitySignal, ...]:
    return (
        OpportunitySignal(
            "local_hiring_volume_signal",
            len(offers) >= 2,
            f"{len(offers)} offre(s) active(s) dans cette localisation.",
        ),
        OpportunitySignal(
            "local_role_diversity_signal",
            len(titles) >= 2,
            f"{len(titles)} intitulé(s) distinct(s) dans cette localisation.",
        ),
        OpportunitySignal(
            "local_recurrent_observation_signal",
            any(offer.observation_count >= 2 for offer in observations),
            f"{sum(offer.observation_count >= 2 for offer in observations)} observation(s) revue(s) dans plusieurs runs dans cette localisation.",
        ),
    )


def _canonicalize_offers(
    offers: Sequence[ObservedJobOffer],
) -> tuple[_CanonicalOffer, ...]:
    """Conservatively join corroborating sources without mutating observations."""
    groups: list[list[ObservedJobOffer]] = []
    for offer in sorted(offers, key=lambda item: item.id):
        matching = next((group for group in groups if _can_join_group(offer, group)), None)
        if matching is None:
            groups.append([offer])
        else:
            matching.append(offer)
    return tuple(_CanonicalOffer(tuple(group)) for group in groups)


def _can_join_group(offer: ObservedJobOffer, group: Sequence[ObservedJobOffer]) -> bool:
    if any(existing.source == offer.source for existing in group):
        return False
    return all(_same_cross_source_need(offer, existing) for existing in group)


def _same_cross_source_need(left: ObservedJobOffer, right: ObservedJobOffer) -> bool:
    if _normalize_role_key(left.title) != _normalize_role_key(right.title):
        return False
    left_locations = {_normalize_location_key(value) for value in (left.commune, left.location_label) if _clean_location_value(value)}
    right_locations = {_normalize_location_key(value) for value in (right.commune, right.location_label) if _clean_location_value(value)}
    if not left_locations or not right_locations or left_locations.isdisjoint(right_locations):
        return False
    left_contract = _normalize_text_key(left.contract_type)
    right_contract = _normalize_text_key(right.contract_type)
    if left_contract and right_contract and left_contract != right_contract:
        return False
    left_date = _parse_datetime(left.created_at)
    right_date = _parse_datetime(right.created_at)
    if left_date and right_date:
        return abs((left_date - right_date).total_seconds()) <= 21 * 86400
    return abs((left.first_seen_at - right.first_seen_at).total_seconds()) <= 7 * 86400


def _canonical_date(offer: _CanonicalOffer) -> Optional[datetime]:
    dates = [_parse_datetime(item.created_at) for item in offer.observations]
    usable = [item for item in dates if item is not None]
    return min(usable) if usable else None


def _canonical_updated_date(offer: _CanonicalOffer) -> Optional[datetime]:
    dates = [_parse_datetime(item.updated_at) for item in offer.observations]
    usable = [item for item in dates if item is not None]
    return max(usable) if usable else None


def _canonical_value(offer: _CanonicalOffer, name: str) -> Optional[str]:
    return next((value for item in offer.observations if (value := getattr(item, name))), None)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _format_datetime(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _normalize_text_key(value: Optional[str]) -> Optional[str]:
    return " ".join(value.casefold().split()) if isinstance(value, str) and value.strip() else None


def _normalize_role_key(value: Optional[str]) -> Optional[str]:
    normalized = _normalize_text_key(value)
    if not normalized:
        return None
    normalized = re.sub(r"\s*[\[(]?\s*(?:h\s*[/.-]\s*f|f\s*[/.-]\s*h|m\s*[/.-]\s*f)\s*[\])]?\s*$", "", normalized)
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _clean_location_value(value: Optional[str]) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _display_location(commune: Optional[str], location_label: Optional[str]) -> Optional[str]:
    """Expose only a human location; commune codes remain internal join keys."""
    for value in (commune, location_label):
        cleaned = _clean_location_value(value)
        if cleaned and not re.fullmatch(r"\d{5}", cleaned):
            return cleaned
    return None


def _usable_salary(value: Optional[str]) -> Optional[str]:
    cleaned = _clean_location_value(value)
    if not cleaned:
        return None
    numbers = re.findall(r"\d+(?:[.,]\d+)?", cleaned.replace(" ", ""))
    if not numbers:
        return None
    if all(float(number.replace(",", ".")) == 0 for number in numbers):
        return None
    return cleaned


def _normalize_location_key(value: str) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", value).casefold()
        if not unicodedata.combining(character)
    ).strip()
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _sorted_distinct(values) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if isinstance(value, str) and value.strip()}, key=str.casefold))


def _representative_distinct(values) -> tuple[str, ...]:
    representatives: dict[str, str] = {}
    for original, normalized in values:
        if normalized and normalized not in representatives:
            representatives[normalized] = original.strip()
    return tuple(sorted(representatives.values(), key=str.casefold))


def _qualified_offer_id(offer: ObservedJobOffer) -> str:
    return f"{offer.source}:{offer.source_offer_id}"
