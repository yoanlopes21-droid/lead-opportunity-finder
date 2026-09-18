"""Bounded public-web fetcher with redirect and SSRF protection."""

from __future__ import annotations

import ipaddress
import socket
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from app.services.contactability.providers.official_web.contracts import FetchedPage


class SecureFetchError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class SecureWebFetcher:
    """Fetch public HTML only; callers must authorize non-initial URLs via robots."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_redirects: int = 3,
        max_response_bytes: int = 1_048_576,
        requests_per_second_per_domain: float = 1.0,
        user_agent: str = "LeadOpportunityFinder/1.0 (+local contact research)",
        requester: Optional[Callable[..., Any]] = None,
        resolver: Optional[Callable[[str], Iterable[str]]] = None,
        robots_checker: Optional[Callable[[str, str], bool]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if timeout_seconds <= 0 or max_redirects < 0 or max_response_bytes <= 0:
            raise ValueError("invalid fetch limits")
        if requests_per_second_per_domain <= 0:
            raise ValueError("domain rate limit must be positive")
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._max_bytes = max_response_bytes
        self._minimum_interval = 1.0 / requests_per_second_per_domain
        self._user_agent = user_agent
        self._requester = requester
        self._resolver = resolver or _resolve_host
        self._robots_checker = robots_checker
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._now = now
        self._last_request_by_host: dict[str, float] = {}
        self._page_cache: dict[str, FetchedPage] = {}
        self.request_count = 0

    def fetch(self, url: str, *, initial: bool = False) -> FetchedPage:
        if url in self._page_cache:
            return self._page_cache[url]
        current = url
        for redirect_count in range(self._max_redirects + 1):
            parsed = _validate_public_url(current, self._resolver)
            if not initial:
                if self._robots_checker is None:
                    raise SecureFetchError("robots_unverified", "Additional page requires robots permission")
                if not self._robots_checker(current, self._user_agent):
                    raise SecureFetchError("robots_disallowed", "Page is disallowed by robots policy")
            self._rate_limit(parsed.hostname or "")
            try:
                self.request_count += 1
                if self._requester is None:
                    response = _bounded_request(
                        current, user_agent=self._user_agent, timeout=self._timeout,
                        max_bytes=self._max_bytes,
                    )
                else:
                    response = self._requester(
                        "GET", current, headers={"User-Agent": self._user_agent},
                        timeout=self._timeout, follow_redirects=False,
                    )
            except httpx.TimeoutException as exc:
                raise SecureFetchError("timeout", "Website request timed out") from exc
            except httpx.HTTPError as exc:
                raise SecureFetchError("network", "Website request failed") from exc
            self._last_request_by_host[parsed.hostname or ""] = self._monotonic()
            status = int(getattr(response, "status_code", 0))
            if status in {301, 302, 303, 307, 308}:
                location = getattr(response, "headers", {}).get("location")
                if not location:
                    raise SecureFetchError("invalid_redirect", "Redirect has no destination")
                if redirect_count >= self._max_redirects:
                    raise SecureFetchError("too_many_redirects", "Website exceeded redirect limit")
                current = urljoin(current, location)
                _validate_public_url(current, self._resolver)
                continue
            if not 200 <= status <= 299:
                raise SecureFetchError("http_error", "Website returned an unusable response")
            headers = getattr(response, "headers", {})
            content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().casefold()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise SecureFetchError("unsupported_content", "Only HTML or text pages are supported")
            length = headers.get("content-length")
            if length and str(length).isdigit() and int(length) > self._max_bytes:
                raise SecureFetchError("response_too_large", "Website response exceeds size limit")
            content = bytes(getattr(response, "content", b""))
            if len(content) > self._max_bytes:
                raise SecureFetchError("response_too_large", "Website response exceeds size limit")
            encoding = getattr(response, "encoding", None) or "utf-8"
            text = content.decode(encoding, errors="replace")
            visible_text, links = _parse_page(text, current, content_type)
            page = FetchedPage(
                requested_url=url, final_url=current, status_code=status,
                content_type=content_type, text=visible_text,
                links=links, fetched_at=self._now(),
            )
            self._page_cache[url] = page
            self._page_cache[current] = page
            return page
        raise SecureFetchError("too_many_redirects", "Website exceeded redirect limit")

    def _rate_limit(self, hostname: str) -> None:
        last = self._last_request_by_host.get(hostname)
        if last is None:
            return
        remaining = self._minimum_interval - (self._monotonic() - last)
        if remaining > 0:
            self._sleeper(remaining)


def _validate_public_url(url: str, resolver: Callable[[str], Iterable[str]]):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SecureFetchError("invalid_url", "Website URL is invalid") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise SecureFetchError("invalid_url", "Only HTTP(S) URLs are supported")
    if parsed.username or parsed.password:
        raise SecureFetchError("invalid_url", "Credential-bearing URLs are forbidden")
    if port is not None and port not in {80, 443}:
        raise SecureFetchError("forbidden_port", "Only standard web ports are allowed")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise SecureFetchError("ssrf_blocked", "Local destinations are forbidden")
    try:
        addresses = tuple(resolver(hostname))
    except (OSError, socket.gaierror) as exc:
        raise SecureFetchError("dns_error", "Website hostname could not be resolved") from exc
    if not addresses:
        raise SecureFetchError("dns_error", "Website hostname has no address")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SecureFetchError("dns_error", "Website resolved to an invalid address") from exc
        if not address.is_global:
            raise SecureFetchError("ssrf_blocked", "Non-public destinations are forbidden")
    return parsed


def _resolve_host(hostname: str) -> tuple[str, ...]:
    return tuple({item[4][0] for item in socket.getaddrinfo(hostname, None)})


class _BufferedResponse:
    def __init__(self, status_code: int, headers, content: bytes, encoding: Optional[str]) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.encoding = encoding


def _bounded_request(url: str, *, user_agent: str, timeout: float, max_bytes: int):
    """Stream the default transport so the in-memory size limit is effective."""
    with httpx.stream(
        "GET", url, headers={"User-Agent": user_agent}, timeout=timeout,
        follow_redirects=False,
    ) as response:
        length = response.headers.get("content-length")
        if length and str(length).isdigit() and int(length) > max_bytes:
            raise SecureFetchError("response_too_large", "Website response exceeds size limit")
        chunks = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise SecureFetchError("response_too_large", "Website response exceeds size limit")
            chunks.append(chunk)
        return _BufferedResponse(
            response.status_code, dict(response.headers), b"".join(chunks), response.encoding,
        )


class _PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if isinstance(href, str) and href.strip():
                joined = urljoin(self.base_url, href.strip())
                if urlsplit(joined).scheme in {"http", "https"}:
                    self.links.append(joined)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.text_parts.append(data.strip())


def _parse_page(content: str, base_url: str, content_type: str) -> tuple[str, tuple[str, ...]]:
    if content_type == "text/plain":
        return " ".join(content.split()), ()
    parser = _PageParser(base_url)
    parser.feed(content)
    return " ".join(" ".join(parser.text_parts).split()), tuple(dict.fromkeys(parser.links))
