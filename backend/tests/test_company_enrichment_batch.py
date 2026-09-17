from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CompanyEnrichment, EnrichmentRun, EnrichmentRunItem
from app.services.company_enrichment.batch import (
    BatchPolicy,
    CompanyEnrichmentBatchOrchestrator,
    RunItemStatus,
)
from app.cli.enrichment import execute_cli
from app.services.company_enrichment.contracts import (
    LegalIdentity,
    MatchStatus,
    ProviderCallError,
    ProviderEnrichmentResult,
)
from app.services.company_enrichment.persistence import EnrichmentRunStatus
from app.services.company_enrichment.dinum import DinumSearchError, normalize_candidate
from app.services.company_enrichment.dinum_adapter import DinumCompanyEnrichmentProvider
from app.services.opportunities.company import CompanyOpportunity


def at(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def opportunity(key="acme", name="ACME", communes=("94028",)) -> CompanyOpportunity:
    return CompanyOpportunity(
        company_key=key, company_name=name, department_code="94", active_offer_count=1,
        distinct_job_title_count=1, distinct_source_count=1, sources=("test",),
        oldest_offer_created_at=None, newest_offer_created_at=None,
        oldest_offer_age_days=None, newest_offer_age_days=None, contract_types=(),
        communes=communes, location_labels=("Créteil",), offer_ids=(f"test:{key}",),
        job_titles=("Test",), cdi_offer_count=0, cdd_offer_count=0,
        other_contract_offer_count=1, offers_over_21_days=0, offers_over_45_days=0,
        offers_over_90_days=0, average_offer_age_days=None, median_offer_age_days=None,
        signals=(),
    )


def high(sector="private"):
    return ProviderEnrichmentResult(
        status=MatchStatus.HIGH_CONFIDENCE,
        confidence_score=100,
        entity_sector_type=sector,
        confirmed_identity=LegalIdentity(siren="123456789", official_name="ACME"),
        provider_source="https://provider.test/search",
    )


def status_result(status):
    return ProviderEnrichmentResult(
        status=status,
        confidence_score=80 if status in {MatchStatus.REVIEW_NEEDED, MatchStatus.AMBIGUOUS} else None,
        suggested_identity=(LegalIdentity(siren="987654321", official_name="Candidate") if status in {MatchStatus.REVIEW_NEEDED, MatchStatus.AMBIGUOUS} else None),
        provider_source="https://provider.test/search",
    )


class FakeProvider:
    name = "fake"
    source = "https://provider.test/search"

    def __init__(self, actions):
        self.actions = {key: list(value) for key, value in actions.items()}
        self.calls = []

    def enrich(self, item):
        self.calls.append(item.company_key)
        action = self.actions[item.company_key].pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class FakeDinumClient:
    def __init__(self, candidates=(), error=None):
        self.candidates = candidates
        self.error = error

    def search(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.candidates


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def orchestrator(provider, now=at(16), **policy):
    sleeps = []
    runner = CompanyEnrichmentBatchOrchestrator(
        provider,
        policy=BatchPolicy(**policy),
        sleeper=sleeps.append,
        clock=lambda: now,
    )
    return runner, sleeps


def test_completed_run_has_exact_status_counters(session):
    items = [opportunity(str(index), f"Company {index}") for index in range(5)]
    outcomes = [high(), status_result(MatchStatus.REVIEW_NEEDED), status_result(MatchStatus.AMBIGUOUS), status_result(MatchStatus.GENERIC), status_result(MatchStatus.NOT_FOUND)]
    provider = FakeProvider({item.company_key: [outcome] for item, outcome in zip(items, outcomes)})
    runner, _ = orchestrator(provider)

    run = runner.run(session, items)

    assert run.status == EnrichmentRunStatus.COMPLETED
    assert run.selected_count == run.processed_count == 5
    assert (run.high_confidence_count, run.review_needed_count, run.ambiguous_count) == (1, 1, 1)
    assert (run.generic_count, run.not_found_count, run.error_count) == (1, 1, 0)


def test_individual_network_error_is_isolated_and_run_completes_with_errors(session):
    first, second = opportunity("first", "First"), opportunity("second", "Second")
    provider = FakeProvider({
        "first": [ProviderCallError("http_400", transient=False)],
        "second": [high()],
    })
    runner, _ = orchestrator(provider)

    run = runner.run(session, [first, second])
    failed_row = session.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_key == "first"))

    assert run.status == EnrichmentRunStatus.COMPLETED_WITH_ERRORS
    assert run.processed_count == 2 and run.error_count == 1 and run.high_confidence_count == 1
    assert failed_row.match_status == MatchStatus.ERROR
    assert failed_row.last_error_type == "http_400"
    assert failed_row.siren is None


def test_transient_failure_retries_once_then_succeeds(session):
    item = opportunity()
    provider = FakeProvider({"acme": [ProviderCallError("timeout", True), high()]})
    runner, sleeps = orchestrator(provider, max_retries=1, initial_backoff_seconds=0.5)

    run = runner.run(session, [item])
    row = session.scalar(select(CompanyEnrichment))

    assert run.status == EnrichmentRunStatus.COMPLETED
    assert provider.calls == ["acme", "acme"]
    assert sleeps == [0.5]
    assert row.attempt_count == 2


def test_http_429_retry_is_strictly_limited(session):
    item = opportunity()
    provider = FakeProvider({"acme": [ProviderCallError("rate_limited", True), ProviderCallError("rate_limited", True)]})
    runner, sleeps = orchestrator(provider, max_retries=1, systemic_error_threshold=3)

    run = runner.run(session, [item])

    assert run.status == EnrichmentRunStatus.COMPLETED_WITH_ERRORS
    assert len(provider.calls) == 2
    assert sleeps == [1.0]


def test_systemic_transient_errors_stop_batch_cleanly(session):
    items = [opportunity(str(index), f"Company {index}") for index in range(4)]
    provider = FakeProvider({
        item.company_key: [ProviderCallError("http_503", True)] for item in items
    })
    runner, _ = orchestrator(provider, max_retries=0, systemic_error_threshold=2)

    run = runner.run(session, items)

    assert run.status == EnrichmentRunStatus.FAILED
    assert run.processed_count == run.error_count == 2
    assert provider.calls == ["0", "1"]
    assert session.query(CompanyEnrichment).count() == 2


def test_recent_high_confidence_is_reused_without_provider_call(session):
    item = opportunity()
    seed = FakeProvider({"acme": [high()]})
    first, _ = orchestrator(seed, now=at(1))
    first.run(session, [item])
    provider = FakeProvider({"acme": [AssertionError("cache miss")]})
    resumed, _ = orchestrator(provider, now=at(20), confirmed_ttl=timedelta(days=30))

    run = resumed.run(session, [item])

    assert run.status == EnrichmentRunStatus.COMPLETED
    assert provider.calls == []
    assert run.processed_count == run.high_confidence_count == 1
    assert session.scalar(select(CompanyEnrichment)).attempt_count == 1


def test_expired_ttl_causes_new_attempt(session):
    item = opportunity()
    first, _ = orchestrator(FakeProvider({"acme": [high()]}), now=at(1))
    first.run(session, [item])
    provider = FakeProvider({"acme": [high()]})
    refresh, _ = orchestrator(
        provider,
        now=datetime(2026, 10, 2, 9, tzinfo=timezone.utc),
        confirmed_ttl=timedelta(days=30),
    )

    refresh.run(session, [item])

    assert provider.calls == ["acme"]
    assert session.scalar(select(CompanyEnrichment)).attempt_count == 2


def test_changed_fingerprint_causes_new_attempt(session):
    original = opportunity()
    first, _ = orchestrator(FakeProvider({"acme": [high()]}), now=at(1))
    first.run(session, [original])
    changed = opportunity(communes=("94028", "94017"))
    provider = FakeProvider({"acme": [high()]})
    refresh, _ = orchestrator(provider, now=at(2))

    refresh.run(session, [changed])

    assert provider.calls == ["acme"]
    assert session.scalar(select(CompanyEnrichment)).attempt_count == 2


def test_interrupted_batch_is_failed_and_later_resume_skips_fresh_result(session):
    first_item, second_item = opportunity("first", "First"), opportunity("second", "Second")
    interrupted_provider = FakeProvider({
        "first": [high()],
        "second": [KeyboardInterrupt()],
    })
    interrupted, _ = orchestrator(interrupted_provider, now=at(1))

    with pytest.raises(KeyboardInterrupt):
        interrupted.run(session, [first_item, second_item])

    failed_run = session.scalars(select(EnrichmentRun).order_by(EnrichmentRun.id)).first()
    assert failed_run.status == EnrichmentRunStatus.FAILED
    assert session.query(CompanyEnrichment).count() == 1

    resume_provider = FakeProvider({"first": [AssertionError("should be cached")], "second": [high()]})
    resumed, _ = orchestrator(resume_provider, now=at(2))
    run = resumed.run(session, [first_item, second_item])

    assert run.status == EnrichmentRunStatus.COMPLETED
    assert resume_provider.calls == ["second"]
    assert session.query(CompanyEnrichment).count() == 2


def test_duplicate_company_key_is_selected_only_once(session):
    provider = FakeProvider({"acme": [high()]})
    runner, _ = orchestrator(provider)

    run = runner.run(session, [opportunity(), opportunity(name="ACME duplicate")])

    assert run.selected_count == run.processed_count == 1
    assert provider.calls == ["acme"]


def test_dinum_adapter_maps_confirmed_identity_without_leaking_provider_structure():
    candidate = normalize_candidate({
        "siren": "123456789",
        "nom_complet": "ACME",
        "nature_juridique": "5710",
        "etat_administratif": "A",
        "complements": {"est_administration": False, "est_association": False},
        "matching_etablissements": [{
            "siret": "12345678900010", "commune": "94028",
            "libelle_commune": "Créteil", "code_postal": "94000",
        }],
    })
    provider = DinumCompanyEnrichmentProvider(FakeDinumClient((candidate,)))

    result = provider.enrich(opportunity())

    assert result.status == MatchStatus.HIGH_CONFIDENCE
    assert result.confirmed_identity.siren == "123456789"
    assert result.confirmed_identity.commune == "Créteil"
    assert result.entity_sector_type == "private"
    assert result.match_reasons
    assert result.match_signals


def test_dinum_adapter_marks_429_as_transient_without_exposing_http_details():
    provider = DinumCompanyEnrichmentProvider(
        FakeDinumClient(error=DinumSearchError("rate_limited"))
    )

    with pytest.raises(ProviderCallError) as raised:
        provider.enrich(opportunity())

    assert raised.value.error_type == "rate_limited"
    assert raised.value.transient is True


def test_run_selection_is_durable_and_unique_before_processing(session):
    runner, _ = orchestrator(FakeProvider({}))

    run = runner.create_run(session, [opportunity(), opportunity(name="duplicate")])
    items = session.scalars(select(EnrichmentRunItem)).all()

    assert run.selected_count == 1
    assert len(items) == 1
    assert items[0].status == RunItemStatus.PENDING
    assert items[0].input_snapshot["company_name"] == "ACME"
    duplicate = EnrichmentRunItem(
        run_id=run.id, company_key="acme", source_company_name="ACME",
        selection_position=1, input_snapshot=items[0].input_snapshot,
        input_fingerprint=items[0].input_fingerprint, status=RunItemStatus.PENDING,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_same_failed_run_resumes_pending_and_orphan_processing_only(session):
    first_item, second_item = opportunity("first", "First"), opportunity("second", "Second")
    provider = FakeProvider({"first": [high()], "second": [KeyboardInterrupt()]})
    runner, _ = orchestrator(provider, now=at(1))

    with pytest.raises(KeyboardInterrupt):
        runner.run(session, [first_item, second_item])

    run = session.scalar(select(EnrichmentRun))
    before = session.scalars(
        select(EnrichmentRunItem).order_by(EnrichmentRunItem.selection_position)
    ).all()
    assert run.status == EnrichmentRunStatus.FAILED
    assert [item.status for item in before] == [RunItemStatus.COMPLETED, RunItemStatus.PROCESSING]
    assert run.selected_count == 2

    resume_provider = FakeProvider({"second": [high()]})
    resumed, _ = orchestrator(resume_provider, now=at(2))
    resumed_run = resumed.resume(session, run.id)
    after = session.scalars(
        select(EnrichmentRunItem).order_by(EnrichmentRunItem.selection_position)
    ).all()

    assert resumed_run.status == EnrichmentRunStatus.COMPLETED
    assert resume_provider.calls == ["second"]
    assert [item.status for item in after] == [RunItemStatus.COMPLETED, RunItemStatus.COMPLETED]
    assert after[1].attempt_count == 2
    assert resumed_run.selected_count == resumed_run.processed_count == 2


def test_cached_item_is_durable_and_not_reprocessed_on_resume(session):
    item = opportunity()
    first, _ = orchestrator(FakeProvider({"acme": [high()]}), now=at(1))
    first.run(session, [item])
    provider = FakeProvider({"acme": [AssertionError("must remain cached")]})
    second, _ = orchestrator(provider, now=at(2))
    run = second.run(session, [item])
    run_item = session.scalar(
        select(EnrichmentRunItem).where(EnrichmentRunItem.run_id == run.id)
    )

    assert run_item.status == RunItemStatus.CACHED
    assert run_item.attempt_count == 0
    second.resume(session, run.id)
    assert provider.calls == []


def test_cli_resume_uses_simulated_provider_and_logs_only_counts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cli.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    provider = FakeProvider({"acme": [high()]})
    with factory() as setup_session:
        runner, _ = orchestrator(provider)
        run = runner.create_run(setup_session, [opportunity()])
        run_id = run.id
    output = []

    code = execute_cli(
        ["resume", "--run-id", str(run_id)],
        session_factory=factory,
        provider=provider,
        output=output.append,
        prepare_schema=False,
    )

    assert code == 0
    assert provider.calls == ["acme"]
    rendered = "\n".join(output)
    assert "processed=1/1" in rendered
    assert "client_secret" not in rendered.casefold()
    assert "token" not in rendered.casefold()
    assert "ACME" not in rendered
    engine.dispose()


def test_legacy_failed_run_without_items_is_not_modified(session):
    run = EnrichmentRun(
        provider="fake", started_at=at(1), finished_at=at(1, 9), status="failed",
        selected_count=120, processed_count=117,
    )
    session.add(run)
    session.commit()
    runner, _ = orchestrator(FakeProvider({}))

    with pytest.raises(ValueError, match="predates durable run items"):
        runner.resume(session, run.id)

    session.refresh(run)
    assert run.status == "failed"
    assert run.selected_count == 120 and run.processed_count == 117


def test_systemic_failure_resume_processes_pending_but_not_error_items(session):
    items = [opportunity(str(index), f"Company {index}") for index in range(4)]
    first_provider = FakeProvider({
        item.company_key: [ProviderCallError("http_503", True)] for item in items
    })
    first, _ = orchestrator(
        first_provider, max_retries=0, systemic_error_threshold=2, now=at(1)
    )
    failed_run = first.run(session, items)
    assert failed_run.status == EnrichmentRunStatus.FAILED

    resume_provider = FakeProvider({"2": [high()], "3": [high()]})
    resumed, _ = orchestrator(resume_provider, now=at(2))
    final_run = resumed.resume(session, failed_run.id)
    states = session.scalars(
        select(EnrichmentRunItem)
        .where(EnrichmentRunItem.run_id == failed_run.id)
        .order_by(EnrichmentRunItem.selection_position)
    ).all()

    assert resume_provider.calls == ["2", "3"]
    assert [item.status for item in states] == [
        RunItemStatus.ERROR, RunItemStatus.ERROR,
        RunItemStatus.COMPLETED, RunItemStatus.COMPLETED,
    ]
    assert final_run.status == EnrichmentRunStatus.COMPLETED_WITH_ERRORS
    assert final_run.selected_count == final_run.processed_count == 4
