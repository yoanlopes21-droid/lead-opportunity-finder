from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.cli.contact_enrichment import execute_cli
from app.database import Base
from app.models import (
    ContactEnrichmentRun,
    ContactEnrichmentRunItem,
    ContactEvidence,
    ContactPoint,
    ContactProviderState,
    PersonContact,
)
from app.services.company_enrichment.contracts import MatchStatus
from app.services.contactability.batch import (
    ContactBatchPolicy,
    ContactEnrichmentBatchOrchestrator,
    ContactEnrichmentRunItemStatus,
    ContactEnrichmentRunStatus,
)
from app.services.contactability.contracts import (
    ContactConfidence,
    ContactEvidenceCandidate,
    ContactPointCandidate,
    ContactProviderAttemptMetadata,
    ContactProviderResult,
    ContactProviderStatus,
    ContactScope,
    ContactTarget,
    ContactType,
    PersonContactCandidate,
    PersonRelevanceRole,
    VerificationStatus,
)
from app.services.contactability.normalization import normalize_email, normalize_person_name
from app.services.contactability.providers.societe_com import societe_com_target_fingerprint


def at(day: int) -> datetime:
    return datetime(2026, 9, 1, 9, tzinfo=timezone.utc) + timedelta(days=day - 1)


def target(key="acme", siren="123456789", **overrides):
    values = {
        "company_key": key,
        "organization_name_snapshot": f"{key.upper()} SAS",
        "scope": ContactScope.COMPANY,
        "siren": siren,
        "local_key": None,
        "local_commune_snapshot": None,
        "local_location_label_snapshot": None,
        "employer_relationship_status": "direct_employer",
        "identity_match_status": MatchStatus.HIGH_CONFIDENCE,
        "warnings": (),
    }
    values.update(overrides)
    return ContactTarget(**values)


def metadata(contact_target, error_type=None):
    return ContactProviderAttemptMetadata(
        target_fingerprint=societe_com_target_fingerprint(contact_target),
        attempted_at=at(1), request_count=1, error_type=error_type,
    )


def point_result(contact_target):
    evidence = ContactEvidenceCandidate(
        provider="fake_societe", source_name="Mock source", source_url="https://provider.test/contact",
        source_identifier=contact_target.siren, observed_at=at(1), evidence_reason="mock_contact",
    )
    return ContactProviderResult(
        provider="fake_societe", status=ContactProviderStatus.COMPLETED,
        candidates=(ContactPointCandidate(
            company_key=contact_target.company_key,
            organization_name_snapshot=contact_target.organization_name_snapshot,
            scope=ContactScope.COMPANY, contact_type=ContactType.EMAIL,
            value="contact@example.test", normalized_value=normalize_email("contact@example.test"),
            confidence_level=ContactConfidence.HIGH_CONFIDENCE,
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            attribution_reason="mock", siren=contact_target.siren, evidence=(evidence,),
        ),), metadata=metadata(contact_target),
    )


def person_result(contact_target):
    evidence = ContactEvidenceCandidate(
        provider="fake_societe", source_name="Mock source", source_url="https://provider.test/directors",
        source_identifier=contact_target.siren, observed_at=at(1), evidence_reason="mock_director",
    )
    return ContactProviderResult(
        provider="fake_societe", status=ContactProviderStatus.COMPLETED,
        person_candidates=(PersonContactCandidate(
            company_key=contact_target.company_key,
            organization_name_snapshot=contact_target.organization_name_snapshot,
            scope=ContactScope.COMPANY, full_name="Alice Martin",
            normalized_name=normalize_person_name("Alice Martin"),
            relevance_role=PersonRelevanceRole.DIRECTOR,
            confidence_level=ContactConfidence.HIGH_CONFIDENCE,
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            attribution_reason="mock", siren=contact_target.siren, evidence=(evidence,),
        ),), metadata=metadata(contact_target),
    )


def not_found_result(contact_target):
    return ContactProviderResult(
        provider="fake_societe", status=ContactProviderStatus.NOT_FOUND,
        metadata=metadata(contact_target),
    )


def error_result(contact_target, error_type):
    return ContactProviderResult(
        provider="fake_societe", status=ContactProviderStatus.ERROR,
        metadata=metadata(contact_target, error_type=error_type),
    )


class FakeResourceProvider:
    name = "fake_societe"
    resources = ("contact", "directors")
    is_configured = True

    def __init__(self, actions=None):
        self.actions = {key: list(values) for key, values in (actions or {}).items()}
        self.calls = []

    def target_fingerprint(self, item):
        return societe_com_target_fingerprint(item)

    def inapplicability_reason(self, item):
        if item.scope != ContactScope.COMPANY or not item.siren:
            return "requires_confirmed_company_siren"
        if item.identity_match_status != MatchStatus.HIGH_CONFIDENCE:
            return "requires_high_confidence_identity"
        if item.employer_relationship_status == "intermediary":
            return "intermediary_not_supported"
        return None

    def resource_ttl(self, resource, status):
        if status == ContactProviderStatus.NOT_FOUND:
            return timedelta(days=30)
        return timedelta(days=30 if resource == "contact" else 90)

    def discover_resource(self, item, resource):
        self.calls.append((item.company_key, resource))
        action = self.actions[(item.company_key, resource)].pop(0)
        if isinstance(action, BaseException):
            raise action
        return action(item) if callable(action) else action


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact-batch.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def runner(provider, now=at(10), **policy):
    sleeps = []
    return (
        ContactEnrichmentBatchOrchestrator(
            provider, policy=ContactBatchPolicy(**policy), sleeper=sleeps.append, clock=lambda: now,
        ),
        sleeps,
    )


def completed_actions(item):
    return {(item.company_key, "contact"): [point_result], (item.company_key, "directors"): [person_result]}


def test_selection_is_persisted_before_any_provider_call(session):
    provider = FakeResourceProvider()
    batch, _ = runner(provider)
    run = batch.create_run(session, [target("b"), target("a"), target("a")])
    items = session.scalars(select(ContactEnrichmentRunItem).order_by(ContactEnrichmentRunItem.deterministic_position)).all()
    assert run.selected_count == 2 and provider.calls == []
    assert [item.company_key for item in items] == ["a", "b"]
    assert all(item.status == ContactEnrichmentRunItemStatus.PENDING for item in items)
    assert all("warnings" not in item.input_snapshot for item in items)


def test_empty_run_completes_without_calls(session):
    batch, _ = runner(FakeResourceProvider())
    run = batch.run(session, [])
    assert run.status == ContactEnrichmentRunStatus.COMPLETED
    assert run.selected_count == run.processed_count == 0


def test_not_applicable_target_never_calls_provider(session):
    provider = FakeResourceProvider()
    batch, _ = runner(provider)
    run = batch.run(session, [target(siren=None)])
    assert run.not_applicable_count == run.processed_count == 1
    assert provider.calls == []


def test_both_fresh_resources_are_cached_without_calls(session):
    item = target()
    first_provider = FakeResourceProvider(completed_actions(item))
    runner(first_provider, now=at(1))[0].run(session, [item])
    cached_provider = FakeResourceProvider()
    run = runner(cached_provider, now=at(10))[0].run(session, [item])
    assert run.cached_count == 1 and run.external_call_count == 0
    assert cached_provider.calls == []


def test_only_expired_directors_are_recalled(session):
    item = target()
    runner(FakeResourceProvider(completed_actions(item)), now=at(1))[0].run(session, [item])
    contact_state = session.scalar(select(ContactProviderState).where(ContactProviderState.resource == "contact"))
    contact_state.fresh_until = at(200)
    session.commit()
    provider = FakeResourceProvider({(item.company_key, "directors"): [person_result]})
    run = runner(provider, now=at(100))[0].run(session, [item])
    assert provider.calls == [("acme", "directors")]
    assert run.external_call_count == 1


def test_only_expired_contact_is_recalled(session):
    item = target()
    runner(FakeResourceProvider(completed_actions(item)), now=at(1))[0].run(session, [item])
    provider = FakeResourceProvider({(item.company_key, "contact"): [point_result]})
    run = runner(provider, now=at(40)) [0].run(session, [item])
    assert provider.calls == [("acme", "contact")]
    assert run.external_call_count == 1


def test_both_expired_resources_require_two_calls_and_changed_fingerprint_invalidates_cache(session):
    original = target()
    runner(FakeResourceProvider(completed_actions(original)), now=at(1))[0].run(session, [original])
    provider = FakeResourceProvider(completed_actions(original))
    run = runner(provider, now=at(100))[0].run(session, [original])
    assert len(provider.calls) == run.external_call_count == 2
    changed = target(siren="987654321")
    changed_provider = FakeResourceProvider(completed_actions(changed))
    runner(changed_provider, now=at(2))[0].run(session, [changed])
    assert len(changed_provider.calls) == 2


def test_not_found_is_cached_for_thirty_days(session):
    item = target()
    actions = {(item.company_key, resource): [not_found_result] for resource in ("contact", "directors")}
    first = runner(FakeResourceProvider(actions), now=at(1))[0].run(session, [item])
    assert first.not_found_count == 1
    cached = FakeResourceProvider()
    second = runner(cached, now=at(20))[0].run(session, [item])
    assert second.cached_count == 1 and cached.calls == []


def test_results_and_evidence_are_deduplicated_across_refreshes(session):
    item = target()
    runner(FakeResourceProvider(completed_actions(item)), now=at(1))[0].run(session, [item])
    runner(FakeResourceProvider(completed_actions(item)), now=at(100))[0].run(session, [item])
    assert len(session.scalars(select(ContactPoint)).all()) == 1
    assert len(session.scalars(select(PersonContact)).all()) == 1
    assert len(session.scalars(select(ContactEvidence)).all()) == 2


def test_interruption_preserves_completed_items_and_resume_keeps_order(session):
    first, second = target("first"), target("second")
    interrupted = FakeResourceProvider({
        **completed_actions(first),
        (second.company_key, "contact"): [KeyboardInterrupt()],
        (second.company_key, "directors"): [person_result],
    })
    batch, _ = runner(interrupted, now=at(1))
    with pytest.raises(KeyboardInterrupt):
        batch.run(session, [second, first])
    failed = session.scalar(select(ContactEnrichmentRun))
    assert failed.status == ContactEnrichmentRunStatus.FAILED
    resume = FakeResourceProvider({
        (second.company_key, "contact"): [point_result],
        (second.company_key, "directors"): [person_result],
    })
    completed = runner(resume, now=at(2))[0].resume(session, failed.id)
    items = session.scalars(select(ContactEnrichmentRunItem).order_by(ContactEnrichmentRunItem.deterministic_position)).all()
    assert completed.status == ContactEnrichmentRunStatus.COMPLETED
    assert resume.calls == [("second", "contact"), ("second", "directors")]
    assert [item.company_key for item in items] == ["first", "second"]
    assert completed.processed_count == completed.selected_count == 2


def test_orphan_processing_becomes_pending_and_inconsistent_run_is_refused(session):
    item = target()
    batch, _ = runner(FakeResourceProvider(completed_actions(item)))
    run = batch.create_run(session, [item])
    row = session.scalar(select(ContactEnrichmentRunItem))
    row.status = ContactEnrichmentRunItemStatus.PROCESSING
    session.commit()
    completed = batch.resume(session, run.id)
    assert completed.status == ContactEnrichmentRunStatus.COMPLETED
    broken = batch.create_run(session, [target("broken")])
    broken_item = session.scalar(select(ContactEnrichmentRunItem).where(ContactEnrichmentRunItem.run_id == broken.id))
    broken_item.input_snapshot = {**broken_item.input_snapshot, "company_key": "tampered"}
    session.commit()
    with pytest.raises(ValueError, match="inconsistent"):
        batch.resume(session, broken.id)


@pytest.mark.parametrize("error_type", ["timeout", "rate_limited", "server_error"])
def test_transient_resource_error_retries_once(error_type, session):
    item = target()
    provider = FakeResourceProvider({
        (item.company_key, "contact"): [
            lambda value: error_result(value, error_type), point_result,
        ],
        (item.company_key, "directors"): [person_result],
    })
    batch, sleeps = runner(provider, max_retries_per_resource=1, initial_backoff_seconds=0.25)
    run = batch.run(session, [item])
    assert provider.calls.count(("acme", "contact")) == 2
    assert sleeps == [0.25] and run.retry_count == 1 and run.external_call_count == 3


def test_permanent_error_is_not_retried_and_three_transient_item_errors_fail_run(session):
    item = target()
    permanent = FakeResourceProvider({
        (item.company_key, "contact"): [lambda value: error_result(value, "http_error")],
    })
    run = runner(permanent, max_retries_per_resource=1)[0].run(session, [item])
    assert permanent.calls == [("acme", "contact")] and run.error_count == 1
    targets = [target(str(index), f"12345678{index}") for index in range(3)]
    transient = FakeResourceProvider({
        (value.company_key, "contact"): [lambda current: error_result(current, "timeout")]
        for value in targets
    })
    failed = runner(transient, max_retries_per_resource=0, systemic_error_threshold=3)[0].run(session, targets)
    assert failed.status == ContactEnrichmentRunStatus.FAILED
    assert failed.processed_count == failed.error_count == 3


def test_contact_success_is_kept_when_directors_fail_and_next_run_only_calls_directors(session):
    item = target()
    first = FakeResourceProvider({
        (item.company_key, "contact"): [point_result],
        (item.company_key, "directors"): [lambda value: error_result(value, "timeout")],
    })
    failed = runner(first, max_retries_per_resource=0)[0].run(session, [item])
    assert failed.error_count == 1
    second = FakeResourceProvider({(item.company_key, "directors"): [person_result]})
    recovered = runner(second, now=at(2))[0].run(session, [item])
    assert recovered.external_call_count == 1
    assert second.calls == [("acme", "directors")]


def test_resume_retries_only_a_transient_terminal_error(session):
    item = target()
    first = FakeResourceProvider({
        (item.company_key, "contact"): [lambda value: error_result(value, "dns_error")],
    })
    failed = runner(first, max_retries_per_resource=0)[0].run(session, [item])
    assert failed.status == ContactEnrichmentRunStatus.COMPLETED_WITH_ERRORS
    resumed = FakeResourceProvider(completed_actions(item))
    completed = runner(resumed, now=at(2))[0].resume(session, failed.id)
    assert completed.status == ContactEnrichmentRunStatus.COMPLETED
    assert resumed.calls == [("acme", "contact"), ("acme", "directors")]
    assert completed.processed_count == completed.selected_count == 1


def test_repeated_resume_is_idempotent_for_terminal_items(session):
    first, second = target("first"), target("second")
    batch, _ = runner(FakeResourceProvider({
        **completed_actions(first), **completed_actions(second),
    }))
    completed = batch.run(session, [first, second])
    again = batch.resume(session, completed.id)
    once_more = batch.resume(session, completed.id)
    assert completed.processed_count == again.processed_count == once_more.processed_count == 2
    assert once_more.processed_count <= once_more.selected_count


def test_cli_new_and_resume_are_safe_with_mocked_provider(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact-cli.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    item = target()
    output = []
    code = execute_cli(
        ["new", "--provider", "societe_com", "--limit", "1"],
        session_factory=factory,
        provider=FakeResourceProvider(completed_actions(item)),
        prepare_schema=False,
        target_selector=lambda session, department, limit: (item,),
        output=output.append,
    )
    with factory() as session:
        run_id = session.scalar(select(ContactEnrichmentRun.id))
    resume_code = execute_cli(
        ["resume", "--run-id", str(run_id)], session_factory=factory,
        provider=FakeResourceProvider(), prepare_schema=False, output=output.append,
    )
    assert code == resume_code == 0
    assert all("contact@example.test" not in line for line in output)
    engine.dispose()


def test_cli_unconfigured_provider_creates_no_run(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact-cli-unconfigured.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    provider = FakeResourceProvider()
    provider.is_configured = False
    output = []
    code = execute_cli(
        ["new", "--provider", "societe_com"], session_factory=factory,
        provider=provider, prepare_schema=False, output=output.append,
    )
    with factory() as session:
        assert session.scalars(select(ContactEnrichmentRun)).all() == []
    assert code == 4 and "not configured" in output[0]
    engine.dispose()


def test_official_web_cli_requires_explicit_company_keys(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact-cli-official.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    output = []
    code = execute_cli(
        ["new", "--provider", "official_web"], session_factory=factory,
        provider=FakeResourceProvider(), prepare_schema=False, output=output.append,
    )
    assert code == 4
    assert output == ["Contact enrichment batch could not be started or resumed safely."]
    engine.dispose()


def test_official_web_cli_runs_only_the_explicit_mocked_selection(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact-cli-official-limited.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    item = target()
    monkeypatch.setattr(
        "app.cli.contact_enrichment.select_official_web_targets",
        lambda session, company_keys, limit: (item,) if company_keys == ["acme"] and limit == 1 else (),
    )
    provider = FakeResourceProvider(completed_actions(item))
    code = execute_cli(
        ["new", "--provider", "official_web", "--limit", "1", "--company-key", "acme"],
        session_factory=factory, provider=provider, prepare_schema=False, output=lambda _: None,
    )
    assert code == 0 and provider.calls == [("acme", "contact"), ("acme", "directors")]
    engine.dispose()
