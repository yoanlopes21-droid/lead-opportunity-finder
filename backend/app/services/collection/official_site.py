"""Bounded JobPosting discovery on a verified employer domain."""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

from app.services.collection.greenhouse import EMPLOYER_CAREER_SOURCE
from app.services.collection.jobposting import parse_jobpostings
from app.services.collection.providers import ProviderPage
from app.services.contactability.providers.official_web.fetcher import SecureWebFetcher
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy


_CAREER_TERMS = ("career", "jobs", "recrut", "rejoind", "nous-rejoindre", "emploi", "offre")
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

    def iter_pages(self) -> Iterable[ProviderPage]:
        home = self.fetcher.fetch(self.site_url, initial=False)
        if urlsplit(home.final_url).hostname != self.domain:
            raise ValueError("employer site redirected to another domain")
        links = tuple(home.links)
        candidates = [url for url in links if _career_link(url) and _same_domain(url, self.domain)]
        known = [url for url in self.known_pages if _same_domain(url, self.domain)]
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
                continue
            fetched.add(url)
            all_links.extend(page.links)
            offers.extend(parse_jobpostings(page.html, page.final_url, self.company_name, self.domain))
        # Follow only a few job links found on confirmed career pages.
        job_links = [url for url in all_links if _same_domain(url, self.domain) and _career_link(url) and url not in fetched]
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
            offers.extend(parse_jobpostings(page.html, page.final_url, self.company_name, self.domain))
        self.career_pages = tuple(sorted(fetched))
        self.ats_candidates = tuple(sorted(set(candidate for url in all_links if (candidate := ats_link(url)))))
        deduped = {offer.source_offer_id: offer for offer in offers}
        yield ProviderPage(page_number=1, offers=tuple(deduped.values()), is_last=True)


def _same_domain(url: str, domain: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() == domain


def _career_link(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return any(term in path for term in _CAREER_TERMS)


def _sitemap_links(text: str, domain: str) -> tuple[str, ...]:
    import re
    from html import unescape
    return tuple(url for value in re.findall(r"<loc>\s*([^<]+)\s*</loc>", text, re.I)
                 if _same_domain((url := unescape(value.strip())), domain) and _career_link(url))[:20]
