from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ContactProviderState, VerifiedWebsiteRecord, WebsiteCandidateRecord
from app.services.contactability.batch import ContactBatchPolicy, ContactEnrichmentBatchOrchestrator
from app.services.contactability.contracts import ContactProviderStatus, ContactScope, ContactTarget
from app.services.contactability.providers.official_web.brave_client import BraveSearchClient
from app.services.contactability.providers.official_web.contracts import (
    BraveSearchResult,
    FetchedPage,
    WebsiteCandidateClassification,
    WebsiteSeed,
    WebsiteVerificationStatus,
)
from app.services.contactability.providers.official_web.discovery import (
    candidate_set_fingerprint,
    classify_domain,
    discover_website_candidates,
)
from app.services.contactability.providers.official_web.fetcher import SecureFetchError, SecureWebFetcher
from app.services.contactability.providers.official_web.persistence import OfficialWebRepository
from app.services.contactability.providers.official_web.provider import (
    OfficialWebProvider,
    official_web_target_fingerprint,
)
from app.services.contactability.providers.official_web.verification import verify_website_candidates


NOW = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
PUBLIC_IP = lambda host: ("93.184.216.34",)


def target(**overrides):
    values = dict(
        company_key="acme", organization_name_snapshot="ACME INDUSTRIE SAS",
        scope=ContactScope.COMPANY, siren="123456789", local_key=None,
        local_commune_snapshot=None, local_location_label_snapshot=None,
        employer_relationship_status="direct_employer",
        identity_match_status="matched_high_confidence", warnings=(),
        display_name_snapshot="Acme Industrie", identity_location_snapshot="Créteil",
    )
    values.update(overrides)
    return ContactTarget(**values)


class FakeBrave:
    def __init__(self, batches):
        self.batches = list(batches)
        self.queries = []

    def search(self, query, count=5):
        self.queries.append((query, count))
        return tuple(self.batches.pop(0))


class Response:
    def __init__(self, status=200, *, content=b"", content_type="text/html", headers=None, payload=None):
        self.status_code = status
        self.content = content
        self.encoding = "utf-8"
        self.headers = {"content-type": content_type, **(headers or {})}
        self._payload = payload

    def json(self):
        return self._payload


def page(url, text, links=()):
    return FetchedPage(url, url, 200, "text/html", text, tuple(links), NOW)


class PageFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.request_count = 0

    def fetch(self, url, initial=False):
        self.calls.append((url, initial))
        self.request_count += 1
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'official-web.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def test_brave_not_configured_does_not_block_preexisting_candidate(session):
    item = target()
    seed = WebsiteSeed("https://acme.fr", "offer_description", NOW)
    provider = OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=PageFetcher({}), brave_client=None,
        seed_loader=lambda _: (seed,), now=lambda: NOW,
    )
    result = provider.discover_resource(item, "discovery")
    assert result.status == ContactProviderStatus.COMPLETED
    assert "brave_search_not_configured" not in result.warnings  # serious seed avoids fallback


def test_brave_not_configured_without_candidates_is_explicit(session):
    provider = OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=PageFetcher({}), now=lambda: NOW,
    )
    result = provider.discover_resource(target(), "discovery")
    assert result.status == ContactProviderStatus.NOT_CONFIGURED
    assert result.metadata.request_count == 0


def test_first_satisfactory_search_avoids_fallback_and_limits_results():
    brave = FakeBrave([[BraveSearchResult("https://acme.fr", "Acme Industrie")]])
    candidates, calls, _ = discover_website_candidates(
        target(), target_fingerprint="x" * 64, seeds=(), brave_client=brave, observed_at=NOW,
    )
    assert calls == len(brave.queries) == 1
    assert candidates[0].registrable_domain == "acme.fr"
    assert brave.queries[0][1] == 5


def test_fallback_only_when_first_search_has_no_serious_candidate():
    brave = FakeBrave([
        [BraveSearchResult("https://unrelated.example", "Autre société")],
        [BraveSearchResult("https://acme.fr/contact", "Contact Acme Industrie")],
    ])
    candidates, calls, _ = discover_website_candidates(
        target(), target_fingerprint="x" * 64, seeds=(), brave_client=brave, observed_at=NOW,
    )
    assert calls == len(brave.queries) == 2
    assert {item.registrable_domain for item in candidates} == {"unrelated.example", "acme.fr"}


def test_deduplicates_same_domain_and_rejects_jobboards_and_socials():
    brave = FakeBrave([[
        BraveSearchResult("https://acme.fr/"), BraveSearchResult("https://www.acme.fr/contact"),
        BraveSearchResult("https://fr.indeed.com/acme"), BraveSearchResult("https://linkedin.com/company/acme"),
    ]])
    candidates, _, _ = discover_website_candidates(
        target(), target_fingerprint="x" * 64, seeds=(), brave_client=brave, observed_at=NOW,
    )
    assert [item.registrable_domain for item in candidates].count("acme.fr") == 1
    assert classify_domain("indeed.com")[0] == WebsiteCandidateClassification.EXCLUDED
    assert classify_domain("linkedin.com")[0] == WebsiteCandidateClassification.EXCLUDED


def test_brave_client_keeps_only_five_and_never_exposes_key():
    seen = {}
    def requester(url, **kwargs):
        seen.update(kwargs)
        return Response(payload={"web": {"results": [
            {"url": f"https://site{i}.example", "title": f"Site {i}"} for i in range(8)
        ]}})
    client = BraveSearchClient(api_key="top-secret", requester=requester, sleeper=lambda _: None)
    results = client.search("acme", count=99)
    assert len(results) == 5 and seen["params"]["count"] == 5
    assert "top-secret" not in repr(results)


@pytest.mark.parametrize("url,resolver", [
    ("http://localhost/x", PUBLIC_IP),
    ("http://internal.test/x", lambda host: ("10.0.0.8",)),
    ("http://link.test/x", lambda host: ("169.254.1.1",)),
])
def test_fetcher_blocks_non_public_destinations(url, resolver):
    fetcher = SecureWebFetcher(requester=lambda *a, **k: None, resolver=resolver)
    with pytest.raises(SecureFetchError, match="forbidden"):
        fetcher.fetch(url, initial=True)


def test_fetcher_checks_redirect_destination_and_redirect_limit():
    responses = [Response(302, headers={"location": "http://127.0.0.1/admin"})]
    fetcher = SecureWebFetcher(
        requester=lambda *a, **k: responses.pop(0),
        resolver=lambda host: ("127.0.0.1",) if host == "127.0.0.1" else PUBLIC_IP(host),
    )
    with pytest.raises(SecureFetchError) as exc:
        fetcher.fetch("https://public.example", initial=True)
    assert exc.value.kind == "ssrf_blocked"


def test_fetcher_enforces_maximum_redirect_count():
    def requester(method, url, **kwargs):
        number = int(url.rsplit("/", 1)[-1])
        return Response(302, headers={"location": f"https://public.example/{number + 1}"})
    fetcher = SecureWebFetcher(requester=requester, resolver=PUBLIC_IP, max_redirects=3)
    with pytest.raises(SecureFetchError) as exc:
        fetcher.fetch("https://public.example/0", initial=True)
    assert exc.value.kind == "too_many_redirects"


@pytest.mark.parametrize("response,kind", [
    (Response(content=b"%PDF", content_type="application/pdf"), "unsupported_content"),
    (Response(content=b"x" * 101), "response_too_large"),
])
def test_fetcher_rejects_binary_and_oversized_content(response, kind):
    fetcher = SecureWebFetcher(
        requester=lambda *a, **k: response, resolver=PUBLIC_IP, max_response_bytes=100,
    )
    with pytest.raises(SecureFetchError) as exc:
        fetcher.fetch("https://acme.fr", initial=True)
    assert exc.value.kind == kind


def test_fetcher_requires_robots_for_additional_pages_and_reuses_memory_cache():
    calls = []
    fetcher = SecureWebFetcher(
        requester=lambda *a, **k: calls.append(a) or Response(content=b"<p>Acme</p>"),
        resolver=PUBLIC_IP, robots_checker=lambda url, ua: True, sleeper=lambda _: None,
    )
    first = fetcher.fetch("https://acme.fr", initial=True)
    assert fetcher.fetch("https://acme.fr", initial=True) is first and len(calls) == 1
    fetcher.fetch("https://acme.fr/legal", initial=False)
    denied = SecureWebFetcher(
        requester=lambda *a, **k: Response(content=b"ok"), resolver=PUBLIC_IP,
        robots_checker=lambda url, ua: False,
    )
    with pytest.raises(SecureFetchError) as exc:
        denied.fetch("https://acme.fr/contact", initial=False)
    assert exc.value.kind == "robots_disallowed"


def _candidate(item=None, url="https://acme.fr"):
    item = item or target()
    candidates, _, _ = discover_website_candidates(
        item, target_fingerprint=official_web_target_fingerprint(item),
        seeds=(WebsiteSeed(url, "offer_description", NOW),), brave_client=None, observed_at=NOW,
    )
    return candidates[0]


def test_exact_siren_is_strong_and_conflict_rejects():
    item = target()
    candidate = _candidate(item)
    good_fetcher = PageFetcher({"https://acme.fr/": page(
        "https://acme.fr/", "ACME INDUSTRIE SAS - SIREN 123 456 789 - Créteil"
    )})
    good = verify_website_candidates(item, (candidate,), fetcher=good_fetcher, verified_at=NOW)[0]
    assert good.status == WebsiteVerificationStatus.HIGH_CONFIDENCE
    assert any(signal.signal_type == "siren_exact" and signal.weight == 70 for signal in good.signals)
    bad_fetcher = PageFetcher({"https://acme.fr/": page(
        "https://acme.fr/", "AUTRE SOCIETE - SIREN 987 654 321"
    )})
    bad = verify_website_candidates(item, (candidate,), fetcher=bad_fetcher, verified_at=NOW)[0]
    assert bad.status == WebsiteVerificationStatus.REJECTED


def test_name_geography_and_legal_page_can_verify_without_siren():
    item = target(siren=None, organization_name_snapshot="ACME INDUSTRIE SAS", display_name_snapshot="ACME INDUSTRIE")
    candidate = _candidate(item)
    legal = "https://acme.fr/mentions-legales"
    fetcher = PageFetcher({
        "https://acme.fr/": page("https://acme.fr/", "ACME INDUSTRIE SAS à Créteil", (legal,)),
        legal: page(legal, "Mentions légales ACME INDUSTRIE SAS Créteil"),
    })
    result = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)[0]
    assert result.status == WebsiteVerificationStatus.HIGH_CONFIDENCE
    assert result.score >= 80
    assert len(fetcher.calls) == 2


def test_homonym_without_signals_is_ambiguous_and_dinum_not_found_is_supported():
    item = target(siren=None, identity_match_status="not_found")
    candidate = _candidate(item, "https://homonyme.fr")
    fetcher = PageFetcher({"https://homonyme.fr/": page("https://homonyme.fr/", "Bienvenue")})
    result = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)[0]
    assert result.status == WebsiteVerificationStatus.AMBIGUOUS


def test_local_target_has_no_global_siren_and_keeps_non_juridical_warning():
    item = target(
        scope=ContactScope.LOCAL, siren=None, local_key="commune:creteil",
        local_commune_snapshot="Créteil", local_location_label_snapshot="94 - Créteil",
    )
    candidate = _candidate(item)
    fetcher = PageFetcher({"https://acme.fr/": page("https://acme.fr/", "Acme Industrie Créteil")})
    result = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)[0]
    assert item.siren is None
    assert any("juridique" in warning for warning in result.attribution_warnings)


def test_fingerprints_stable_and_candidate_change_invalidates_verification(session):
    item = target()
    repo = OfficialWebRepository(session)
    first = _candidate(item)
    repo.replace_candidates(official_web_target_fingerprint(item), (first,))
    provider = OfficialWebProvider(repository=repo, fetcher=PageFetcher({}), now=lambda: NOW)
    fp1 = provider.resource_input_fingerprint(item, "verification")
    fp1_again = provider.resource_input_fingerprint(item, "verification")
    second = _candidate(item, "https://acme-industrie.fr")
    repo.replace_candidates(official_web_target_fingerprint(item), (first, second))
    fp2 = provider.resource_input_fingerprint(item, "verification")
    assert fp1 == fp1_again and fp1 != fp2
    assert candidate_set_fingerprint((first,)) != candidate_set_fingerprint((first, second))


def test_batch_persists_artifacts_and_caches_resources_independently(session):
    item = target()
    seed = WebsiteSeed("https://acme.fr", "offer_description", NOW)
    legal = "https://acme.fr/mentions-legales"
    fetcher = PageFetcher({
        "https://acme.fr/": page("https://acme.fr/", "ACME INDUSTRIE SAS SIREN 123456789", (legal,)),
        legal: page(legal, "ACME INDUSTRIE SAS SIREN 123456789"),
    })
    provider = OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=fetcher,
        seed_loader=lambda _: (seed,), now=lambda: NOW,
    )
    batch = ContactEnrichmentBatchOrchestrator(
        provider, policy=ContactBatchPolicy(commit_each_result=False), clock=lambda: NOW,
        sleeper=lambda _: None,
    )
    run = batch.run(session, [item])
    assert run.status == "completed"
    assert session.scalar(select(WebsiteCandidateRecord)) is not None
    assert session.scalar(select(VerifiedWebsiteRecord)).status == WebsiteVerificationStatus.HIGH_CONFIDENCE
    states = session.scalars(select(ContactProviderState).order_by(ContactProviderState.resource)).all()
    assert [state.resource for state in states] == ["discovery", "verification"]
    assert all(state.target_fingerprint for state in states)


def test_no_raw_html_or_brave_payload_is_persisted(session):
    item = target()
    repo = OfficialWebRepository(session)
    candidate = _candidate(item)
    repo.replace_candidates(official_web_target_fingerprint(item), (candidate,))
    session.flush()
    record = session.scalar(select(WebsiteCandidateRecord))
    assert not hasattr(record, "html") and not hasattr(record, "raw_payload")
    assert "<html" not in (record.snippet or "").casefold()


def test_official_web_has_no_societe_dependency_and_target_fingerprint_is_stable(session):
    provider = OfficialWebProvider(repository=OfficialWebRepository(session), fetcher=PageFetcher({}))
    assert provider.target_fingerprint(target()) == provider.target_fingerprint(target())
    assert "societe" not in type(provider).__module__
