"""Small injectable Brave Search client used only for domain discovery."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Optional

import httpx

from app.services.contactability.providers.official_web.contracts import BraveSearchResult
from app.services.brave_usage import BraveBudgetExceeded


class BraveSearchError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class BraveSearchClient:
    def __init__(
        self,
        *,
        api_key: str,
        usage_service,
        base_url: str = "https://api.search.brave.com/res/v1/web/search",
        timeout_seconds: float = 10.0,
        requests_per_second: float = 1.0,
        requester: Optional[Callable[..., Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        run_hard_cap: Optional[int] = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Brave Search API key is required")
        if timeout_seconds <= 0 or requests_per_second <= 0:
            raise ValueError("timeout and rate limit must be positive")
        if usage_service is None:
            raise ValueError("Brave Search usage service is required")
        self._api_key = api_key.strip()
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._minimum_interval = 1.0 / requests_per_second
        self._requester = requester or httpx.get
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._last_request_at: Optional[float] = None
        self._usage_service = usage_service
        self._run_id: Optional[int] = None
        self._run_hard_cap = run_hard_cap

    def set_run_id(self, run_id: Optional[int]) -> None:
        self._run_id = run_id

    def search(
        self, query: str, *, count: int = 5, company_key: Optional[str] = None,
        request_index: Optional[int] = None,
    ) -> tuple[BraveSearchResult, ...]:
        reservation = None
        reservation = self._usage_service.reserve_request(
            run_id=self._run_id, company_key=company_key, query=query,
            request_index=request_index, run_hard_cap=self._run_hard_cap,
        )
        self._rate_limit()
        try:
            response = self._requester(
                self._base_url,
                headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
                params={"q": query, "count": min(max(count, 1), 5)},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            self._record(reservation, "timeout")
            raise BraveSearchError("timeout", "Brave Search request timed out") from exc
        except httpx.HTTPError as exc:
            self._record(reservation, "network_error")
            raise BraveSearchError("network", "Brave Search request failed") from exc
        finally:
            self._last_request_at = self._monotonic()
        status = int(getattr(response, "status_code", 0))
        if status == 429:
            self._record(reservation, "rate_limited")
            raise BraveSearchError("rate_limited", "Brave Search rate limit reached")
        if 500 <= status <= 599:
            self._record(reservation, "server_error")
            raise BraveSearchError("server_error", "Brave Search service error")
        if not 200 <= status <= 299:
            self._record(reservation, "http_error")
            raise BraveSearchError("http_error", "Brave Search request was rejected")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            self._record(reservation, "invalid_response")
            raise BraveSearchError("invalid_response", "Brave Search returned invalid JSON") from exc
        self._record(reservation, "completed")
        return _parse_results(payload, limit=min(max(count, 1), 5))

    def _record(self, reservation, outcome: str) -> None:
        if reservation is not None:
            self._usage_service.record_outcome(reservation.id, outcome)

    def _rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self._minimum_interval - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleeper(remaining)


def _parse_results(payload: Any, *, limit: int) -> tuple[BraveSearchResult, ...]:
    if not isinstance(payload, Mapping):
        return ()
    web = payload.get("web")
    if not isinstance(web, Mapping) or not isinstance(web.get("results"), list):
        return ()
    parsed = []
    for item in web["results"][:limit]:
        if not isinstance(item, Mapping) or not isinstance(item.get("url"), str):
            continue
        parsed.append(BraveSearchResult(
            url=item["url"],
            title=_short_text(item.get("title"), 500),
            description=_short_text(item.get("description"), 1000),
        ))
    return tuple(parsed)


def _short_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split()).strip()
    return compact[:limit] or None
