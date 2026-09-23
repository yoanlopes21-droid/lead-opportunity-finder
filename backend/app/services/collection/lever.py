"""Public Lever postings provider for one explicitly configured employer site."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

import httpx

from app.services.collection.geography import explicit_commune, is_val_de_marne
from app.services.collection.greenhouse import EMPLOYER_CAREER_SOURCE
from app.services.collection.providers import ProviderPage
from app.services.persistence.offers import OfferSnapshot


class LeverProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeverBoard:
    site_name: str
    company_name: str
    discovery_provider: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.site_name.strip() or not self.company_name.strip():
            raise ValueError("Lever site name and company name are required")


class LeverJobBoardProvider:
    """Read Lever's documented public postings feed and retain explicit 94 jobs."""

    provider_id = EMPLOYER_CAREER_SOURCE
    scope_type = "provider_board"
    can_deactivate_unseen = True

    def __init__(
        self,
        board: LeverBoard,
        *,
        base_url: str = "https://api.lever.co/v0/postings",
        timeout_seconds: float = 10.0,
        requests_per_second: float = 1.0,
        requester: Optional[Callable[..., Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or requests_per_second <= 0:
            raise ValueError("timeout and rate limit must be positive")
        self.board = board
        self.scope_value = f"lever:{board.site_name.strip()}"
        self._url = f"{base_url.rstrip('/')}/{board.site_name.strip()}"
        self._timeout = timeout_seconds
        self._requester = requester or httpx.get
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._minimum_interval = 1.0 / requests_per_second
        self._last_request_at: Optional[float] = None

    def iter_pages(self) -> Iterable[ProviderPage]:
        if self._last_request_at is not None:
            remaining = self._minimum_interval - (self._monotonic() - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
        try:
            response = self._requester(
                self._url,
                params={"mode": "json"},
                headers={"Accept": "application/json", "User-Agent": "LeadOpportunityFinder/0.1"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise LeverProviderError("Lever public postings request failed") from exc
        finally:
            self._last_request_at = self._monotonic()
        if not 200 <= int(getattr(response, "status_code", 0)) <= 299:
            raise LeverProviderError("Lever public postings feed rejected the request")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise LeverProviderError("Lever returned invalid JSON") from exc
        offers, skipped = _parse_postings(payload, self.board, self.scope_value)
        yield ProviderPage(page_number=1, offers=offers, skipped_count=skipped, is_last=True)


def _parse_postings(
    payload: Any, board: LeverBoard, origin: str
) -> tuple[tuple[OfferSnapshot, ...], int]:
    if not isinstance(payload, list):
        raise LeverProviderError("Lever response does not contain a postings list")
    parsed: list[OfferSnapshot] = []
    skipped = 0
    seen: set[str] = set()
    for item in payload:
        offer = _parse_posting(item, board, origin)
        if offer is None or offer.source_offer_id in seen:
            skipped += 1
            continue
        seen.add(offer.source_offer_id)
        parsed.append(offer)
    return tuple(parsed), skipped


def _parse_posting(
    item: Any, board: LeverBoard, origin: str
) -> Optional[OfferSnapshot]:
    if not isinstance(item, Mapping):
        return None
    identifier = _text(item.get("id"))
    title = _text(item.get("text"))
    url = _text(item.get("hostedUrl")) or _text(item.get("applyUrl"))
    categories = item.get("categories")
    location = _text(categories.get("location")) if isinstance(categories, Mapping) else None
    if not identifier or not title or not url or not location or not is_val_de_marne(location):
        return None
    return OfferSnapshot(
        source=EMPLOYER_CAREER_SOURCE,
        source_offer_id=f"lever:{board.site_name.strip()}:{identifier}",
        title=title,
        description=_text(item.get("descriptionPlain")) or _text(item.get("description")),
        company_name=board.company_name.strip(),
        location_label=location,
        commune=explicit_commune(location),
        department_code="94",
        created_at=_timestamp(item.get("createdAt")),
        updated_at=None,
        contract_type=(
            _text(categories.get("commitment")) if isinstance(categories, Mapping) else None
        ),
        salary=None,
        source_url=url,
        discovery_provider=board.discovery_provider,
        origin=origin,
    )


def _timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split()).strip()
    return compact or None
