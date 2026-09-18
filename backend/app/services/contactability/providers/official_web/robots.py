"""A small, fail-closed and injectable robots.txt policy for web extraction."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from typing import Optional


class RobotsTxtPolicy:
    """Fetch and cache robots decisions per origin; any uncertainty denies exploration."""

    def __init__(self, fetcher: object) -> None:
        self._fetcher = fetcher
        self._policies: dict[str, Optional[RobotFileParser]] = {}

    def allowed(self, url: str, user_agent: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        origin = f"{parsed.scheme.casefold()}://{parsed.hostname.casefold()}"
        parser = self._policies.get(origin, _MISSING)
        if parser is _MISSING:
            parser = self._load(origin)
            self._policies[origin] = parser
        return bool(parser and parser.can_fetch(user_agent, url))

    def _load(self, origin: str) -> Optional[RobotFileParser]:
        robots_url = urlunsplit((*urlsplit(origin)[:2], "/robots.txt", "", ""))
        try:
            page = self._fetcher.fetch(robots_url, initial=True)
        except Exception:
            return None
        # A redirect to another origin is not a reliable policy for this origin.
        if urlsplit(page.final_url).netloc.casefold() != urlsplit(origin).netloc.casefold():
            return None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(page.text.splitlines())
        return parser


_MISSING = object()
