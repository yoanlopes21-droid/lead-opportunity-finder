"""Pure, conservative evidence extraction for offer-publisher intermediation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol


class OfferDescription(Protocol):
    source: str
    source_offer_id: str
    title: str
    location_label: Optional[str]
    description: Optional[str]


@dataclass(frozen=True)
class IntermediaryDescriptionExample:
    offer_id: str
    title: str
    location_label: Optional[str]
    marker_types: tuple[str, ...]


@dataclass(frozen=True)
class IntermediaryDescriptionEvidence:
    total_offer_count: int = 0
    strong_signal_offer_count: int = 0
    strong_signal_proportion: float = 0.0
    marker_types: tuple[str, ...] = ()
    examples: tuple[IntermediaryDescriptionExample, ...] = ()


_STRONG_MARKERS = (
    ("for_our_client", re.compile(r"\bpour notre client\b")),
    ("for_one_of_our_clients", re.compile(r"\bpour (?:l )?un de nos clients\b")),
    ("for_one_of_their_clients", re.compile(r"\bpour (?:l )?un de ses clients\b")),
    ("on_behalf_of_our_client", re.compile(r"\bpour le compte de notre client\b")),
    ("temporary_work_agency", re.compile(r"\bagence d interim\b")),
    ("recruitment_firm", re.compile(r"\bcabinet de recrutement\b")),
    ("firm_recruits_for_client", re.compile(r"\bcabinet recrute pour\b")),
)


def analyze_intermediary_descriptions(
    offers: Iterable[OfferDescription], max_examples: int = 3
) -> IntermediaryDescriptionEvidence:
    """Extract only unambiguous intermediary wording from persisted descriptions."""
    if max_examples < 1:
        raise ValueError("max_examples must be positive")
    items = tuple(offers)
    matched: list[IntermediaryDescriptionExample] = []
    marker_types: set[str] = set()
    for offer in items:
        text = _normalized_text(offer.description)
        markers = tuple(code for code, pattern in _STRONG_MARKERS if pattern.search(text))
        if not markers:
            continue
        marker_types.update(markers)
        matched.append(IntermediaryDescriptionExample(
            offer_id=f"{offer.source}:{offer.source_offer_id}",
            title=offer.title,
            location_label=offer.location_label,
            marker_types=markers,
        ))
    count = len(matched)
    return IntermediaryDescriptionEvidence(
        total_offer_count=len(items),
        strong_signal_offer_count=count,
        strong_signal_proportion=round(count / len(items), 3) if items else 0.0,
        marker_types=tuple(sorted(marker_types)),
        examples=tuple(matched[:max_examples]),
    )


def _normalized_text(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value).casefold()
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", without_accents)).strip()
