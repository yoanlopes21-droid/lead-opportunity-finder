"""Synthetic-only checks for dynamic provenance, direct postings and company seeds."""

from datetime import datetime, timezone
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ObservedJobOffer, CompanyDiscoverySeed, CommercialExclusion
from app.config import Settings
from app.services.non_ft_runs import create_non_ft_run
from app.services.company_first import eligible_seed
from app.services.contactability.providers.official_web.contracts import FetchedPage
from app.services.contactability.providers.official_web.discovery import classify_domain, WebsiteCandidateClassification
from app.services.collection.jobposting import parse_jobpostings
from app.services.collection.official_site import OfficialSiteJobProvider, ats_link
from app.services.collection.ashby import AshbyBoard, AshbyJobBoardProvider
from app.services.collection.workable import WorkableBoard, WorkableJobBoardProvider
from app.services.company_first import import_dinum_seed_page
from app.services.opportunities.company import aggregate_active_company_opportunities
from app.services.opportunities.provenance import non_ft_only


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def posting(**updates):
    value = {
        "@context": "https://schema.org", "@type": "JobPosting",
        "title": "Technicien", "description": "<p>Maintenance des équipements</p>",
        "datePosted": "2026-09-30", "validThrough": "2026-11-01",
        "hiringOrganization": {"@type": "Organization", "name": "Atelier Exemple"},
        "jobLocation": {"@type": "Place", "address": {
            "@type": "PostalAddress", "addressLocality": "Créteil", "postalCode": "94000", "addressCountry": "FR",
        }},
    }
    value.update(updates)
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


def observed(source, identifier, origin=None):
    return ObservedJobOffer(
        source=source, source_offer_id=identifier, title="Technicien",
        company_name="Atelier Exemple", location_label="Créteil", commune="creteil",
        department_code="94", created_at="2026-09-30", source_url=f"https://example.test/{identifier}",
        origin=origin, first_seen_at=NOW, last_seen_at=NOW, last_changed_at=NOW,
        is_active=True, observation_count=1,
    )


def test_non_ft_only_changes_when_france_travail_corrobates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direct = observed("employer_career_site", "direct", "official_web:example.test")
        session.add(direct); session.commit()
        first = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]
        assert first.active_offer_count == 1
        assert first.active_job_offers[0].non_ft_only
        assert first.active_job_offers[0].primary_provenance == "employer_direct"
        ft = observed("france_travail", "ft")
        session.add(ft); session.commit()
        second = aggregate_active_company_opportunities(session, now=NOW).opportunities[0]
        assert second.active_offer_count == 1
        assert not second.active_job_offers[0].non_ft_only
        assert set(second.active_job_offers[0].sources) == {"france_travail", "employer_career_site"}
        assert not non_ft_only((direct, ft))
    engine.dispose()


def test_jobposting_requires_official_domain_freshness_identity_and_job_geography():
    url = "https://atelier.example/careers/technicien"
    good = parse_jobpostings(posting(), url, "Atelier Exemple", "atelier.example", now=NOW)
    assert len(good) == 1 and good[0].department_code == "94"
    assert good[0].commune == "creteil"
    assert not parse_jobpostings(posting(), url, "Atelier Exemple", "other.example", now=NOW)
    assert not parse_jobpostings(posting(jobLocation=None), url, "Atelier Exemple", "atelier.example", now=NOW)
    assert not parse_jobpostings(posting(hiringOrganization={"name": "Autre"}), url, "Atelier Exemple", "atelier.example", now=NOW)
    assert not parse_jobpostings(posting(validThrough="2026-09-01"), url, "Atelier Exemple", "atelier.example", now=NOW)
    assert not parse_jobpostings(posting(datePosted=None), url, "Atelier Exemple", "atelier.example", now=NOW)
    assert not parse_jobpostings("<html>Aucune offre</html>", url, "Atelier Exemple", "atelier.example", now=NOW)


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_public_ats_feeds_need_explicit_job_location():
    ashby = AshbyJobBoardProvider(AshbyBoard("atelier", "Atelier Exemple"), requester=lambda *a, **kw: Response({"jobs": [
        {"title": "Technicien", "location": "Créteil", "jobUrl": "https://jobs.ashbyhq.com/atelier/1", "publishedAt": "2026-09-30"},
        {"title": "Paris", "location": "Paris", "jobUrl": "https://jobs.ashbyhq.com/atelier/2"},
    ]}))
    assert len(tuple(ashby.iter_pages())[0].offers) == 1
    workable = WorkableJobBoardProvider(WorkableBoard("atelier", "Atelier Exemple"), requester=lambda *a, **kw: Response({"jobs": [
        {"title": "Technicien", "city": "Créteil", "country": "France", "shortcode": "X1", "application_url": "https://apply.workable.com/atelier/j/X1"},
        {"title": "Paris", "city": "Paris", "country": "France", "shortcode": "X2", "application_url": "https://apply.workable.com/atelier/j/X2"},
    ]}))
    assert len(tuple(workable.iter_pages())[0].offers) == 1
    assert ats_link("https://jobs.ashbyhq.com/atelier") == ("ashby", "atelier")
    assert ats_link("https://evil.example/atelier") is None


def test_dinum_seed_is_not_recruitment_evidence():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        payload = {"results": [{
            "siren": "123456789", "nom_complet": "Atelier Exemple", "etat_administratif": "A",
            "categorie_entreprise": "PME",
            "matching_etablissements": [{"siret": "12345678900011", "code_postal": "94000", "libelle_commune": "Créteil", "etat_administratif": "A"}],
        }]}
        assert import_dinum_seed_page(session, requester=lambda *a, **kw: Response(payload)) == 1
        assert session.query(CompanyDiscoverySeed).count() == 1
        assert aggregate_active_company_opportunities(session).opportunities == ()
    engine.dispose()


def test_verified_site_crawl_follows_bounded_career_links_and_collects_only_jobposting():
    home_url = "https://atelier.example/"
    career_url = "https://atelier.example/recrutement"
    job_url = "https://atelier.example/jobs/technicien"

    class Fetcher:
        def __init__(self):
            self.calls = []

        def set_robots_checker(self, checker):
            self.checker = checker

        def fetch(self, url, *, initial=False):
            self.calls.append(url)
            links = {
                home_url: (career_url, "https://jobs.ashbyhq.com/atelier"),
                career_url: (job_url,),
                job_url: (),
            }[url]
            return FetchedPage(url, url, 200, "text/html", "", links, NOW,
                               posting() if url == job_url else "<html></html>")

    fetcher = Fetcher()
    provider = OfficialSiteJobProvider("Atelier Exemple", home_url, fetcher=fetcher)
    pages = tuple(provider.iter_pages())
    assert len(pages[0].offers) == 1
    assert pages[0].offers[0].source_url == job_url
    assert provider.ats_candidates == (("ashby", "atelier"),)
    assert fetcher.calls == [home_url, career_url, job_url]
    assert provider.can_deactivate_unseen is False


def test_excluded_seed_is_rejected_before_web_and_zero_brave_run_is_supported():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed = CompanyDiscoverySeed(siren="123456789", siret="12345678900011",
                                    company_name="Atelier Exemple", commune="Créteil")
        session.add(seed)
        session.add(CommercialExclusion(company_key="atelier exemple", company_name_snapshot="Atelier Exemple",
                                        exclusion_type="manual_exclusion", reason="Synthetic exclusion",
                                        starts_at=NOW))
        session.commit()
        assert not eligible_seed(session, seed)
        run, created = create_non_ft_run(session, Settings(_env_file=None), brave_max_requests=0)
        assert created and run.brave_hard_cap == 0
    engine.dispose()


def test_company_directory_subdomains_cannot_become_official_sites():
    for host in ("annuaire-entreprises.data.gouv.fr", "labonnealternance.apprentissage.beta.gouv.fr",
                 "www.emploi-collectivites.fr", "fr.kompass.com"):
        assert classify_domain(host)[0] == WebsiteCandidateClassification.EXCLUDED
