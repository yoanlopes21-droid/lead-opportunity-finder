from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import CompanyEnrichment, CompanyEnrichmentDetail, EnrichmentRun
from app.services.company_enrichment.contracts import (
    LegalIdentity,
    MatchStatus,
    ProviderEnrichmentResult,
    ProviderCallError,
)
from app.services.company_enrichment.persistence import (
    EnrichmentRunStatus,
    compute_input_fingerprint,
    create_enrichment_run,
    finish_enrichment_run,
    ensure_enrichment_schema,
    is_fresh_reusable_high_confidence,
    upsert_company_enrichment,
)
from app.services.opportunities.company import CompanyOpportunity


def at(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def opportunity(**overrides) -> CompanyOpportunity:
    values = {
        "company_key": "acme sas", "company_name": "ACME SAS", "department_code": "94",
        "active_offer_count": 2, "distinct_job_title_count": 2, "distinct_source_count": 1,
        "sources": ("test",), "oldest_offer_created_at": None, "newest_offer_created_at": None,
        "oldest_offer_age_days": None, "newest_offer_age_days": None, "contract_types": ("CDI",),
        "communes": ("94028",), "location_labels": ("Créteil",),
        "offer_ids": ("test:1", "test:2"), "job_titles": ("A", "B"),
        "cdi_offer_count": 2, "cdd_offer_count": 0, "other_contract_offer_count": 0,
        "offers_over_21_days": 0, "offers_over_45_days": 0, "offers_over_90_days": 0,
        "average_offer_age_days": None, "median_offer_age_days": None, "signals": (),
    }
    values.update(overrides)
    return CompanyOpportunity(**values)


def identity(**overrides) -> LegalIdentity:
    values = {
        "siren": "123456789", "siret": "12345678900010", "official_name": "ACME SAS",
        "address": "1 rue Exemple", "postal_code": "94000", "commune": "Créteil",
        "naf_code": "62.01Z", "activity_label": "Programmation informatique",
        "legal_nature": "5710", "employee_range": "20-49", "administrative_status": "A",
    }
    values.update(overrides)
    return LegalIdentity(**values)


def result(status=MatchStatus.HIGH_CONFIDENCE, sector="private", **overrides):
    values = {
        "status": status,
        "confidence_score": 100.0,
        "entity_sector_type": sector,
        "confirmed_identity": identity() if status == MatchStatus.HIGH_CONFIDENCE else None,
        "suggested_identity": identity() if status in {MatchStatus.REVIEW_NEEDED, MatchStatus.AMBIGUOUS} else None,
        "provider_source": "https://provider.test/search",
    }
    values.update(overrides)
    return ProviderEnrichmentResult(**values)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'enrichment-test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def test_high_confidence_persists_confirmed_identity(session):
    item = opportunity()
    run = create_enrichment_run(session, "dinum", 1, now=at(16))
    row = upsert_company_enrichment(
        session, run, item, result(), compute_input_fingerprint(item), now=at(16, 9)
    )

    assert row.siren == "123456789"
    assert row.official_name == "ACME SAS"
    assert row.suggested_siren is None
    assert row.entity_sector_type == "private"
    assert row.attempt_count == 1


@pytest.mark.parametrize("status", [MatchStatus.REVIEW_NEEDED, MatchStatus.AMBIGUOUS])
def test_review_or_ambiguous_keeps_only_a_separate_suggestion(session, status):
    item = opportunity()
    run = create_enrichment_run(session, "dinum", 1, now=at(16))
    row = upsert_company_enrichment(
        session, run, item, result(status), compute_input_fingerprint(item), now=at(16, 9)
    )

    assert row.siren is None
    assert row.official_name is None
    assert row.suggested_siren == "123456789"
    assert row.suggested_name == "ACME SAS"
    assert row.suggested_score == 100.0


@pytest.mark.parametrize("status", [MatchStatus.GENERIC, MatchStatus.NOT_FOUND, MatchStatus.ERROR])
def test_non_identity_statuses_never_store_identity_or_suggestion(session, status):
    item = opportunity()
    run = create_enrichment_run(session, "dinum", 1, now=at(16))
    row = upsert_company_enrichment(
        session,
        run,
        item,
        result(status, confirmed_identity=identity(), suggested_identity=identity()),
        compute_input_fingerprint(item),
        now=at(16, 9),
        error_type="timeout" if status == MatchStatus.ERROR else None,
        error_message="Provider request failed (timeout)." if status == MatchStatus.ERROR else None,
    )

    assert row.siren is None
    assert row.suggested_siren is None
    if status == MatchStatus.ERROR:
        assert row.enriched_at is None
    else:
        assert row.enriched_at == at(16, 9)


def test_upsert_reuses_provider_key_and_increments_attempt_count(session):
    item = opportunity()
    first_run = create_enrichment_run(session, "dinum", 1, now=at(15))
    first = upsert_company_enrichment(
        session, first_run, item, result(), compute_input_fingerprint(item), attempts=1, now=at(15, 9)
    )
    second_run = create_enrichment_run(session, "dinum", 1, now=at(16))
    second = upsert_company_enrichment(
        session, second_run, item, result(MatchStatus.NOT_FOUND), compute_input_fingerprint(item), attempts=2, now=at(16, 9)
    )

    assert second.id == first.id
    assert second.attempt_count == 3
    assert session.query(CompanyEnrichment).count() == 1
    assert second.siren is None


def test_ambiguous_result_never_inherits_previous_confirmed_identity(session):
    item = opportunity()
    first_run = create_enrichment_run(session, "dinum", 1, now=at(15))
    upsert_company_enrichment(
        session, first_run, item, result(), compute_input_fingerprint(item), now=at(15, 9)
    )
    second_run = create_enrichment_run(session, "dinum", 1, now=at(16))
    row = upsert_company_enrichment(
        session, second_run, item, result(MatchStatus.AMBIGUOUS), compute_input_fingerprint(item), now=at(16, 9)
    )

    assert row.match_status == MatchStatus.AMBIGUOUS
    assert row.siren is None
    assert row.suggested_siren == "123456789"


@pytest.mark.parametrize("sector", ["private", "public", "nonprofit", "unknown"])
def test_entity_sector_type_is_preserved(session, sector):
    item = opportunity(company_key=f"{sector}-key", company_name=sector)
    run = create_enrichment_run(session, "dinum", 1, now=at(16))
    row = upsert_company_enrichment(
        session, run, item, result(sector=sector), compute_input_fingerprint(item), now=at(16, 9)
    )
    assert row.entity_sector_type == sector


def test_fingerprint_is_deterministic_and_changes_with_identity_inputs():
    base = opportunity()
    same = opportunity(communes=("94028",), location_labels=("Créteil",))
    changed = opportunity(communes=("94028", "94017"))

    assert compute_input_fingerprint(base) == compute_input_fingerprint(same)
    assert compute_input_fingerprint(base) != compute_input_fingerprint(changed)


def test_high_confidence_cache_obeys_fingerprint_and_ttl(session):
    item = opportunity()
    fingerprint = compute_input_fingerprint(item)
    run = create_enrichment_run(session, "dinum", 1, now=at(1))
    row = upsert_company_enrichment(session, run, item, result(), fingerprint, now=at(1, 9))

    assert is_fresh_reusable_high_confidence(row, fingerprint, at(20), timedelta(days=30))
    assert not is_fresh_reusable_high_confidence(row, "changed", at(20), timedelta(days=30))
    assert not is_fresh_reusable_high_confidence(
        row,
        fingerprint,
        datetime(2026, 10, 2, 10, tzinfo=timezone.utc),
        timedelta(days=30),
    )


def test_run_lifecycle_accepts_all_terminal_statuses(session):
    for index, status in enumerate((
        EnrichmentRunStatus.COMPLETED,
        EnrichmentRunStatus.COMPLETED_WITH_ERRORS,
        EnrichmentRunStatus.FAILED,
    )):
        run = create_enrichment_run(session, f"provider-{index}", 0, now=at(16))
        finish_enrichment_run(session, run, status, now=at(16, 9))
        assert run.status == status
        assert run.finished_at == at(16, 9)


def test_provider_error_never_retains_raw_potentially_sensitive_message():
    error = ProviderCallError("timeout invalid", transient=True, message="token=do-not-store")

    assert error.error_type == "timeout_invalid"
    assert error.safe_message == "Provider request failed (timeout_invalid)."
    assert "do-not-store" not in str(error)


def test_matching_evidence_and_suggested_candidate_details_are_persisted(session):
    item = opportunity()
    run = create_enrichment_run(session, "dinum", 1, now=at(16))
    enriched = result(
        MatchStatus.REVIEW_NEEDED,
        match_reasons=("Nom exact.", "Commune cohérente."),
        match_signals=("name_exact", "commune_match"),
        suggested_entity_sector_type="private",
        candidate_aliases=("ACME", "ACME FRANCE"),
    )

    row = upsert_company_enrichment(
        session, run, item, enriched, compute_input_fingerprint(item), now=at(16, 9)
    )
    detail = session.scalar(
        select(CompanyEnrichmentDetail).where(
            CompanyEnrichmentDetail.enrichment_id == row.id
        )
    )

    assert detail.match_reasons == ["Nom exact.", "Commune cohérente."]
    assert detail.match_signals == ["name_exact", "commune_match"]
    assert detail.suggested_commune == "Créteil"
    assert detail.suggested_postal_code == "94000"
    assert detail.suggested_entity_sector_type == "private"
    assert detail.candidate_aliases == ["ACME", "ACME FRANCE"]
    assert row.siren is None


def test_complementary_schema_evolution_preserves_old_enrichments(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old-schema.sqlite3'}")
    Base.metadata.create_all(
        engine,
        tables=[EnrichmentRun.__table__, CompanyEnrichment.__table__],
    )
    with Session(engine) as old_session:
        old_run = EnrichmentRun(
            provider="dinum", started_at=at(1), finished_at=at(1, 9),
            status="failed", selected_count=120, processed_count=117,
        )
        old_session.add(old_run)
        old_session.flush()
        old_session.add(CompanyEnrichment(
            company_key="legacy", source_company_name="Legacy", provider="dinum",
            match_status=MatchStatus.NOT_FOUND, entity_sector_type="unknown",
            last_attempt_at=at(1), attempt_count=1, input_fingerprint="a" * 64,
            last_run_id=old_run.id,
        ))
        old_session.commit()

    ensure_enrichment_schema(engine)

    assert {"company_enrichment_details", "enrichment_run_items"}.issubset(
        set(inspect(engine).get_table_names())
    )
    with Session(engine) as migrated_session:
        assert migrated_session.query(CompanyEnrichment).count() == 1
        assert migrated_session.query(EnrichmentRun).one().status == "failed"
        assert migrated_session.query(CompanyEnrichmentDetail).count() == 0
    engine.dispose()
