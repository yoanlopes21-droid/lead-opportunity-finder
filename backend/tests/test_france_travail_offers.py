from __future__ import annotations

import httpx
import pytest
from datetime import datetime, timezone

from app.config import Settings
from app.services.france_travail.offers import (
    FranceTravailOffersClient,
    FranceTravailOffersError,
    CreationDateWindow,
)


class FakeResponse:
    def __init__(self, payload: dict, headers: dict | None = None, status_code: int = 200):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class FakeAuthClient:
    def get_access_token(self) -> str:
        return "test-access-token"


def offer_payload() -> dict:
    return {
        "id": "194ABCD",
        "intitule": "Technicien maintenance H/F",
        "description": "Assurer la maintenance préventive.",
        "dateCreation": "2026-09-15T08:00:00.000Z",
        "dateActualisation": "2026-09-16T08:00:00.000Z",
        "lieuTravail": {"libelle": "Créteil", "commune": "94028"},
        "entreprise": {"nom": "Entreprise Exemple"},
        "typeContrat": "CDI",
        "salaire": {"libelle": "Selon profil"},
        "origineOffre": {"origine": "1", "urlOrigine": "https://example.test/offre/194ABCD"},
    }


def client() -> FranceTravailOffersClient:
    settings = Settings(_env_file=None)
    return FranceTravailOffersClient(settings, FakeAuthClient(), sleeper=lambda _: None)


def test_department_page_normalizes_offers_and_uses_only_department_94(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse({"resultats": [offer_payload()]}, {"Content-Range": "0-0/23"})

    monkeypatch.setattr(httpx, "get", fake_get)
    page = client().search_department_page(offset=0, limit=1)

    assert calls[0][1]["params"] == {"departement": "94", "range": "0-0"}
    assert "Range" not in calls[0][1]["headers"]
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-access-token"
    assert page.total_count == 23
    assert page.http_status == 200
    assert page.next_offset == 1
    assert page.skipped_offers == 0
    assert page.offers[0].source == "france_travail"
    assert page.offers[0].source_offer_id == "194ABCD"
    assert page.offers[0].company_name == "Entreprise Exemple"
    assert page.offers[0].department_code == "94"
    assert page.offers[0].source_url == "https://example.test/offre/194ABCD"


def test_creation_date_filters_are_utc_query_parameters(monkeypatch):
    calls = []
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: calls.append(kwargs)
        or FakeResponse({"resultats": [offer_payload()]}, {"Content-Range": "offres 0-0/1"}),
    )
    window = CreationDateWindow(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 12, 30, tzinfo=timezone.utc),
    )

    client().search_department_page(limit=1, creation_window=window)

    assert calls[0]["params"] == {
        "departement": "94",
        "range": "0-0",
        "minCreationDate": "2026-01-01T00:00:00Z",
        "maxCreationDate": "2026-01-02T12:30:00Z",
    }


def test_pagination_stops_when_content_range_is_exhausted(monkeypatch):
    offsets = []

    def fake_get(*args, **kwargs):
        offsets.append(kwargs["params"]["range"])
        if len(offsets) == 1:
            return FakeResponse({"resultats": [offer_payload(), offer_payload() | {"id": "194EFGH"}]}, {"Content-Range": "0-1/3"})
        return FakeResponse({"resultats": [offer_payload() | {"id": "194IJKL"}]}, {"Content-Range": "2-2/3"})

    monkeypatch.setattr(httpx, "get", fake_get)
    pages = list(client().iter_department_pages(page_size=2))

    assert offsets == ["0-1", "2-3"]
    assert [len(page.offers) for page in pages] == [2, 1]
    assert pages[-1].next_offset is None


def test_pagination_handles_server_enforced_page_size_without_total(monkeypatch):
    ranges = []

    def fake_get(*args, **kwargs):
        ranges.append(kwargs["params"]["range"])
        if len(ranges) == 1:
            return FakeResponse({"resultats": [offer_payload(), offer_payload() | {"id": "194EFGH"}]})
        return FakeResponse({"resultats": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    pages = list(client().iter_department_pages(page_size=1))

    assert ranges == ["0-0", "2-2"]
    assert pages[0].next_offset == 2
    assert pages[-1].next_offset is None


def test_content_range_with_unit_is_used_for_total_count(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"resultats": [offer_payload()]}, {"Content-Range": "items 0-0/23"}
        ),
    )

    page = client().search_department_page(limit=1)

    assert page.total_count == 23


def test_content_range_end_is_used_to_advance_pagination(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"resultats": [_offer_with_id(index) for index in range(150)]},
            {"Content-Range": "items 0-149/300"},
        ),
    )

    page = client().search_department_page(limit=1)

    assert page.next_offset == 150


def test_malformed_offer_is_skipped_without_failing_page(monkeypatch):
    malformed_offer = offer_payload() | {"id": None}
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: FakeResponse({"resultats": [offer_payload(), malformed_offer]}),
    )

    page = client().search_department_page(limit=1)

    assert len(page.offers) == 1
    assert page.skipped_offers == 1
    assert page.next_offset == 2


def test_client_waits_between_successive_requests(monkeypatch):
    clock_values = iter([0.0, 0.0, 0.25])
    delays = []
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse({"resultats": []}))
    rate_limited_client = FranceTravailOffersClient(
        Settings(_env_file=None),
        FakeAuthClient(),
        clock=lambda: next(clock_values),
        sleeper=delays.append,
    )

    rate_limited_client.search_department_page(limit=1)
    rate_limited_client.search_department_page(limit=1)

    assert delays == [0.25]


def test_invalid_payload_is_rejected_without_using_local_environment(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse({"unexpected": []}))

    with pytest.raises(FranceTravailOffersError):
        client().search_department_page(limit=1)


def test_real_observed_header_pagination_is_rejected_when_second_page_overlaps(monkeypatch):
    """Reproduces the diagnostic: a Range HTTP header was ignored by the API."""
    sent_ranges = []
    first_page = [_offer_with_id(index) for index in range(150)]

    def fake_get(*args, **kwargs):
        sent_ranges.append(kwargs["params"]["range"])
        return FakeResponse({"resultats": first_page}, {"Content-Range": "offres 0-149/6129"}, 206)

    monkeypatch.setattr(httpx, "get", fake_get)

    pages = client().iter_department_pages(page_size=150)
    first = next(pages)
    assert first.next_offset == 150
    with pytest.raises(FranceTravailOffersError, match="does not begin"):
        next(pages)
    assert sent_ranges == ["0-149", "150-299"]


def test_query_range_handles_server_enforced_150_item_pages_through_completion(monkeypatch):
    sent_ranges = []
    responses = [
        ("offres 0-149/320", [_offer_with_id(index) for index in range(150)]),
        ("offres 150-299/320", [_offer_with_id(index) for index in range(150, 300)]),
        ("offres 300-319/320", [_offer_with_id(index) for index in range(300, 320)]),
    ]

    def fake_get(*args, **kwargs):
        sent_ranges.append(kwargs["params"]["range"])
        header, payload = responses.pop(0)
        return FakeResponse({"resultats": payload}, {"Content-Range": header}, 206)

    monkeypatch.setattr(httpx, "get", fake_get)
    pages = list(client().iter_department_pages(page_size=1))

    assert sent_ranges == ["0-0", "150-150", "300-300"]
    assert [page.next_offset for page in pages] == [150, 300, None]
    assert len({offer.source_offer_id for page in pages for offer in page.offers}) == 320


def test_invalid_content_range_without_progress_is_rejected(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"resultats": [offer_payload()]}, {"Content-Range": "offres 150-149/6129"}
        ),
    )

    with pytest.raises(FranceTravailOffersError, match="invalid Content-Range"):
        client().search_department_page(offset=150, limit=1)


def test_repeated_page_without_content_range_is_rejected_before_an_infinite_loop(monkeypatch):
    repeated_payload = {"resultats": [offer_payload()]}
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(repeated_payload))

    pages = client().iter_department_pages(page_size=1)
    next(pages)
    with pytest.raises(FranceTravailOffersError, match="repeated page"):
        next(pages)


def _offer_with_id(index: int) -> dict:
    return offer_payload() | {"id": f"FT{index:05d}"}
