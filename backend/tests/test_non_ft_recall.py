"""Synthetic recall checks; these tests never contact public services."""

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import CollectionRun, CompanyDiscoverySeed, WebsiteCandidateRecord
from app.services.collection.official_site import OfficialSiteJobProvider
from app.services.collection.jobposting import parse_jobpostings
from app.services.contactability.providers.official_web.contracts import FetchedPage
from app.services.inpi_rne import InpiRneClient, InpiUnavailable, RneNotFound, RneNotReusable, declared_domains
from app.services.non_ft_runs import _count, _resolve_rne_seeds, _resolve_cached_seeds, _verify_seed_site, _verify_ats_search_board, create_non_ft_run
from app.services.company_first import attach_ats_candidates


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def payload(domain="atelier.example", *, commercial=True):
    return {"siren": "123456789", "diffusionINSEE": "O", "content": {
        "diffusionCommerciale": commercial,
        "personneMorale": {
            "identite": {"nomsDeDomaine": [{"nomDomaine": domain, "dateEffet": "2025-01-01"}]},
            "etablissementPrincipal": {"nomsDeDomaine": []},
        },
    }}


def test_rne_contract_present_absent_invalid_and_opposition():
    found = declared_domains(payload(), "123456789")
    assert [(item.domain, item.effective_date, item.source_path) for item in found] == [
        ("atelier.example", "2025-01-01", "content.personneMorale.identite.nomsDeDomaine")]
    assert declared_domains(payload("localhost"), "123456789") == ()
    try:
        declared_domains(payload(commercial=False), "123456789")
    except RneNotReusable:
        pass
    else:
        assert False, "commercial opposition must be distinguishable from no domain"
    restricted = payload()
    restricted["diffusionINSEE"] = "N"
    try:
        declared_domains(restricted, "123456789")
    except RneNotReusable:
        pass
    else:
        assert False, "restricted dissemination must not be treated as no domain"
    missing = payload()
    missing["content"]["personneMorale"]["identite"]["nomsDeDomaine"] = []
    assert declared_domains(missing, "123456789") == ()
    wrapped = {"siren": "123456789", "updatedAt": "2026-10-02T08:00:00Z",
               "formality": {"siren": "123456789", "diffusionINSEE": "O",
                             "diffusionCommerciale": True, "content": payload()["content"]}}
    wrapped_found = declared_domains(wrapped, "123456789")
    assert wrapped_found[0].source_path == "formality.content.personneMorale.identite.nomsDeDomaine"
    assert wrapped_found[0].source_updated_at == "2026-10-02T08:00:00Z"
    wrapped["formality"]["diffusionCommerciale"] = False
    try:
        declared_domains(wrapped, "123456789")
    except RneNotReusable:
        pass
    else:
        assert False, "wrapped commercial opposition must be refused"
    try:
        declared_domains(payload(), "987654321")
    except InpiUnavailable:
        pass
    else:
        assert False, "mismatched SIREN must be refused"


def test_inpi_client_login_then_targeted_siren_and_remote_error():
    calls = []

    class Response:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def json(self):
            return self.body

    def requester(method, url, **kwargs):
        calls.append((method, url))
        return Response(200, {"token": "x"} if method == "POST" else payload())

    client = InpiRneClient("", "", requester=requester)
    assert client.domains_for_siren("123456789")[0].domain == "atelier.example"
    assert calls == [("POST", "https://registre-national-entreprises.inpi.fr/api/sso/login"),
                     ("GET", "https://registre-national-entreprises.inpi.fr/api/companies/123456789")]
    client.requester = lambda *args, **kwargs: Response(500, {})
    try:
        client.domains_for_siren("123456789")
    except InpiUnavailable:
        pass
    else:
        assert False, "provider failure must remain optional"
    client.requester = lambda *args, **kwargs: Response(404, {})
    try:
        client.domains_for_siren("123456789")
    except RneNotFound:
        pass
    else:
        assert False, "HTTP 404 must not be counted as a record without domains"


def test_rne_verification_uses_accessible_site_identity_and_preserves_conflict(monkeypatch):
    class Fetcher:
        def set_robots_checker(self, checker):
            pass

        def fetch(self, url, *, initial=False):
            return FetchedPage(url, url, 200, "text/html", "Atelier Exemple SIREN 123456789",
                               (), NOW, "<html>Atelier Exemple SIREN 123456789</html>")

    monkeypatch.setattr("app.services.non_ft_runs.SecureWebFetcher", Fetcher)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed = CompanyDiscoverySeed(siren="123456789", siret="12345678900011",
                                    company_name="Atelier Exemple", commune="Créteil")
        session.add(seed)
        run, _ = create_non_ft_run(session, Settings(_env_file=None), brave_max_requests=0)
        assert _verify_seed_site(session, seed, ["https://atelier.example/"], source="inpi_rne",
                                 run=run, effective_date="2025-01-01")
        assert seed.domain_evidence["discovered_by"] == "inpi_rne"
        assert seed.domain_evidence["siren"] == seed.siren
        assert seed.domain_evidence["verification_status"] == "high_confidence"
        assert run.funnel_stats["domain_candidates"] == 1
        seed.official_site_url = "https://another.example/"
        assert not _verify_seed_site(session, seed, ["https://atelier.example/"], source="inpi_rne", run=run)
        assert seed.official_site_url == "https://another.example/"
    engine.dispose()


def test_missing_credentials_does_not_fail_run_or_consume_brave():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run, _ = create_non_ft_run(session, Settings(_env_file=None), brave_max_requests=0)
        _resolve_rne_seeds(session, run, Settings(_env_file=None))
        assert run.funnel_stats["inpi_unconfigured"] == 1
        assert run.brave_requests_used == 0
        _count(run, "domain_rejected_fetch")
        session.commit()
        assert session.get(CollectionRun, run.id).funnel_stats["domain_rejected_fetch"] == 1
    engine.dispose()


def test_cached_official_link_is_checked_with_zero_brave(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed = CompanyDiscoverySeed(siren="123456789", siret="12345678900011",
                                    company_name="Atelier Exemple", commune="Créteil", status="review_required")
        session.add(seed)
        session.add(WebsiteCandidateRecord(
            company_key="atelier exemple", target_scope="company", target_fingerprint="a" * 64,
            query_fingerprint="b" * 64, search_provider="offer_description", query_index=0,
            result_rank=1, url="https://atelier.example/", canonical_url="https://atelier.example/",
            registrable_domain="atelier.example", observed_at=NOW, classification="potential_official",
            rejection_reasons=[], candidate_fingerprint="c" * 64, is_active=True,
        ))
        session.commit()
        run, _ = create_non_ft_run(session, Settings(_env_file=None), brave_max_requests=0)

        def verify(_session, target, _results, **kwargs):
            target.official_site_url = "https://atelier.example/"
            target.site_status = "high_confidence"
            target.domain_evidence = {"discovered_by": "official_web_cache"}
            return True

        monkeypatch.setattr("app.services.non_ft_runs._verify_seed_site", verify)
        monkeypatch.setattr("app.services.non_ft_runs.inspect_seed", lambda *args: (0, 0))
        _resolve_cached_seeds(session, run)
        assert run.funnel_stats["cache_seeds_checked"] == 1
        assert run.funnel_stats["domains_from_cache"] == 1
        assert run.brave_requests_used == 0
    engine.dispose()


def test_inpi_error_is_counted_and_run_continues(monkeypatch):
    class BrokenClient:
        def __init__(self, *args):
            pass

        def domains_for_siren(self, siren):
            raise InpiUnavailable("synthetic failure")

    class EmptySecret:
        def get_secret_value(self):
            return ""

    monkeypatch.setattr("app.services.non_ft_runs.InpiRneClient", BrokenClient)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(CompanyDiscoverySeed(siren="123456789", siret="12345678900011",
                                         company_name="Atelier Exemple", commune="Créteil", status="review_required"))
        session.commit()
        run, _ = create_non_ft_run(session, Settings(_env_file=None), brave_max_requests=0)
        _resolve_rne_seeds(session, run, SimpleNamespace(inpi_username=EmptySecret(), inpi_password=EmptySecret()))
        assert run.funnel_stats["inpi_errors"] == 1
        assert session.get(CompanyDiscoverySeed, 1).official_site_url is None
    engine.dispose()


def test_multiple_boards_from_one_provider_remain_ambiguous():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        rows, offers = attach_ats_candidates(session, "Atelier Exemple",
                                             (("lever", "atelier"), ("lever", "atelier-france")))
        assert offers == 0
        assert [row["status"] for row in rows] == ["review_required", "review_required"]
    engine.dispose()


def test_ats_web_hit_needs_provider_page_identity(monkeypatch):
    class Fetcher:
        def set_robots_checker(self, checker):
            pass

        def fetch(self, url, *, initial=False):
            assert not initial
            return FetchedPage(url, url, 200, "text/html", self.text, (), NOW)

    fetcher = Fetcher()
    monkeypatch.setattr("app.services.non_ft_runs.SecureWebFetcher", lambda: fetcher)
    fetcher.text = "Jobs at Unrelated Company"
    assert not _verify_ats_search_board("Atelier Exemple", "jobs.lever.co", "atelier")
    fetcher.text = "Jobs at Atelier Exemple"
    assert _verify_ats_search_board("Atelier Exemple", "jobs.lever.co", "atelier")


def test_career_label_and_iframe_find_ats_without_json_ld():
    home = "https://atelier.example/"
    careers = "https://atelier.example/page-42"

    class Fetcher:
        def set_robots_checker(self, checker):
            pass

        def fetch(self, url, *, initial=False):
            if url == home:
                return FetchedPage(url, url, 200, "text/html", "", (careers,), NOW,
                                   '<a href="/page-42">Nos recrutements</a>')
            return FetchedPage(url, url, 200, "text/html", "", (), NOW,
                               '<iframe src="https://jobs.ashbyhq.com/atelier"></iframe>')

    provider = OfficialSiteJobProvider("Atelier Exemple", home, fetcher=Fetcher())
    assert tuple(provider.iter_pages())[0].offers == ()
    assert provider.ats_candidates == (("ashby", "atelier"),)
    assert provider.career_pages == (careers,)


def test_jobposting_rejection_reason_is_observable():
    html = ('<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"Technicien","description":"Atelier",'
            '"datePosted":"2026-09-30","hiringOrganization":{"name":"Atelier Exemple"},'
            '"jobLocation":{"address":{"addressLocality":"Paris","postalCode":"75001"}}}'
            '</script>')
    diagnostics = {}
    assert parse_jobpostings(html, "https://atelier.example/jobs/1", "Atelier Exemple", "atelier.example",
                             now=NOW, diagnostics=diagnostics) == ()
    assert diagnostics == {"jobposting_found": 1, "rejected_geography_94": 1}
