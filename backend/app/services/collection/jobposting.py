"""Conservative JSON-LD JobPosting extraction from a verified employer page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
from typing import Mapping
from urllib.parse import urlsplit

from app.services.collection.geography import explicit_commune, is_val_de_marne
from app.services.collection.greenhouse import EMPLOYER_CAREER_SOURCE
from app.services.opportunities.company import normalize_company_key
from app.services.persistence.offers import OfferSnapshot


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json = False
        self.current = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        self.in_json = tag == "script" and dict(attrs).get("type", "").casefold() == "application/ld+json"
        if self.in_json:
            self.current = []

    def handle_data(self, data):
        if self.in_json:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            self.scripts.append("".join(self.current))
            self.in_json = False


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _objects(value):
    if isinstance(value, list):
        for item in value:
            yield from _objects(item)
    elif isinstance(value, Mapping):
        if "@graph" in value:
            yield from _objects(value["@graph"])
        yield value


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def parse_jobpostings(html: str, page_url: str, company_name: str, official_domain: str, *, now: datetime | None = None) -> tuple[OfferSnapshot, ...]:
    """Reject third-party, stale, unattributed and geographically uncertain postings."""
    if (urlsplit(page_url).hostname or "").casefold() != official_domain.casefold():
        return ()
    observed_at = now or datetime.now(timezone.utc)
    parser = _Scripts()
    parser.feed(html)
    offers = []
    for script in parser.scripts:
        try:
            document = json.loads(script)
        except ValueError:
            continue
        for job in _objects(document):
            types = job.get("@type")
            if "JobPosting" not in (types if isinstance(types, list) else [types]):
                continue
            title, description = job.get("title"), job.get("description")
            posted = _date(job.get("datePosted"))
            expiry = _date(job.get("validThrough"))
            if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not description.strip():
                continue
            if posted is None or posted > observed_at + timedelta(days=1) or posted < observed_at - timedelta(days=180):
                continue
            if expiry is not None and expiry < observed_at:
                continue
            hiring = job.get("hiringOrganization")
            if isinstance(hiring, Mapping) and isinstance(hiring.get("name"), str):
                if normalize_company_key(hiring["name"]) != normalize_company_key(company_name):
                    continue
            locations = job.get("jobLocation")
            locations = locations if isinstance(locations, list) else [locations]
            labels = []
            for location in locations:
                address = location.get("address") if isinstance(location, Mapping) else None
                if not isinstance(address, Mapping):
                    continue
                country = address.get("addressCountry")
                if isinstance(country, str) and country.casefold() not in {"fr", "fra", "france"}:
                    continue
                label = " ".join(str(address[key]) for key in ("addressLocality", "postalCode", "addressRegion") if isinstance(address.get(key), str))
                if is_val_de_marne(label):
                    labels.append(label)
            if not labels:
                continue
            declared_url = job.get("url")
            if isinstance(declared_url, str) and declared_url.strip() and urlsplit(declared_url).hostname != official_domain:
                continue
            url = declared_url if isinstance(declared_url, str) and declared_url.strip() else page_url
            identifier = job.get("identifier")
            if isinstance(identifier, Mapping):
                identifier = identifier.get("value")
            if not isinstance(identifier, str) or not identifier.strip():
                identifier = hashlib.sha256(url.encode()).hexdigest()[:32]
            text = _Text()
            text.feed(description)
            offers.append(OfferSnapshot(
                source=EMPLOYER_CAREER_SOURCE, source_offer_id=f"official_web:{official_domain}:{identifier}"[:255],
                title=title.strip(), description=" ".join(text.parts).strip()[:10000],
                company_name=company_name, location_label=labels[0], commune=explicit_commune(labels[0]),
                department_code="94", created_at=posted.isoformat(),
                valid_through=expiry.isoformat() if expiry else None,
                contract_type=job.get("employmentType") if isinstance(job.get("employmentType"), str) else None,
                source_url=url, discovery_provider="official_web", origin=f"official_web:{official_domain}",
            ))
    return tuple(offers)
