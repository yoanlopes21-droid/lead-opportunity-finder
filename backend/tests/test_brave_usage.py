from datetime import datetime, timezone
from decimal import Decimal
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import BraveUsageEvent
from app.services.brave_usage import BraveBudgetExceeded, BraveBudgetPolicy, BraveUsageService
from app.services.contactability.providers.official_web.brave_client import BraveSearchClient, BraveSearchError
from app.services.contactability.contracts import ContactScope, ContactTarget
from app.services.contactability.providers.official_web.contracts import WebsiteSeed
from app.services.contactability.providers.official_web.discovery import discover_website_candidates


NOW = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'brave-usage.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def service(session, **changes):
    values = dict(monthly_request_budget=1000, default_run_hard_cap=2,
                  estimated_price_per_1000_usd=Decimal("5"), estimated_monthly_free_credit_usd=Decimal("5"))
    values.update(changes)
    return BraveUsageService(session, BraveBudgetPolicy(**values), now=lambda: NOW)


class Response:
    status_code = 200
    def json(self): return {"web": {"results": [{"url": "https://acme.fr"}]}}


def client(usage, requester=lambda *args, **kwargs: Response()):
    return BraveSearchClient(api_key="test-key", requester=requester, usage_service=usage, sleeper=lambda _: None)


def target():
    return ContactTarget(
        company_key="acme", organization_name_snapshot="ACME", scope=ContactScope.COMPANY,
        siren="123456789", local_key=None, local_commune_snapshot=None,
        local_location_label_snapshot=None, employer_relationship_status="direct_employer",
        identity_match_status="matched_high_confidence", display_name_snapshot="ACME",
        identity_location_snapshot="Créteil",
    )


def test_first_and_fallback_requests_are_reserved_and_counted(session):
    brave = client(service(session))
    brave.set_run_id(10)
    brave.search("acme official", company_key="acme", request_index=1)
    brave.search("acme contact", company_key="acme", request_index=2)
    events = session.query(BraveUsageEvent).order_by(BraveUsageEvent.id).all()
    assert [(item.run_id, item.request_index, item.outcome) for item in events] == [(10, 1, "completed"), (10, 2, "completed")]


def test_connection_failure_before_dispatch_is_kept_but_not_counted(session):
    usage = service(session, monthly_request_budget=1)
    failing = client(usage, requester=lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("nope")))
    failing.set_run_id(9)
    with pytest.raises(BraveSearchError):
        failing.search("acme", company_key="acme", request_index=1)
    event = session.query(BraveUsageEvent).one()
    assert (event.outcome, event.counted_for_budget) == ("connection_failed_pre_dispatch", False)
    # The run/monthly slot is released after the confirmed pre-dispatch error.
    assert service(session, monthly_request_budget=1).snapshot().monthly_remaining == 1
    succeeding = client(service(session, monthly_request_budget=1))
    succeeding.set_run_id(9)
    succeeding.search("acme retry", company_key="acme", request_index=1)
    assert service(session, monthly_request_budget=1).snapshot().monthly_used == 1


def test_read_timeout_is_counted_conservatively_after_possible_dispatch(session):
    failing = client(service(session), requester=lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("nope")))
    with pytest.raises(BraveSearchError, match="timed out"):
        failing.search("acme", company_key="acme", request_index=1)
    event = session.query(BraveUsageEvent).one()
    assert (event.outcome, event.counted_for_budget) == ("transport_outcome_unknown", True)


@pytest.mark.parametrize("status", [429, 500])
def test_provider_http_response_is_counted(session, status):
    response = Response()
    response.status_code = status
    with pytest.raises(BraveSearchError):
        client(service(session), requester=lambda *args, **kwargs: response).search("acme")
    assert session.query(BraveUsageEvent).one().counted_for_budget is True


def test_run_and_monthly_caps_are_shared_without_double_counting(session):
    usage = service(session, monthly_request_budget=3, default_run_hard_cap=2)
    brave = client(usage)
    brave.set_run_id(1)
    brave.search("one")
    brave.search("two")
    with pytest.raises(BraveBudgetExceeded, match="run_budget_exhausted"):
        brave.search("three")
    brave.set_run_id(2)
    brave.search("four")
    with pytest.raises(BraveBudgetExceeded, match="monthly_budget_exhausted"):
        brave.search("five")
    assert session.query(BraveUsageEvent).count() == 3


def test_sqlite_concurrent_reservations_cannot_exceed_a_hard_cap(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-brave-usage.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    barrier = threading.Barrier(2)
    outcomes = []

    def reserve(index):
        with Session(engine) as local_session:
            local_usage = service(local_session, monthly_request_budget=1, default_run_hard_cap=1)
            barrier.wait()
            try:
                local_usage.reserve_request(
                    run_id=10, company_key=f"acme-{index}", query=f"query-{index}", request_index=1,
                )
                outcomes.append("reserved")
            except BraveBudgetExceeded as exc:
                outcomes.append(exc.kind)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["monthly_budget_exhausted", "reserved"]
    engine.dispose()


def test_history_is_idempotent_and_months_keep_project_total(session):
    usage = service(session)
    assert usage.seed_initial_history() is True
    assert usage.seed_initial_history() is False
    september = usage.snapshot(at=NOW)
    october = usage.snapshot(at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert (september.monthly_used, september.monthly_remaining, september.estimated_cost_used_usd) == (14, 986, Decimal("0.07"))
    assert october.monthly_used == 0 and october.project_total_requests == 14


def test_reconciliation_keeps_run_attempts_but_restores_estimated_budget(session):
    usage = service(session)
    usage.seed_initial_history()
    events = []
    for index in range(6):
        event = usage.reserve_request(
            run_id=4, company_key="acme", query=f"query-{index}", request_index=1,
            run_hard_cap=8,
        )
        usage.record_outcome(event.id, "network_error")
        events.append(event.id)
    assert usage.snapshot().monthly_used == 20
    assert usage.reconcile_pre_dispatch_failures(tuple(events), note="Confirmed pre-provider transport failure") == 6
    snapshot = usage.snapshot()
    assert (snapshot.monthly_used, snapshot.monthly_remaining, snapshot.estimated_cost_used_usd) == (14, 986, Decimal("0.07"))
    assert (snapshot.project_total_attempts, snapshot.project_total_counted_requests) == (20, 14)


def test_snapshot_pacing_is_deterministic(session):
    snapshot = service(session).snapshot()
    assert snapshot.status == "healthy"
    assert snapshot.maximum_allowed_for_next_run == 2
    assert snapshot.pacing_per_day == Decimal("76.92")


def test_discovery_fallback_is_counted_twice_but_cached_seed_is_free(session):
    calls = []
    def requester(*args, **kwargs):
        calls.append(kwargs["params"]["q"])
        response = Response()
        url = "https://unrelated.example" if len(calls) == 1 else "https://acme.fr"
        response.json = lambda: {"web": {"results": [{"url": url}]}}
        return response
    brave = client(service(session), requester=requester)
    brave.set_run_id(4)
    discover_website_candidates(target(), target_fingerprint="x" * 64, seeds=(), brave_client=brave, observed_at=NOW)
    assert len(calls) == session.query(BraveUsageEvent).count() == 2
    discover_website_candidates(
        target(), target_fingerprint="y" * 64,
        seeds=(WebsiteSeed("https://acme.fr", "offer_description", NOW),),
        brave_client=brave, observed_at=NOW,
    )
    assert session.query(BraveUsageEvent).count() == 2


def test_usage_endpoint_is_read_only_and_exposes_estimates(session):
    def override_get_db():
        yield session
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/brave-usage")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert {"monthly_remaining", "estimated_cost_used_usd", "maximum_allowed_for_next_run"} <= set(response.json())
