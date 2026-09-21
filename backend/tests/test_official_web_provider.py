from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ContactEvidence, ContactPoint, ContactProviderState, VerifiedWebsiteRecord, WebsiteCandidateRecord
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
    discovery_queries,
)
from app.services.contactability.providers.official_web.fetcher import SecureFetchError, SecureWebFetcher
from app.services.contactability.providers.official_web.extraction import extract_official_contacts
from app.services.contactability.providers.official_web.persistence import OfficialWebRepository, ensure_official_web_schema
from app.services.contactability.providers.official_web.provider import (
    OfficialWebProvider,
    official_web_target_fingerprint,
)
from app.services.contactability.providers.official_web import provider as provider_module
from app.services.contactability.providers.official_web.verification import verify_website_candidates
from app.services.contactability.providers.official_web.robots import RobotsTxtPolicy
from app.services.brave_usage import BraveBudgetPolicy, BraveUsageService


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

    def search(self, query, count=5, **kwargs):
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


def test_discovery_query_uses_structured_commune_not_full_address():
    item = target(
        identity_location_snapshot="8 Villa des Fleurs 94220 Charenton-le-Pont",
        local_commune_snapshot="Charenton-le-Pont",
    )
    first, _ = discovery_queries(item)
    assert '"Charenton-le-Pont"' in first
    assert "Villa des Fleurs" not in first and "94220" not in first


def test_discovery_query_extracts_city_from_address_when_no_commune_exists():
    first, _ = discovery_queries(target(
        identity_location_snapshot="8 Villa des Fleurs 94220 Charenton-le-Pont",
    ))
    assert '"Charenton-le-Pont"' in first
    assert "Villa des Fleurs" not in first and "94220" not in first


def test_multilocal_company_discovery_uses_france():
    first, _ = discovery_queries(target(is_multi_local=True, local_commune_snapshot="Créteil"))
    assert '"France"' in first and "Créteil" not in first


@pytest.mark.parametrize("location", ["94 - Charenton-le-Pont", "94"])
def test_vague_department_location_falls_back_to_france(location):
    first, _ = discovery_queries(target(identity_location_snapshot=location))
    assert '"France"' in first and "94" not in first


def test_missing_location_falls_back_to_france():
    first, _ = discovery_queries(target(identity_location_snapshot=None))
    assert '"France"' in first


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


def test_brave_client_keeps_only_five_and_never_exposes_key(session):
    seen = {}
    def requester(url, **kwargs):
        seen.update(kwargs)
        return Response(payload={"web": {"results": [
            {"url": f"https://site{i}.example", "title": f"Site {i}"} for i in range(8)
        ]}})
    client = BraveSearchClient(
        api_key="top-secret", requester=requester, sleeper=lambda _: None,
        usage_service=BraveUsageService(session, BraveBudgetPolicy(), now=lambda: NOW),
    )
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


def test_real_robots_policy_is_cached_and_fails_closed_when_disallowed():
    calls = []
    def requester(method, url, **kwargs):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return Response(content=b"User-agent: *\nDisallow: /private\nAllow: /contact\n", content_type="text/plain")
        return Response(content=b"<p>ok</p>")
    fetcher = SecureWebFetcher(requester=requester, resolver=PUBLIC_IP, sleeper=lambda _: None)
    policy = RobotsTxtPolicy(fetcher)
    fetcher.set_robots_checker(policy.allowed)
    fetcher.fetch("https://acme.fr/contact", initial=False)
    with pytest.raises(SecureFetchError) as exc:
        fetcher.fetch("https://acme.fr/private", initial=False)
    assert exc.value.kind == "robots_disallowed"
    assert calls.count("https://acme.fr/robots.txt") == 1


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


def test_directory_profile_with_exact_siren_is_not_an_official_site():
    item = target()
    candidate = replace(
        _candidate(item, "https://rubypayeur.com/societe/acme-123456789"),
        classification=WebsiteCandidateClassification.POTENTIAL_OFFICIAL,
        rejection_reasons=(),
    )
    legal = "https://rubypayeur.com/mentions-legales"
    fetcher = PageFetcher({
        candidate.canonical_url: page(candidate.canonical_url,
            "Fiche entreprise ACME INDUSTRIE SAS SIREN 123456789 données légales", (legal,)),
        legal: page(legal, "Mentions légales. Éditeur : RubyPayeur."),
    })
    result = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)[0]
    assert result.status == WebsiteVerificationStatus.REJECTED
    assert result.score == 0
    assert "third_party_directory" in result.rejection_reasons
    assert any(signal.signal_type == "siren_exact" for signal in result.signals)


def test_legal_operator_third_party_rejects_even_when_profile_has_target_identity():
    item = target()
    candidate = _candidate(item, "https://data.example/entreprise/acme")
    legal = "https://data.example/mentions-legales"
    fetcher = PageFetcher({
        candidate.canonical_url: page(candidate.canonical_url, "ACME INDUSTRIE SAS SIREN 123456789", (legal,)),
        legal: page(legal, "Éditeur : Data Holdings, annuaire des entreprises."),
    })
    result = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)[0]
    assert result.status == WebsiteVerificationStatus.REJECTED
    assert "third_party_directory" in result.rejection_reasons


def test_brand_domain_and_coherent_legal_operator_can_be_high_confidence():
    item = target(siren=None, organization_name_snapshot="BABILOU FRANCE", display_name_snapshot="Babilou")
    candidate = _candidate(item, "https://babilou.fr")
    legal = "https://babilou.fr/mentions-legales"
    fetcher = PageFetcher({
        candidate.canonical_url: page(candidate.canonical_url, "Babilou, solutions petite enfance", (legal,)),
        legal: page(legal, "Mentions légales. Éditeur : Babilou France."),
    })
    result = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)[0]
    assert result.status == WebsiteVerificationStatus.HIGH_CONFIDENCE
    assert any(signal.signal_type == "domain_brand_match" for signal in result.signals)
    assert any(signal.signal_type == "legal_operator_target" for signal in result.signals)


def test_discovery_uses_brand_and_context_not_siren_as_primary_query():
    item = target(siren="123456789", is_multi_local=True)
    first, fallback = discovery_queries(item)
    assert "123456789" not in first and "site officiel" in first and "France" in first
    assert fallback.endswith("contact")
    assert classify_domain("rubypayeur.com") == (
        WebsiteCandidateClassification.EXCLUDED, ("financial_directory",)
    )


def test_initial_dns_failure_is_a_retryable_resource_error_not_a_rejected_website(session):
    item = target()
    seed = WebsiteSeed("https://acme.fr", "offer_description", NOW)
    failing = PageFetcher({"https://acme.fr/": SecureFetchError("dns_error", "DNS unavailable")})
    provider = OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=failing,
        seed_loader=lambda _: (seed,), now=lambda: NOW,
    )
    batch = ContactEnrichmentBatchOrchestrator(
        provider, policy=ContactBatchPolicy(max_retries_per_resource=0, commit_each_result=False),
        clock=lambda: NOW, sleeper=lambda _: None,
    )
    failed = batch.run(session, [item])
    state = session.scalar(select(ContactProviderState).where(
        ContactProviderState.resource == "verification",
    ))
    assert failed.status == "completed_with_errors"
    assert state.last_status == ContactProviderStatus.ERROR and state.fresh_until is None
    assert session.scalars(select(VerifiedWebsiteRecord)).all() == []

    recovered_fetcher = PageFetcher({"https://acme.fr/": page(
        "https://acme.fr/", "ACME INDUSTRIE SAS SIREN 123456789",
    )})
    recovered_provider = OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=recovered_fetcher,
        seed_loader=lambda _: (seed,), now=lambda: NOW,
    )
    recovered = ContactEnrichmentBatchOrchestrator(
        recovered_provider, policy=ContactBatchPolicy(commit_each_result=False),
        clock=lambda: NOW, sleeper=lambda _: None,
    ).run(session, [item])
    assert recovered.status == "completed" and recovered_fetcher.calls
    assert session.scalar(select(VerifiedWebsiteRecord)).status == WebsiteVerificationStatus.HIGH_CONFIDENCE


def test_oversized_response_is_a_non_cacheable_technical_error_not_a_rejected_website(session):
    item = target()
    seed = WebsiteSeed("https://acme.fr", "offer_description", NOW)
    failing = PageFetcher({"https://acme.fr/": SecureFetchError("response_too_large", "response too large")})
    provider = OfficialWebProvider(
        repository=OfficialWebRepository(session), fetcher=failing,
        seed_loader=lambda _: (seed,), now=lambda: NOW,
    )
    run = ContactEnrichmentBatchOrchestrator(
        provider, policy=ContactBatchPolicy(max_retries_per_resource=0, commit_each_result=False),
        clock=lambda: NOW, sleeper=lambda _: None,
    ).run(session, [item])
    state = session.scalar(select(ContactProviderState).where(
        ContactProviderState.resource == "verification",
    ))
    assert run.status == "completed_with_errors"
    assert state.last_status == ContactProviderStatus.ERROR and state.fresh_until is None
    assert session.scalars(select(VerifiedWebsiteRecord)).all() == []


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


def test_discovery_policy_version_invalidates_only_discovery_cache_and_keeps_candidates(session, monkeypatch):
    item = target()
    repo = OfficialWebRepository(session)
    candidate = _candidate(item)
    repo.replace_candidates(official_web_target_fingerprint(item), (candidate,))
    provider = OfficialWebProvider(repository=repo, fetcher=PageFetcher({}), now=lambda: NOW)
    old_fingerprint = provider.resource_input_fingerprint(item, "discovery")
    monkeypatch.setattr(provider_module, "DISCOVERY_POLICY_VERSION", 3)
    new_fingerprint = provider.resource_input_fingerprint(item, "discovery")
    assert old_fingerprint != new_fingerprint
    persisted = repo.candidates(official_web_target_fingerprint(item))
    assert [value.candidate_fingerprint for value in persisted] == [candidate.candidate_fingerprint]


def test_verification_policy_version_invalidates_an_ambiguous_cache(session, monkeypatch):
    item = target()
    repo = OfficialWebRepository(session)
    candidate = _candidate(item)
    repo.replace_candidates(official_web_target_fingerprint(item), (candidate,))
    provider = OfficialWebProvider(repository=repo, fetcher=PageFetcher({}), now=lambda: NOW)
    old_fingerprint = provider.resource_input_fingerprint(item, "verification")
    session.add(ContactProviderState(
        provider="official_web", target_fingerprint=old_fingerprint, resource="verification",
        company_key=item.company_key, target_scope=item.scope, siren=item.siren, local_key=item.local_key,
        last_status=ContactProviderStatus.COMPLETED, last_attempt_at=NOW, last_success_at=NOW,
        fresh_until=NOW.replace(year=2027), attempt_count=1, result_count=1,
    ))
    session.commit()
    monkeypatch.setattr(provider_module, "VERIFICATION_POLICY_VERSION", 3)
    new_fingerprint = provider.resource_input_fingerprint(item, "verification")
    assert new_fingerprint != old_fingerprint
    assert session.scalar(select(ContactProviderState).where(
        ContactProviderState.target_fingerprint == new_fingerprint,
        ContactProviderState.resource == "verification",
    )) is None


def test_extraction_fingerprint_depends_on_verified_domains_and_policy(session, monkeypatch):
    item = target()
    repo = OfficialWebRepository(session)
    candidate = _candidate(item)
    repo.replace_candidates(official_web_target_fingerprint(item), (candidate,))
    verified = verify_website_candidates(item, (candidate,), fetcher=PageFetcher({
        candidate.canonical_url: page(candidate.canonical_url, "ACME INDUSTRIE SAS SIREN 123456789"),
    }), verified_at=NOW)
    repo.persist_verified(verified, high_ttl=timedelta(days=90), review_ttl=timedelta(days=30))
    provider = OfficialWebProvider(repository=repo, fetcher=PageFetcher({}), now=lambda: NOW)
    old_fingerprint = provider.resource_input_fingerprint(item, "extraction")
    monkeypatch.setattr(provider_module, "VERIFICATION_POLICY_VERSION", 3)
    assert provider.resource_input_fingerprint(item, "extraction") != old_fingerprint


def test_schema_normalizes_historic_rejected_score_to_zero(session):
    record = VerifiedWebsiteRecord(
        company_key="acme", target_scope=ContactScope.COMPANY, local_key=None,
        target_fingerprint="target", candidate_fingerprint="candidate", candidate_set_fingerprint="set",
        provider="official_web", canonical_url="https://third.example", registrable_domain="third.example",
        status=WebsiteVerificationStatus.REJECTED, score=100, rejection_reasons=["third_party_directory"],
        attribution_warnings=[], observed_at=NOW, verified_at=NOW, fresh_until=NOW, fingerprint="historic-rejected",
    )
    session.add(record)
    session.commit()
    ensure_official_web_schema(session.bind)
    assert session.get(VerifiedWebsiteRecord, record.id).score == 0


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
    assert session.scalar(select(ContactPoint)).contact_type == "website"
    assert session.scalar(select(ContactEvidence)).provider == "official_web"
    states = session.scalars(select(ContactProviderState).order_by(ContactProviderState.resource)).all()
    assert [state.resource for state in states] == ["discovery", "extraction", "verification"]
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


def test_extraction_keeps_public_contacts_people_and_minimal_evidence(session):
    item = target()
    candidate = _candidate(item)
    contact_url = "https://acme.fr/contact"
    fetcher = PageFetcher({
        "https://acme.fr/": page("https://acme.fr/", "ACME INDUSTRIE SAS SIREN 123456789", (contact_url,)),
        contact_url: page(contact_url, "ACME INDUSTRIE SAS Contact contact@acme.fr 01 23 45 67 89 Alice Martin - Directrice des ressources humaines"),
    })
    verified = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)
    class AllowRobots:
        def allowed(self, url, user_agent): return True
    points, people, _, _ = extract_official_contacts(
        item, verified, fetcher=fetcher, observed_at=NOW, robots_policy=AllowRobots(),
    )
    assert {point.contact_type for point in points} >= {"website", "professional_url", "email", "phone"}
    assert people[0].full_name == "Alice Martin" and people[0].relevance_role == "hr"
    assert all(point.confidence_level == "high_confidence" for point in points)
    assert all(point.verification_status == "source_verified" for point in points)
    assert all(evidence.source_url and len(evidence.excerpt or "") <= 500 for point in points for evidence in point.evidence)


def test_person_extraction_skips_legal_pages_and_organization_like_names():
    item = target()
    candidate = _candidate(item)
    contact_url, legal_url = "https://acme.fr/contact", "https://acme.fr/mentions-legales"
    fetcher = PageFetcher({
        "https://acme.fr/": page("https://acme.fr/", "ACME INDUSTRIE SAS SIREN 123456789", (contact_url, legal_url)),
        contact_url: page(contact_url, "Alice Martin - Directrice des ressources humaines"),
        legal_url: page(legal_url, "Mentions légales spécifiques - Directeur SAU ACME INDUSTRIE Ce - Responsable"),
    })
    verified = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)
    class AllowRobots:
        def allowed(self, url, user_agent): return True
    _, people, _, _ = extract_official_contacts(
        item, verified, fetcher=fetcher, observed_at=NOW, robots_policy=AllowRobots(),
    )
    assert [(person.full_name, person.relevance_role) for person in people] == [("Alice Martin", "hr")]


def test_local_extraction_never_propagates_without_explicit_location():
    item = target(scope=ContactScope.LOCAL, local_key="creteil", local_commune_snapshot="Créteil")
    candidate = _candidate(item)
    fetcher = PageFetcher({"https://acme.fr/": page("https://acme.fr/", "ACME INDUSTRIE SAS SIREN 123456789 contact@acme.fr")})
    verified = verify_website_candidates(item, (candidate,), fetcher=fetcher, verified_at=NOW)
    points, people, _, _ = extract_official_contacts(item, verified, fetcher=fetcher, observed_at=NOW)
    assert points == () and people == ()
