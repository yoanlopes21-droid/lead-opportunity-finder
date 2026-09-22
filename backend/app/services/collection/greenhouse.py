"""Public Greenhouse job-board provider for one explicitly configured employer."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

import httpx

from app.services.collection.providers import ProviderPage
from app.services.persistence.offers import OfferSnapshot


EMPLOYER_CAREER_SOURCE = "employer_career_site"
_POSTAL_94 = re.compile(r"(?<!\d)94\d{3}(?!\d)")
_COMMUNES_94 = {
    "ablon sur seine", "alfortville", "arcueil", "boissy saint leger", "bonneuil sur marne",
    "bry sur marne", "cachan", "champigny sur marne", "charenton le pont", "chennevieres sur marne",
    "chevilly larue", "choisy le roi", "creteil", "fontenay sous bois", "fresnes", "gentilly",
    "ivry sur seine", "joinville le pont", "la queue en brie", "le kremlin bicetre", "le perreux sur marne",
    "le plessis trevise", "lhay les roses", "limeil brevannes", "maisons alfort", "mandres les roses",
    "marolles en brie", "nogent sur marne", "noiseau", "orly", "ormesson sur marne", "perigny",
    "rungis", "saint mande", "saint maur des fosses", "saint maurice", "santeny", "sucy en brie",
    "thiais", "valenton", "villecresnes", "villejuif", "villeneuve le roi", "villeneuve saint georges",
    "villiers sur marne", "vincennes", "vitry sur seine",
}


class GreenhouseProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class GreenhouseBoard:
    board_token: str
    company_name: str
    discovery_provider: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.board_token.strip() or not self.company_name.strip():
            raise ValueError("Greenhouse board token and company name are required")


class GreenhouseJobBoardProvider:
    """Read a complete public employer board and retain only explicit 94 locations."""

    provider_id = EMPLOYER_CAREER_SOURCE
    scope_type = "provider_board"
    can_deactivate_unseen = True

    def __init__(
        self,
        board: GreenhouseBoard,
        *,
        base_url: str = "https://boards-api.greenhouse.io/v1/boards",
        timeout_seconds: float = 10.0,
        requests_per_second: float = 1.0,
        requester: Optional[Callable[..., Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or requests_per_second <= 0:
            raise ValueError("timeout and rate limit must be positive")
        self.board = board
        self.scope_value = f"greenhouse:{board.board_token.strip()}"
        self._url = f"{base_url.rstrip('/')}/{board.board_token.strip()}/jobs"
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
                params={"content": "true"},
                headers={"Accept": "application/json", "User-Agent": "LeadOpportunityFinder/0.1"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GreenhouseProviderError("Greenhouse public board request failed") from exc
        finally:
            self._last_request_at = self._monotonic()
        if not 200 <= int(getattr(response, "status_code", 0)) <= 299:
            raise GreenhouseProviderError("Greenhouse public board rejected the request")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise GreenhouseProviderError("Greenhouse returned invalid JSON") from exc
        offers, skipped = _parse_board(payload, self.board, self.scope_value)
        yield ProviderPage(page_number=1, offers=offers, skipped_count=skipped, is_last=True)


def _parse_board(
    payload: Any, board: GreenhouseBoard, origin: str
) -> tuple[tuple[OfferSnapshot, ...], int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise GreenhouseProviderError("Greenhouse response does not contain a jobs list")
    parsed: list[OfferSnapshot] = []
    skipped = 0
    seen: set[str] = set()
    for item in payload["jobs"]:
        offer = _parse_job(item, board, origin)
        if offer is None or offer.source_offer_id in seen:
            skipped += 1
            continue
        seen.add(offer.source_offer_id)
        parsed.append(offer)
    return tuple(parsed), skipped


def _parse_job(
    item: Any, board: GreenhouseBoard, origin: str
) -> Optional[OfferSnapshot]:
    if not isinstance(item, Mapping):
        return None
    identifier = item.get("id")
    title = _text(item.get("title"))
    url = _text(item.get("absolute_url"))
    location = item.get("location")
    location_label = _text(location.get("name")) if isinstance(location, Mapping) else None
    if identifier is None or title is None or url is None or location_label is None:
        return None
    if not _is_val_de_marne(location_label):
        return None
    return OfferSnapshot(
        source=EMPLOYER_CAREER_SOURCE,
        source_offer_id=f"greenhouse:{board.board_token.strip()}:{identifier}",
        title=title,
        description=_text(item.get("content")),
        company_name=board.company_name.strip(),
        location_label=location_label,
        commune=_explicit_commune(location_label),
        department_code="94",
        created_at=None,
        updated_at=_text(item.get("updated_at")),
        contract_type=None,
        salary=None,
        source_url=url,
        discovery_provider=board.discovery_provider,
        origin=origin,
    )


def _is_val_de_marne(location: str) -> bool:
    normalized = _location_key(location)
    return bool(_POSTAL_94.search(location)) or any(
        re.search(rf"\b{re.escape(commune)}\b", normalized) for commune in _COMMUNES_94
    )


def _explicit_commune(location: str) -> Optional[str]:
    normalized = _location_key(location)
    matches = sorted(
        (commune for commune in _COMMUNES_94 if re.search(rf"\b{re.escape(commune)}\b", normalized)),
        key=len,
        reverse=True,
    )
    return matches[0] if matches else None


def _location_key(value: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    ascii_like = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", ascii_like)).strip()


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split()).strip()
    return compact or None
