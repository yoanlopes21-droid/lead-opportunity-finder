"""Bounded JobPosting discovery on a verified employer domain."""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit, urljoin, urlunsplit, unquote
from html.parser import HTMLParser
import re

from app.services.collection.greenhouse import EMPLOYER_CAREER_SOURCE
from app.services.collection.jobposting import parse_jobpostings
from app.services.collection.providers import ProviderPage
from app.services.contactability.providers.official_web.fetcher import SecureWebFetcher
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy


_CAREER_LABEL = re.compile(r"\b(carri[eè]res?|recrutements?|rejoindre|jobs?|offres? d.emploi)\b", re.I)
_CAREER_PATH = re.compile(r"(?:^|[/_-])(?:careers?|carriere|carrieres|recrutement|rejoindre|jobs?|offres?|offres?-d-emploi)(?:$|[/_.-])", re.I)
_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
}


def ats_link(url: str):
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        return None
    provider = _ATS_HOSTS.get((parsed.hostname or "").casefold())
    segments = [part for part in parsed.path.split("/") if part]
    if provider and segments and segments[0].replace("-", "").replace("_", "").isalnum():
        return provider, segments[0].casefold()
    return None


class OfficialSiteJobProvider:
    provider_id = EMPLOYER_CAREER_SOURCE
    scope_type = "provider_board"
    can_deactivate_unseen = False  # A bounded crawl cannot prove every old page disappeared.

    def __init__(self, company_name: str, site_url: str, *, fetcher: SecureWebFetcher | None = None,
                 known_pages: tuple[str, ...] = ()):
        parsed = urlsplit(site_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("verified HTTPS employer site required")
        self.company_name = company_name
        self.site_url = site_url
        self.domain = parsed.hostname.casefold()
        self.scope_value = f"official_web:{self.domain}"
        self.fetcher = fetcher or SecureWebFetcher()
        policy = RobotsTxtPolicy(self.fetcher)
        self.fetcher.set_robots_checker(policy.allowed)
        self.known_pages = known_pages
        self.career_pages: tuple[str, ...] = ()
        self.ats_candidates: tuple[tuple[str, str], ...] = ()
        self.jobposting_stats: dict[str, int] = {}

    def iter_pages(self) -> Iterable[ProviderPage]:
        home = self.fetcher.fetch(self.site_url, initial=False)
        if urlsplit(home.final_url).hostname != self.domain:
            raise ValueError("employer site redirected to another domain")
        semantic = _semantic_links(home.html, home.final_url)
        links = tuple(home.links) + semantic
        candidates = [_page_url(url) for url in links if (_career_link(url) or url in semantic)
                      and _same_domain(url, self.domain) and _page_url(url) != _page_url(home.final_url)]
        known = [_page_url(url) for url in self.known_pages if _same_domain(url, self.domain) and _career_link(url)]
        pages = [home.final_url, *dict.fromkeys(known + candidates)][:5]
        # A public sitemap is consulted only if homepage links expose no careers path.
        if len(pages) == 1:
            try:
                sitemap = self.fetcher.fetch(f"https://{self.domain}/sitemap.xml", initial=False)
                pages += [url for url in _sitemap_links(sitemap.text, self.domain) if url not in pages][:3]
            except Exception:
                pass
        all_links = list(links)
        fetched = set()
        offers = []
        for url in pages:
            if url in fetched:
                continue
            try:
                page = home if url == home.final_url else self.fetcher.fetch(url, initial=False)
            except Exception:
                continue
            if not _same_domain(page.final_url, self.domain):
                all_links.append(page.final_url)  # bounded employer redirect to a public ATS
                continue
            fetched.add(url)
            all_links.extend(page.links)
            all_links.extend(_semantic_links(page.html, page.final_url))
            offers.extend(parse_jobpostings(page.html, page.final_url, self.company_name, self.domain,
                                            diagnostics=self.jobposting_stats))
        # Follow only a few job links found on confirmed career pages.
        job_links = [_page_url(url) for url in all_links if _same_domain(url, self.domain) and _career_link(url) and _page_url(url) not in fetched]
        for url in dict.fromkeys(job_links):
            if len(fetched) >= 12:
                break
            try:
                page = self.fetcher.fetch(url, initial=False)
            except Exception:
                continue
            if not _same_domain(page.final_url, self.domain):
                continue
            fetched.add(url)
            offers.extend(parse_jobpostings(page.html, page.final_url, self.company_name, self.domain,
                                            diagnostics=self.jobposting_stats))
        self.career_pages = tuple(sorted(url for url in fetched if _career_link(url) or url in candidates or url in known))
        self.ats_candidates = tuple(sorted(set(candidate for url in all_links if (candidate := ats_link(url)))))
        deduped = {offer.source_offer_id: offer for offer in offers}
        yield ProviderPage(page_number=1, offers=tuple(deduped.values()), is_last=True)


def _same_domain(url: str, domain: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() == domain


def _career_link(url: str) -> bool:
    path = unquote(urlsplit(url).path).casefold()
    return bool(_CAREER_PATH.search(path))


def _page_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _sitemap_links(text: str, domain: str) -> tuple[str, ...]:
    import re
    from html import unescape
    return tuple(url for value in re.findall(r"<loc>\s*([^<]+)\s*</loc>", text, re.I)
                 if _same_domain((url := unescape(value.strip())), domain) and _career_link(url))[:20]


class _CareerLinkParser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[str] = []
        self.anchor: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "iframe" and isinstance(data.get("src"), str):
            self.links.append(urljoin(self.base, data["src"]))
        if tag == "a" and isinstance(data.get("href"), str):
            self.anchor = urljoin(self.base, data["href"])
            self.label = []

    def handle_data(self, value):
        if self.anchor:
            self.label.append(value)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            if _CAREER_LABEL.search(" ".join(self.label)):
                self.links.append(self.anchor)
            self.anchor = None
            self.label = []


def _semantic_links(html: str, base: str) -> tuple[str, ...]:
    parser = _CareerLinkParser(base)
    parser.feed(html or "")
    return tuple(url for url in dict.fromkeys(parser.links) if urlsplit(url).scheme == "https")[:40]
