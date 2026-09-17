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
    dated_offers = [(offer, _parse_datetime(offer.created_at)) for offer in offers]
    ages = [max((observed_at - created_at).total_seconds() / 86400, 0) for _, created_at in dated_offers if created_at]
    dates = [created_at for _, created_at in dated_offers if created_at]
    sources = _sorted_distinct(offer.source for offer in offers)
    titles = _representative_distinct((offer.title, _normalize_text_key(offer.title)) for offer in offers)
    contracts = _sorted_distinct(offer.contract_type for offer in offers)
    communes = _sorted_distinct(offer.commune for offer in offers)
    labels = _sorted_distinct(offer.location_label for offer in offers)
    distinct_locations = _sorted_distinct(
        offer.commune if offer.commune else offer.location_label for offer in offers
    )
    cdi_count = sum(1 for offer in offers if _normalize_text_key(offer.contract_type) == "cdi")
    cdd_count = sum(1 for offer in offers if _normalize_text_key(offer.contract_type) == "cdd")
    other_count = len(offers) - cdi_count - cdd_count
    oldest = min(dates) if dates else None
    newest = max(dates) if dates else None
    rounded_ages = [int(age) for age in ages]
    signals = (
        OpportunitySignal("hiring_volume_signal", len(offers) >= 2, f"{len(offers)} offre(s) active(s)."),
        OpportunitySignal("role_diversity_signal", len(titles) >= 2, f"{len(titles)} intitulé(s) distinct(s)."),
        OpportunitySignal("persistent_need_signal", any(age > 21 for age in ages), f"{sum(age > 21 for age in ages)} offre(s) active(s) de plus de 21 jours."),
        OpportunitySignal("multi_location_signal", len(distinct_locations) >= 2, f"{len(distinct_locations)} lieu(x) distinct(s)."),
        OpportunitySignal("recurrent_observation_signal", any(offer.observation_count >= 2 for offer in offers), f"{sum(offer.observation_count >= 2 for offer in offers)} offre(s) observée(s) dans plusieurs runs."),
    )
    intermediary_description_evidence = analyze_intermediary_descriptions(offers)
    local_opportunities = _build_local_opportunities(offers, department_code)
    return CompanyOpportunity(
        company_key=company_key,
        company_name=original_name,
        department_code=department_code,
        active_offer_count=len(offers),
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
        intermediary_description_evidence=intermediary_description_evidence,
        local_opportunities=local_opportunities,
    )


def _build_local_opportunities(
    offers: Sequence[ObservedJobOffer], department_code: str
) -> tuple[LocalOpportunity, ...]:
    grouped: dict[str, list[ObservedJobOffer]] = {}
    locations: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for offer in offers:
        local_key, commune, location_label = _local_bucket(offer, department_code)
        grouped.setdefault(local_key, []).append(offer)
        locations.setdefault(local_key, (commune, location_label))

    opportunities: list[LocalOpportunity] = []
    for local_key, local_offers in grouped.items():
        commune, fallback_label = locations[local_key]
        location_label = next(
            (_clean_location_value(offer.location_label) for offer in local_offers if _clean_location_value(offer.location_label)),
            fallback_label,
        )
        titles = _representative_distinct(
            (offer.title, _normalize_text_key(offer.title)) for offer in local_offers
        )
        dates = [
            created_at
            for offer in local_offers
            if (created_at := _parse_datetime(offer.created_at)) is not None
        ]
        source_offer_ids = tuple(sorted(_qualified_offer_id(offer) for offer in local_offers))
        source_urls = _sorted_distinct(offer.source_url for offer in local_offers)
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
                signals=_local_signals(local_offers, titles),
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
    offers: Sequence[ObservedJobOffer], titles: tuple[str, ...]
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
            any(offer.observation_count >= 2 for offer in offers),
            f"{sum(offer.observation_count >= 2 for offer in offers)} offre(s) observée(s) dans plusieurs runs dans cette localisation.",
        ),
    )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_datetime(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _normalize_text_key(value: Optional[str]) -> Optional[str]:
    return " ".join(value.casefold().split()) if isinstance(value, str) and value.strip() else None


def _clean_location_value(value: Optional[str]) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


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
