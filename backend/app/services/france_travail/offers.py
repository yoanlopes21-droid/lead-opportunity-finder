"""France Travail Offres d'emploi v2 client, isolated from application models."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import httpx

from app.config import Settings
from app.services.france_travail.auth import FranceTravailOAuthClient


VAL_DE_MARNE_DEPARTMENT = "94"
MAX_PAGE_SIZE = 150
MAX_RESULTS_PER_QUERY = 3150
MAX_REQUESTS_PER_SECOND = 4
_REQUEST_INTERVAL_SECONDS = 1 / MAX_REQUESTS_PER_SECOND
_CONTENT_RANGE_PATTERN = re.compile(r"^(?:[A-Za-z]+\s+)?(\d+)-(\d+)/(\d+|\*)$")


class FranceTravailOffersError(Exception):
    """Raised when the official offers API cannot be queried safely."""


@dataclass(frozen=True)
class NormalizedJobOffer:
    """Source-independent representation of an offer, with no contact details."""

    source: str
    source_offer_id: str
    title: str
    description: Optional[str]
    company_name: Optional[str]
    location_label: Optional[str]
    commune: Optional[str]
    department_code: str
    created_at: Optional[str]
    updated_at: Optional[str]
    contract_type: Optional[str]
    salary: Optional[str]
    source_url: Optional[str]
    origin: Optional[str]


@dataclass(frozen=True)
class OfferSearchPage:
    """One page of a paginated France Travail department search."""

    offers: Sequence[NormalizedJobOffer]
    http_status: int
    offset: int
    limit: int
    total_count: Optional[int]
    next_offset: Optional[int]
    skipped_offers: int


@dataclass(frozen=True)
class ContentRange:
    start: int
    end: int
    total_count: Optional[int]


@dataclass(frozen=True)
class CreationDateWindow:
    """Inclusive UTC creation-date bounds sent to France Travail."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("creation date bounds must be timezone-aware")
        if self.start > self.end:
            raise ValueError("creation date start must not be after its end")

    def as_query_params(self) -> Mapping[str, str]:
        return {
            "minCreationDate": _format_api_datetime(self.start),
            "maxCreationDate": _format_api_datetime(self.end),
        }

    def split_inclusively(self) -> tuple["CreationDateWindow", "CreationDateWindow"]:
        """Split in two inclusive windows, overlapping exactly at the midpoint."""
        start = self.start.astimezone(timezone.utc).replace(microsecond=0)
        end = self.end.astimezone(timezone.utc).replace(microsecond=0)
        if start >= end:
            raise FranceTravailOffersError(
                "France Travail creation-date window cannot be split further."
            )
        midpoint = start + (end - start) / 2
        midpoint = midpoint.replace(microsecond=0)
        if midpoint <= start:
            midpoint = start.replace(microsecond=0) + timedelta(seconds=1)
        if midpoint >= end:
            raise FranceTravailOffersError(
                "France Travail creation-date window cannot be split further."
            )
        return CreationDateWindow(start, midpoint), CreationDateWindow(midpoint, end)


class FranceTravailOffersClient:
    """Queries only active France Travail offers located in Val-de-Marne (94)."""

    def __init__(
        self,
        settings: Settings,
        auth_client: FranceTravailOAuthClient,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._settings = settings
        self._auth_client = auth_client
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: Optional[float] = None

    def search_department_page(
        self,
        offset: int = 0,
        limit: int = MAX_PAGE_SIZE,
        creation_window: Optional[CreationDateWindow] = None,
    ) -> OfferSearchPage:
        """Fetch one page. No occupation, sector, or contract filter is applied."""
        if offset < 0:
            raise ValueError("offset must be positive")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")

        access_token = self._auth_client.get_access_token()
        end = offset + limit - 1
        self._respect_rate_limit()
        try:
            response = httpx.get(
                self._settings.france_travail_offers_url,
                # France Travail defines ``range`` as a query parameter.  It is
                # not an HTTP Range header; sending it as a header is ignored.
                params={
                    "departement": VAL_DE_MARNE_DEPARTMENT,
                    "range": f"{offset}-{end}",
                    **(creation_window.as_query_params() if creation_window else {}),
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=self._settings.france_travail_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FranceTravailOffersError("France Travail offers request failed.") from exc

        results = payload.get("resultats") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            raise FranceTravailOffersError("France Travail returned an invalid offers payload.")

        offers, skipped_offers = _normalize_results(results)
        content_range = _parse_content_range(response.headers.get("Content-Range"))
        _validate_content_range(content_range, offset, len(results))
        total_count = content_range.total_count if content_range else None
        next_offset = _next_offset(
            offset=offset,
            limit=limit,
            result_count=len(results),
            content_range=content_range,
        )
        return OfferSearchPage(
            offers=offers,
            http_status=response.status_code,
            offset=offset,
            limit=limit,
            total_count=total_count,
            next_offset=next_offset,
            skipped_offers=skipped_offers,
        )

    def iter_department_pages(
        self,
        page_size: int = MAX_PAGE_SIZE,
        creation_window: Optional[CreationDateWindow] = None,
        initial_page: Optional[OfferSearchPage] = None,
    ) -> Iterator[OfferSearchPage]:
        """Yield all available 94 pages when a future orchestrator explicitly consumes them."""
        page = initial_page or self.search_department_page(
            offset=0, limit=page_size, creation_window=creation_window
        )
        seen_page_identifiers: set[tuple[str, ...]] = set()
        while True:
            # Without Content-Range the server has not confirmed where the page
            # starts. A repeated non-empty page would otherwise make an
            # ever-increasing requested offset look like valid progress.
            identifiers = tuple(offer.source_offer_id for offer in page.offers)
            if page.total_count is None and identifiers:
                if identifiers in seen_page_identifiers:
                    raise FranceTravailOffersError(
                        "France Travail returned a repeated page without usable pagination metadata."
                    )
                seen_page_identifiers.add(identifiers)
            yield page
            if page.next_offset is None:
                return
            page = self.search_department_page(
                offset=page.next_offset,
                limit=page_size,
                creation_window=creation_window,
            )

    def _respect_rate_limit(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            delay = _REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if delay > 0:
                self._sleeper(delay)
                now = self._clock()
        self._last_request_at = now


def normalize_offer(payload: Mapping[str, Any]) -> NormalizedJobOffer:
    """Normalize only non-contact offer fields needed by the internal lead model."""
    offer_id = _required_string(payload.get("id"), "id")
    title = _required_string(payload.get("intitule"), "intitule")
    location = _mapping_or_empty(payload.get("lieuTravail"))
    company = _mapping_or_empty(payload.get("entreprise"))
    salary = _mapping_or_empty(payload.get("salaire"))
    origin = _mapping_or_empty(payload.get("origineOffre"))
    return NormalizedJobOffer(
        source="france_travail",
        source_offer_id=offer_id,
        title=title,
        description=_optional_string(payload.get("description")),
        company_name=_optional_string(company.get("nom")),
        location_label=_optional_string(location.get("libelle")),
        commune=_optional_string(location.get("commune")),
        department_code=VAL_DE_MARNE_DEPARTMENT,
        created_at=_optional_string(payload.get("dateCreation")),
        updated_at=_optional_string(payload.get("dateActualisation")),
        contract_type=_optional_string(payload.get("typeContrat")),
        salary=_optional_string(salary.get("libelle")),
        source_url=_optional_string(origin.get("urlOrigine")),
        origin=_optional_string(origin.get("origine")),
    )


def _parse_content_range(content_range: Optional[str]) -> Optional[ContentRange]:
    if not content_range:
        return None
    match = _CONTENT_RANGE_PATTERN.match(content_range)
    if not match:
        return None
    total_count = None if match.group(3) == "*" else int(match.group(3))
    return ContentRange(start=int(match.group(1)), end=int(match.group(2)), total_count=total_count)


def _validate_content_range(
    content_range: Optional[ContentRange], offset: int, result_count: int
) -> None:
    """Accept server-enforced page lengths, but reject overlap or malformed ranges."""
    if content_range is None:
        return
    if content_range.start != offset:
        raise FranceTravailOffersError(
            "France Travail returned a page that does not begin at the requested offset."
        )
    if content_range.end < content_range.start:
        raise FranceTravailOffersError("France Travail returned an invalid Content-Range.")
    if content_range.total_count is not None and content_range.end >= content_range.total_count:
        raise FranceTravailOffersError("France Travail returned an invalid Content-Range total.")
    if content_range.end - content_range.start + 1 != result_count:
        raise FranceTravailOffersError(
            "France Travail Content-Range does not match the number of returned offers."
        )


def _next_offset(
    offset: int,
    limit: int,
    result_count: int,
    content_range: Optional[ContentRange],
) -> Optional[int]:
    candidate = content_range.end + 1 if content_range else offset + result_count
    if content_range and content_range.total_count is not None:
        return candidate if candidate < content_range.total_count else None
    # France Travail can enforce a server-side page size larger than the requested Range.
    # Without a total, continue while it returned at least one requested page.
    return candidate if result_count >= limit else None


def _normalize_results(results: Sequence[Any]) -> tuple[Sequence[NormalizedJobOffer], int]:
    normalized = []
    skipped = 0
    for result in results:
        if not isinstance(result, Mapping):
            skipped += 1
            continue
        try:
            normalized.append(normalize_offer(result))
        except FranceTravailOffersError:
            skipped += 1
    return tuple(normalized), skipped


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _required_string(value: Any, field_name: str) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise FranceTravailOffersError(f"France Travail offer has no {field_name}.")
    return normalized


def _format_api_datetime(value: datetime) -> str:
    """The v2 API expects a UTC ISO-8601 timestamp without fractional seconds."""
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
