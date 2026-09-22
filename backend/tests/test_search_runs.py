"""Search-run orchestration is entirely local: no provider HTTP is used here."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import BraveUsageEvent, CommercialExclusion, ContactEvidence, ContactPoint, ObservedJobOffer, SearchRunItem, VerifiedWebsiteRecord
from app.services.commercial_leads.exclusions import ExclusionType
from app.services.brave_usage import BraveBudgetExceeded, BraveBudgetPolicy, BraveUsageService
from app.services.search_runs import (
    OfficialWebSearchEnricher, SearchRunCompletionReason, SearchRunItemStatus,
    SearchRunOrchestrator, SearchRunStatus, ensure_search_run_schema, is_actionable_lead,
)
from app.services.contactability.contracts import ContactScope
from app.services.contactability.providers.official_web.contracts import WebsiteVerificationStatus


NOW = datetime(2026, 9, 21, 9, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'search-runs.sqlite3'}")
    Base.metadata.create_all(engine)
    ensure_search_run_schema(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def offer(session, key, company=None, *, description=None):
    session.add(ObservedJobOffer(
        source="france_travail", source_offer_id=key, title="Technicien", company_name=company or key.upper(),
        location_label="Créteil", commune="Créteil", department_code="94", description=description,
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


class MockOfficialWebEnricher(OfficialWebSearchEnricher):
    def __init__(self, *, add_contact=False):
        self.calls = []
        self.add_contact = add_contact

    def enrich(self, session, lead, run):
        self.calls.append(lead.company_key)
        if self.add_contact:
            actionable_contact(session, lead.company_key)


def verified_site(session, key, *, fresh_until=NOW + timedelta(days=30)):
    session.add(VerifiedWebsiteRecord(
        company_key=key, target_scope=ContactScope.COMPANY, local_key=None,
        target_fingerprint=f"target-{key}", candidate_fingerprint=f"candidate-{key}",
        candidate_set_fingerprint=f"set-{key}", provider="official_web",
        canonical_url=f"https://{key}.test", registrable_domain=f"{key}.test",
        status=WebsiteVerificationStatus.HIGH_CONFIDENCE, score=95, rejection_reasons=[],
        attribution_warnings=[], observed_at=NOW, verified_at=NOW,
        fresh_until=fresh_until, fingerprint=f"site-{key}",
    ))
    session.commit()


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


def test_zero_brave_without_reusable_web_signal_skips_large_scan_without_enrichment(session):
    for index in range(120):
        offer(session, f"unseeded-{index}")
    enricher = MockOfficialWebEnricher()
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)

    completed = runner.resume(session, runner.create(
        session, requested_actionable_leads=100, brave_hard_cap=0,
    ).id)

    assert enricher.calls == []
    assert completed.brave_requests_used == 0
    assert completed.candidates_considered == 120
    assert completed.candidates_enriched == 0
    assert completed.completion_reason == SearchRunCompletionReason.CANDIDATES_EXHAUSTED

    # Existing completed rows gain an accurate read-time reason without an
    # artificial historical update.
    completed.completion_reason = None
    session.commit()
    assert runner.progress(session, completed.id).completion_reason == SearchRunCompletionReason.CANDIDATES_EXHAUSTED
    assert completed.completion_reason is None


def test_zero_brave_allows_known_site_when_extraction_can_be_refreshed(session):
    offer(session, "known-site")
    verified_site(session, "known site", fresh_until=NOW - timedelta(days=1))
    enricher = MockOfficialWebEnricher(add_contact=True)
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)

    completed = runner.resume(session, runner.create(
        session, requested_actionable_leads=1, brave_hard_cap=0,
    ).id)

    assert enricher.calls == ["known site"]
    assert completed.current_actionable_leads == 1
    assert completed.brave_requests_used == 0
    assert completed.completion_reason == SearchRunCompletionReason.TARGET_REACHED


def test_zero_brave_allows_offer_description_url_without_discovery(session):
    offer(session, "seeded", description="Candidatures sur https://seeded.test/recrutement")
    enricher = MockOfficialWebEnricher(add_contact=True)
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)

    completed = runner.resume(session, runner.create(
        session, requested_actionable_leads=1, brave_hard_cap=0,
    ).id)

    assert enricher.calls == ["seeded"]
    assert completed.current_actionable_leads == 1
    assert completed.brave_requests_used == 0


def test_actionable_cache_never_calls_official_web_even_with_zero_brave(session):
    offer(session, "cached")
    actionable_contact(session, "cached")
    enricher = MockOfficialWebEnricher()
    runner = SearchRunOrchestrator(enricher, clock=lambda: NOW)

    completed = runner.resume(session, runner.create(
        session, requested_actionable_leads=1, brave_hard_cap=0,
    ).id)

    assert completed.current_actionable_leads == 1
    assert enricher.calls == []


def test_run_prioritizes_actionable_then_reusable_web_signal_with_score_order_stable(session):
    offer(session, "a", company="A DISCOVERY")
    offer(session, "m", company="M SEEDED")
    offer(session, "z", company="Z ACTIONABLE")
    actionable_contact(session, "z actionable")
    verified_site(session, "m seeded")

    run = SearchRunOrchestrator(clock=lambda: NOW).create(
        session, requested_actionable_leads=3, brave_hard_cap=0,
    )
    positions = session.scalars(select(SearchRunItem).where(
        SearchRunItem.run_id == run.id,
    ).order_by(SearchRunItem.selection_position)).all()

    assert [item.company_key for item in positions] == ["z actionable", "m seeded", "a discovery"]


def test_progress_exposes_human_company_name_and_timezone_aware_timestamps(session):
    offer(session, "human", company="Human Company SAS")

    class ObserveCurrentCompany:
        def enrich(self, session, lead, run):
            session.refresh(run)
            assert run.current_company_key == "human company sas"
            assert run.current_company_name == "Human Company SAS"
            actionable_contact(session, lead.company_key)

    runner = SearchRunOrchestrator(ObserveCurrentCompany(), clock=lambda: NOW)
    completed = runner.resume(session, runner.create(session, requested_actionable_leads=1).id)
    progress = runner.progress(session, completed.id)

    assert progress.created_at.utcoffset() == timedelta(0)
    assert progress.started_at.utcoffset() == timedelta(0)
    assert progress.finished_at.utcoffset() == timedelta(0)


def test_selected_channel_requires_its_own_provenance(session):
    offer(session, "provenance")
    selected = ContactPoint(
        company_key="provenance", scope="company", local_key=None, siren=None,
        organization_name_snapshot="PROVENANCE", local_commune_snapshot=None,
        local_location_label_snapshot=None, contact_type="email", value="recrutement@provenance.test",
        normalized_value="recrutement@provenance.test", confidence_level="confirmed",
        verification_status="source_verified", attribution_reason=None, person_contact_id=None,
        fingerprint="point-selected", first_observed_at=NOW, last_observed_at=NOW, is_active=True,
    )
    session.add(selected)
    session.flush()
    actionable_contact(session, "provenance-other")
    # Move the sourced secondary point onto the same commercial lead.
    secondary = session.scalar(select(ContactPoint).where(ContactPoint.company_key == "provenance-other"))
    secondary.company_key = "provenance"
    secondary.value = "z@provenance.test"
    secondary.normalized_value = "z@provenance.test"
    session.commit()

    runner = SearchRunOrchestrator(clock=lambda: NOW)
    run = runner.create(session, requested_actionable_leads=1)
    lead = runner._leads_by_key(session, run)["provenance"]
    assert lead.contact_strategy.contact_point_id == selected.id
    assert not is_actionable_lead(lead)
    historical_item = session.scalar(select(SearchRunItem).where(
        SearchRunItem.run_id == run.id, SearchRunItem.company_key == "provenance",
    ))
    historical_item.actionable = True
    historical_item.status = SearchRunItemStatus.ACTIONABLE
    session.commit()
    assert runner.results(session, run.id) == ()


def test_terminal_progress_uses_current_actionable_count_without_rewriting_audit(session):
    for index in range(6):
        key = f"current-{index}"
        offer(session, key)
        actionable_contact(session, key.replace("-", " "))
    offer(session, "historical-only")
    session.add(ContactPoint(
        company_key="historical only", scope="company", local_key=None, siren=None,
        organization_name_snapshot="HISTORICAL ONLY", local_commune_snapshot=None,
        local_location_label_snapshot=None, contact_type="email", value="rh@historical-only.test",
        normalized_value="rh@historical-only.test", confidence_level="confirmed",
        verification_status="source_verified", attribution_reason=None, person_contact_id=None,
        fingerprint="point-historical-only", first_observed_at=NOW, last_observed_at=NOW, is_active=True,
    ))
    session.commit()

    runner = SearchRunOrchestrator(clock=lambda: NOW)
    run = runner.create(session, requested_actionable_leads=20)
    items = session.scalars(select(SearchRunItem).where(SearchRunItem.run_id == run.id)).all()
    for item in items:
        item.actionable = True
        item.status = SearchRunItemStatus.ACTIONABLE
    run.status = SearchRunStatus.COMPLETED
    run.current_actionable_leads = 7
    run.completion_reason = SearchRunCompletionReason.CANDIDATES_EXHAUSTED
    session.commit()

    assert len(runner.results(session, run.id)) == 6
    assert runner.progress(session, run.id).current_actionable_leads == 6

    session.expire_all()
    persisted = session.get(type(run), run.id)
    persisted_items = session.scalars(select(SearchRunItem).where(SearchRunItem.run_id == run.id)).all()
    assert persisted.current_actionable_leads == 7
    assert sum(item.actionable for item in persisted_items) == 7
