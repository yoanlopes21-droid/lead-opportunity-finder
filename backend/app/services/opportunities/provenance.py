"""Dynamic provenance of one canonical recruitment need."""

from typing import Iterable

FRANCE_TRAVAIL_SOURCE = "france_travail"
EMPLOYER_SOURCE = "employer_career_site"


def non_ft_only(observations: Iterable[object]) -> bool:
    sources = {getattr(item, "source") for item in observations}
    return bool(sources - {FRANCE_TRAVAIL_SOURCE}) and FRANCE_TRAVAIL_SOURCE not in sources


def primary_provenance(observations: Iterable[object]) -> str:
    rows = tuple(observations)
    if any((getattr(row, "origin", None) or "").startswith("official_web:") for row in rows):
        return "employer_direct"
    if any(getattr(row, "source") == EMPLOYER_SOURCE for row in rows):
        return "ats"
    if any(getattr(row, "source") == FRANCE_TRAVAIL_SOURCE for row in rows):
        return FRANCE_TRAVAIL_SOURCE
    return getattr(rows[0], "source", "unknown") if rows else "unknown"
