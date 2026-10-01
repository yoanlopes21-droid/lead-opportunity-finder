"""Documented public Ashby job-board feed, scoped to one verified employer."""

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import httpx

from app.services.collection.geography import explicit_commune, is_val_de_marne
from app.services.collection.greenhouse import EMPLOYER_CAREER_SOURCE
from app.services.collection.providers import ProviderPage
from app.services.persistence.offers import OfferSnapshot


@dataclass(frozen=True)
class AshbyBoard:
    name: str
    company_name: str


class AshbyJobBoardProvider:
    provider_id = EMPLOYER_CAREER_SOURCE
    scope_type = "provider_board"
    can_deactivate_unseen = True

    def __init__(self, board: AshbyBoard, *, requester: Callable[..., Any] = httpx.get):
        self.board = board
        self.scope_value = f"ashby:{board.name}"
        self.requester = requester

    def iter_pages(self) -> Iterable[ProviderPage]:
        response = self.requester(
            f"https://api.ashbyhq.com/posting-api/job-board/{self.board.name}",
            headers={"Accept": "application/json", "User-Agent": "LeadOpportunityFinder/0.1"},
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError("Ashby public board request failed")
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Ashby public board payload is incomplete")
        offers = []
        for row in payload["jobs"]:
            if not isinstance(row, Mapping) or row.get("isListed") is False:
                continue
            location = row.get("location")
            title = row.get("title")
            url = row.get("jobUrl") or row.get("applyUrl")
            identifier = row.get("id") or url
            if not all(isinstance(value, str) and value.strip() for value in (location, title, url, identifier)):
                continue
            if not is_val_de_marne(location):
                continue
            offers.append(OfferSnapshot(
                source=EMPLOYER_CAREER_SOURCE,
                source_offer_id=f"ashby:{self.board.name}:{identifier}",
                title=title.strip(), description=row.get("descriptionPlain") if isinstance(row.get("descriptionPlain"), str) else None,
                company_name=self.board.company_name, location_label=location,
                commune=explicit_commune(location), department_code="94",
                created_at=row.get("publishedAt") if isinstance(row.get("publishedAt"), str) else None,
                contract_type=row.get("employmentType") if isinstance(row.get("employmentType"), str) else None,
                source_url=url, discovery_provider="ashby", origin=self.scope_value,
            ))
        yield ProviderPage(page_number=1, offers=tuple(offers), is_last=True)
