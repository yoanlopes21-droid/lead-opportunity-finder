"""Search-run orchestration is entirely local: no provider HTTP is used here."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import BraveUsageEvent, CommercialExclusion, ContactEvidence, ContactPoint, ObservedJobOffer, SearchRunItem
from app.services.commercial_leads.exclusions import ExclusionType
from app.services.brave_usage import BraveBudgetExceeded, BraveBudgetPolicy, BraveUsageService
from app.services.search_runs import (
    SearchRunItemStatus, SearchRunOrchestrator, SearchRunStatus, ensure_search_run_schema,
)


NOW = datetime(2026, 9, 21, 9, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'search-runs.sqlite3'}")
    Base.metadata.create_all(engine)
    ensure_search_run_schema(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def offer(session, key, company=None):
    session.add(ObservedJobOffer(
        source="france_travail", source_offer_id=key, title="Technicien", company_name=company or key.upper(),
        location_label="Créteil", commune="Créteil", department_code="94",
        created_at=(NOW - timedelta(days=2)).isoformat(), source_url=f"https://source.test/{key}",
        first_seen_at=NOW, last_seen_at=NOW, last_changed_at=NOW, is_active=True, observation_count=1,
    ))
    session.flush()


def actionable_contact(session, key):
    point = ContactPoint(
        company_key=key, scope="company", local_key=None, siren=None,
        organization_name_snapshot=key.upper(), local_commune_snapshot=None, local_location_label_snapshot=None,
        contact_type="email", value=f"rh@{key}.test", normalized_value=f"rh@{key}.test",
        confidence_level="confirmed", verification_status="source_verified", attribution_reason=None,
        person_contact_id=None, fingerprint=f"point-{key}", first_observed_at=NOW, last_observed_at=NOW, is_active=True,
    )
    session.add(point)
    session.flush()
    session.add(ContactEvidence(
        contact_point_id=point.id, person_contact_id=None, provider="test", source_name="official_site",
        source_url=f"https://{key}.test/contact", source_identifier=None, observed_at=NOW,
        evidence_reason="public contact", excerpt=None, fingerprint=f"evidence-{key}",
    ))
    session.commit()


class AddActionableContact:
    def __init__(self):
        self.calls = []

    def enrich(self, session, lead, run):
        self.calls.append(lead.company_key)
        actionable_contact(session, lead.company_key)


def test_persistent_run_selects_deterministically_and_reuses_actionable_cache(session):
    offer(session, "beta")
    offer(session, "alpha")
    actionable_contact(session, "alpha")
    enricher = AddActionableContact()
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)
    run = runner.create(session, requested_actionable_leads=1)
    positions = session.scalars(select(SearchRunItem).where(SearchRunItem.run_id == run.id).order_by(SearchRunItem.selection_position)).all()
    assert [item.company_key for item in positions] == ["alpha", "beta"]
    run = runner.resume(session, run.id)
    assert run.status == SearchRunStatus.COMPLETED
    assert run.current_actionable_leads == 1 and enricher.calls == []
    assert positions[0].status == SearchRunItemStatus.ACTIONABLE and positions[0].reused_cache


def test_excluded_is_never_sent_to_enrichment(session):
    offer(session, "excluded")
    session.add(CommercialExclusion(company_key="excluded", siren=None, company_name_snapshot="EXCLUDED",
                                    exclusion_type=ExclusionType.CURRENT_CLIENT, starts_at=NOW - timedelta(days=1)))
    session.commit()
    enricher = AddActionableContact()
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)
    run = runner.resume(session, runner.create(session, requested_actionable_leads=1).id)
    assert run.status == SearchRunStatus.COMPLETED and enricher.calls == []


def test_unresolved_is_enriched_once_and_goal_stops_more_candidates(session):
    offer(session, "first")
    offer(session, "second")
    enricher = AddActionableContact()
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)
    run = runner.resume(session, runner.create(session, requested_actionable_leads=1).id)
    assert run.current_actionable_leads == 1
    assert len(enricher.calls) == 1
    assert run.candidates_enriched == 1


def test_stop_and_resume_are_idempotent_and_keep_partial_results(session):
    offer(session, "one")
    runner = SearchRunOrchestrator(AddActionableContact(), clock=lambda: NOW)
    run = runner.create(session, requested_actionable_leads=2)
    runner.request_stop(session, run.id)
    stopped = runner.resume(session, run.id)
    assert stopped.status == SearchRunStatus.STOPPED and stopped.current_actionable_leads == 0
    completed = runner.resume(session, run.id)
    assert completed.current_actionable_leads == 1
    again = runner.resume(session, run.id)
    assert again.current_actionable_leads == 1


def test_brave_ledger_is_counted_once_per_run_and_natural_offer_age_never_calls_enricher(session):
    offer(session, "cached")
    actionable_contact(session, "cached")
    runner = SearchRunOrchestrator(AddActionableContact(), clock=lambda: NOW)
    run = runner.create(session, requested_actionable_leads=1, brave_hard_cap=1)
    session.add(BraveUsageEvent(provider="brave_search", observed_at=NOW, billing_period="2026-09", run_id=run.id,
                                company_key="other", query_fingerprint="q", request_index=1, outcome="completed",
                                counted_for_budget=True, source="live_request", quantity=1))
    session.commit()
    completed = runner.resume(session, run.id)
    assert completed.brave_requests_used == 1
    # The following-day age presentation is derived from dates; no persisted
    # offer field or cache fingerprint changes, hence no new enrichment/Brave call.
    daily_enricher = AddActionableContact()
    tomorrow = SearchRunOrchestrator(daily_enricher, clock=lambda: NOW + timedelta(days=1))
    daily = tomorrow.resume(session, tomorrow.create(session, requested_actionable_leads=1).id)
    assert tomorrow.results(session, daily.id)[0].active_job_offers[0].age_days == 3
    assert daily_enricher.calls == []
    assert completed.brave_requests_used == 1


def test_new_offer_for_known_company_reuses_fresh_contact_but_new_company_can_enrich(session):
    offer(session, "known-1", company="KNOWN")
    actionable_contact(session, "known")
    offer(session, "known-2", company="KNOWN")  # meaningful offer context update, not identity invalidation
    offer(session, "new-company", company="NEW COMPANY")
    enricher = AddActionableContact()
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)
    run = runner.resume(session, runner.create(session, requested_actionable_leads=2).id)
    assert run.current_actionable_leads == 2
    assert enricher.calls == ["new company"]


def test_zero_brave_cap_and_monthly_cap_are_enforced_before_dispatch(session):
    zero_cap = BraveUsageService(session, BraveBudgetPolicy(monthly_request_budget=2, default_run_hard_cap=40), now=lambda: NOW)
    with pytest.raises(BraveBudgetExceeded, match="run_budget_exhausted"):
        zero_cap.reserve_request(run_id=12, company_key="acme", query="ACME", request_index=1, run_hard_cap=0)
    monthly = BraveUsageService(session, BraveBudgetPolicy(monthly_request_budget=1, default_run_hard_cap=40), now=lambda: NOW)
    monthly.reserve_request(run_id=13, company_key="acme", query="ACME", request_index=1, run_hard_cap=1)
    with pytest.raises(BraveBudgetExceeded, match="monthly_budget_exhausted"):
        monthly.reserve_request(run_id=14, company_key="beta", query="BETA", request_index=1, run_hard_cap=1)
