"""Documented public Workable account jobs feed."""

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import httpx

from app.services.collection.geography import explicit_commune, is_val_de_marne
from app.services.collection.greenhouse import EMPLOYER_CAREER_SOURCE
from app.services.collection.providers import ProviderPage
from app.services.persistence.offers import OfferSnapshot


@dataclass(frozen=True)
class WorkableBoard:
    subdomain: str
    company_name: str


class WorkableJobBoardProvider:
    provider_id = EMPLOYER_CAREER_SOURCE
    scope_type = "provider_board"
    can_deactivate_unseen = True

    def __init__(self, board: WorkableBoard, *, requester: Callable[..., Any] = httpx.get):
        self.board = board
        self.scope_value = f"workable:{board.subdomain}"
        self.requester = requester

    def iter_pages(self) -> Iterable[ProviderPage]:
        response = self.requester(
            f"https://www.workable.com/api/accounts/{self.board.subdomain}",
            params={"details": "true"},
            headers={"Accept": "application/json", "User-Agent": "LeadOpportunityFinder/0.1"},
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError("Workable public jobs request failed")
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Workable public jobs payload is incomplete")
        offers = []
        for row in payload["jobs"]:
            if not isinstance(row, Mapping):
                continue
            location = row.get("city")
            title = row.get("title")
            url = row.get("application_url") or row.get("url")
            identifier = row.get("shortcode") or row.get("code")
            if not all(isinstance(value, str) and value.strip() for value in (location, title, url, identifier)):
                continue
            if row.get("country") not in {None, "France", "FR"} or not is_val_de_marne(location):
                continue
            offers.append(OfferSnapshot(
                source=EMPLOYER_CAREER_SOURCE,
                source_offer_id=f"workable:{self.board.subdomain}:{identifier}",
                title=title.strip(), description=row.get("description") if isinstance(row.get("description"), str) else None,
                company_name=self.board.company_name, location_label=location,
                commune=explicit_commune(location), department_code="94",
                created_at=row.get("published_on") if isinstance(row.get("published_on"), str) else None,
                contract_type=row.get("employment_type") if isinstance(row.get("employment_type"), str) else None,
                source_url=url, discovery_provider="workable", origin=self.scope_value,
            ))
        yield ProviderPage(page_number=1, offers=tuple(offers), is_last=True)
