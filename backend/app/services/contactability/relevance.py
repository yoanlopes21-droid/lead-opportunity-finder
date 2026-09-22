"""Commercial relevance of an authentic contact channel for a French lead.

This is deliberately separate from confidence and verification: a contact can
belong to the organisation and still be unsuitable for a French opportunity.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from app.models import ContactEvidence, ContactPoint
from app.services.contactability.contracts import ContactType


class ChannelRelevance:
    RELEVANT = "relevant"
    NATIONAL_FRANCE = "national_france"
    REVIEW_NEEDED = "review_needed"
    IRRELEVANT_FOREIGN = "irrelevant_foreign"


@dataclass(frozen=True)
class ChannelRelevanceAssessment:
    status: str
    warnings: tuple[str, ...] = ()
    rationale_codes: tuple[str, ...] = ()


_FOREIGN_SUFFIXES = {
    ".co.uk": "GB", ".uk": "GB", ".us": "US", ".de": "DE", ".es": "ES",
    ".it": "IT", ".nl": "NL", ".be": "BE", ".ch": "CH", ".ie": "IE",
    ".pt": "PT", ".pl": "PL", ".ca": "CA", ".com.au": "AU", ".co.nz": "NZ",
}
_FOREIGN_TEXT = {
    "GB": re.compile(r"\b(?:royaume[- ]uni|united kingdom|great britain|uk)\b", re.I),
    "US": re.compile(r"\b(?:états[- ]unis|etats[- ]unis|united states|usa|u\.s\.)\b", re.I),
}
_FRANCE_TEXT = re.compile(r"\b(?:france|français|francaise?|national(?:e)? france)\b", re.I)
_RECRUITMENT_TEXT = re.compile(r"\b(?:recrut|emploi|career|jobs?|talent|ressources humaines|\brh\b)", re.I)


def assess_channel_relevance(
    point: ContactPoint,
    evidence: Iterable[ContactEvidence],
    *,
    related_domains: Iterable[str] = (),
) -> ChannelRelevanceAssessment:
    """Assess France relevance from the channel and its sourced context.

    A country TLD or calling code alone triggers review, never a hard rejection.
    A clear mismatch requires corroborating foreign context.  Conversely, an
    explicit French recruitment source can validate a national/group channel.
    """
    rows = tuple(evidence)
    evidence_text = " ".join(filter(None, (
        *(row.source_name for row in rows), *(row.evidence_reason for row in rows),
        *(row.excerpt for row in rows),
    )))
    source_domains = tuple(filter(None, (_hostname(row.source_url) for row in rows)))
    contact_domain = _contact_domain(point)
    context_domains = tuple(dict.fromkeys(filter(None, (contact_domain, *source_domains, *related_domains))))

    france_signals = []
    if point.contact_type == ContactType.PHONE and point.normalized_value.startswith("+33"):
        france_signals.append("french_phone")
    if any(domain == "fr" or domain.endswith(".fr") for domain in context_domains):
        france_signals.append("french_domain")
    if _FRANCE_TEXT.search(evidence_text):
        france_signals.append("explicit_france_context")

    foreign_countries = {_country_for_domain(domain) for domain in context_domains}
    foreign_countries.discard(None)
    phone_country = _phone_country(point)
    if phone_country:
        foreign_countries.add(phone_country)
    explicit_foreign = {country for country, pattern in _FOREIGN_TEXT.items() if pattern.search(evidence_text)}

    if france_signals:
        if point.scope == "company" and _RECRUITMENT_TEXT.search(
            " ".join((point.value, evidence_text, *context_domains))
        ):
            return ChannelRelevanceAssessment(
                ChannelRelevance.NATIONAL_FRANCE,
                ("Canal national ou groupe relié au recrutement en France ; vérifier le bon routage vers l'opportunité locale.",),
                ("national_france_recruitment_channel", *france_signals),
            )
        return ChannelRelevanceAssessment(
            ChannelRelevance.RELEVANT, (), tuple(france_signals),
        )

    if explicit_foreign or len(foreign_countries) > 1:
        return ChannelRelevanceAssessment(
            ChannelRelevance.IRRELEVANT_FOREIGN,
            ("Canal authentique possible, mais rattaché à une entité étrangère sans preuve de couverture de la France.",),
            ("foreign_context_without_france",),
        )
    if foreign_countries:
        return ChannelRelevanceAssessment(
            ChannelRelevance.REVIEW_NEEDED,
            ("Portée géographique étrangère possible : confirmer que ce canal couvre les recrutements en France.",),
            ("foreign_country_signal_requires_review",),
        )
    return ChannelRelevanceAssessment(ChannelRelevance.RELEVANT)


def _contact_domain(point: ContactPoint) -> Optional[str]:
    if point.contact_type == ContactType.EMAIL and "@" in point.normalized_value:
        return point.normalized_value.rsplit("@", 1)[1].casefold().strip(" .")
    if point.contact_type in {ContactType.WEBSITE, ContactType.PROFESSIONAL_URL}:
        return _hostname(point.normalized_value)
    return None


def _hostname(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return (urlsplit(value).hostname or "").casefold().rstrip(".") or None
    except ValueError:
        return None


def _country_for_domain(domain: str) -> Optional[str]:
    for suffix, country in sorted(_FOREIGN_SUFFIXES.items(), key=lambda item: -len(item[0])):
        if domain.endswith(suffix):
            return country
    return None


def _phone_country(point: ContactPoint) -> Optional[str]:
    if point.contact_type != ContactType.PHONE:
        return None
    value = point.normalized_value.replace(" ", "")
    if value.startswith("+44"):
        return "GB"
    if value.startswith("+1"):
        return "US"
    return None
