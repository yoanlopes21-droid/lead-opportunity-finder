from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, get_db
from app.main import app
from app.models import CommercialExclusion, CommercialRelationship, ObservedJobOffer
from app.services.commercial_leads.exclusions import ExclusionType
from app.services.commercial_leads.service import (
    CommercialLeadQuery,
    RecentCommercialLeadQuery,
    list_commercial_leads,
    list_recent_commercial_leads,
)


NOW = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recent-leads.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def add_offer(
    session: Session,
    identifier: str,
    company: str,
    *,
    first_seen_hours_ago: int,
    title: str = "Technicien",
    source: str = "france_travail",
    created_days_ago: int = 5,
    observation_count: int = 1,
    active: bool = True,
) -> ObservedJobOffer:
    first_seen = NOW - timedelta(hours=first_seen_hours_ago)
    offer = ObservedJobOffer(
        source=source,
        source_offer_id=identifier,
        title=title,
        company_name=company,
        location_label="Créteil",
        commune="94028",
        department_code="94",
        created_at=(NOW - timedelta(days=created_days_ago)).isoformat().replace("+00:00", "Z"),
        source_url=f"https://{source}.example/{identifier}",
        first_seen_at=first_seen,
        last_seen_at=NOW,
        last_changed_at=first_seen,
        is_active=active,
        observation_count=observation_count,
    )
    session.add(offer)
    session.flush()
    return offer


def recent(session: Session, hours: int = 48, kind: str = "all"):
    return list_recent_commercial_leads(
        session,
        RecentCommercialLeadQuery(window_hours=hours, kind=kind),
        now=NOW,
    )


def test_new_company_in_24_hours_is_distinguished_from_old_company_new_offer(session):
    add_offer(session, "new-company", "NOUVELLE", first_seen_hours_ago=12)
    add_offer(session, "old", "CONNUE", first_seen_hours_ago=24 * 30, title="Comptable")
    add_offer(session, "new-need", "CONNUE", first_seen_hours_ago=20, title="Commercial")
    session.commit()

    page = recent(session, 24)
    by_name = {item.lead.company_name: item for item in page.items}

    assert by_name["NOUVELLE"].is_new_company_in_window is True
    assert by_name["CONNUE"].is_new_company_in_window is False
    assert by_name["CONNUE"].new_offer_count_in_window == 1
    assert [item.lead.company_name for item in recent(session, 24, "new_companies").items] == ["NOUVELLE"]
    assert [item.lead.company_name for item in recent(session, 24, "new_offers").items] == ["CONNUE"]


def test_old_company_without_new_canonical_offer_is_absent_even_when_seen_again(session):
    add_offer(
        session, "observed-again", "ANCIENNE", first_seen_hours_ago=24 * 20,
        observation_count=9,
    )
    session.commit()

    assert recent(session, 48).items == ()


def test_old_company_with_offer_detected_30_hours_ago_enters_only_48_hour_window(session):
    add_offer(session, "historic", "CONNUE 48H", first_seen_hours_ago=24 * 30, title="Comptable")
    add_offer(session, "recent", "CONNUE 48H", first_seen_hours_ago=30, title="Commercial")
    session.commit()

    assert recent(session, 24).total_count == 0
    item = recent(session, 48).items[0]
    assert item.lead.company_name == "CONNUE 48H"
    assert item.is_new_company_in_window is False
    assert item.new_offer_count_in_window == 1


def test_cross_source_same_need_counts_once_and_keeps_all_provenance(session):
    add_offer(
        session, "ft", "MULTI SOURCE", first_seen_hours_ago=15,
        source="france_travail", title="Auxiliaire de vie H/F",
    )
    add_offer(
        session, "linkedin", "MULTI SOURCE", first_seen_hours_ago=2,
        source="linkedin", title="Auxiliaire de vie",
    )
    session.commit()

    item = recent(session, 24).items[0]

    assert item.new_offer_count_in_window == 1
    assert item.latest_new_opportunity_at == NOW - timedelta(hours=15)
    assert item.lead.active_offer_count == 1
    assert len(item.lead.active_job_offers[0].evidence) == 2
    assert item.lead.active_job_offers[0].sources == ("france_travail", "linkedin")


def test_two_distinct_new_needs_count_twice_and_distinct_repost_can_surface(session):
    add_offer(session, "old", "REPOST", first_seen_hours_ago=24 * 30, title="Comptable")
    add_offer(session, "repost", "REPOST", first_seen_hours_ago=10, title="Comptable")
    add_offer(session, "other", "REPOST", first_seen_hours_ago=5, title="Responsable RH")
    session.commit()

    item = recent(session, 24).items[0]

    assert item.new_offer_count_in_window == 2
    assert set(item.new_offer_ids_in_window) == {
        "france_travail:repost", "france_travail:other",
    }


def test_supported_windows_filter_on_first_detection(session):
    add_offer(session, "boundary", "FENETRE", first_seen_hours_ago=30)
    session.commit()

    assert recent(session, 24).total_count == 0
    assert recent(session, 48).total_count == 1
    assert recent(session, 168).total_count == 1
    assert recent(session, 720).total_count == 1


def test_score_is_primary_sort_and_recency_breaks_equal_scores(session):
    add_offer(session, "high-old", "SCORE HAUT", first_seen_hours_ago=24 * 20, title="Comptable")
    add_offer(session, "high-new-a", "SCORE HAUT", first_seen_hours_ago=40, title="Commercial")
    add_offer(session, "high-new-b", "SCORE HAUT", first_seen_hours_ago=39, title="Responsable RH")
    add_offer(session, "low-new", "SCORE BAS", first_seen_hours_ago=2)
    add_offer(session, "tie-older", "EGAL A", first_seen_hours_ago=12)
    add_offer(session, "tie-newer", "EGAL B", first_seen_hours_ago=3)
    session.commit()

    items = recent(session, 48).items
    scores = [item.lead.scoring.total_score for item in items]
    assert scores == sorted(scores, reverse=True)
    assert [item.lead.company_name for item in items].index("SCORE HAUT") < [item.lead.company_name for item in items].index("SCORE BAS")
    assert next(item for item in items if item.lead.company_name == "EGAL B").lead.scoring.total_score == next(item for item in items if item.lead.company_name == "EGAL A").lead.scoring.total_score
    assert [item.lead.company_name for item in items].index("EGAL B") < [item.lead.company_name for item in items].index("EGAL A")


def test_exclusion_and_active_commercial_tracking_are_reused(session):
    add_offer(session, "eligible", "ELIGIBLE", first_seen_hours_ago=2)
    add_offer(session, "excluded", "EXCLUE", first_seen_hours_ago=2)
    add_offer(session, "followed", "SUIVIE", first_seen_hours_ago=2)
    add_offer(session, "client", "CLIENTE", first_seen_hours_ago=2)
    add_offer(session, "stop", "STOP", first_seen_hours_ago=2)
    session.add(CommercialExclusion(
        company_key="exclue", siren=None, company_name_snapshot="EXCLUE",
        exclusion_type=ExclusionType.CURRENT_CLIENT, starts_at=NOW - timedelta(days=1),
    ))
    session.add(CommercialRelationship(
        company_key="suivie", siren=None, company_name_snapshot="SUIVIE",
        status="contacted", is_active=True, created_at=NOW, updated_at=NOW,
    ))
    session.add(CommercialRelationship(
        company_key="cliente", siren=None, company_name_snapshot="CLIENTE",
        status="client", is_active=True, created_at=NOW, updated_at=NOW,
    ))
    session.add(CommercialRelationship(
        company_key="stop", siren=None, company_name_snapshot="STOP",
        status="do_not_contact", is_active=True, created_at=NOW, updated_at=NOW,
    ))
    session.commit()

    assert [item.lead.company_name for item in recent(session).items] == ["ELIGIBLE"]


def test_recent_read_does_not_change_historical_dashboard_result(session):
    add_offer(session, "old", "TABLEAU", first_seen_hours_ago=24 * 10, title="Comptable")
    add_offer(session, "new", "TABLEAU", first_seen_hours_ago=4, title="Commercial")
    add_offer(session, "other", "AUTRE", first_seen_hours_ago=24 * 5)
    session.commit()
    query = CommercialLeadQuery()

    before = list_commercial_leads(session, query, now=NOW)
    list_recent_commercial_leads(session, RecentCommercialLeadQuery(), now=NOW)
    after = list_commercial_leads(session, query, now=NOW)

    assert [(item.company_key, item.scoring.total_score) for item in before.items] == [
        (item.company_key, item.scoring.total_score) for item in after.items
    ]


def test_recent_api_contract_and_dashboard_contract_remain_separate(session):
    add_offer(session, "api", "API RECENTE", first_seen_hours_ago=1)
    session.commit()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            recent_response = client.get(
                "/api/v1/commercial-leads/recent",
                params={"window_hours": 48, "kind": "all"},
            )
            assert recent_response.status_code == 200, recent_response.text
            recent_body = recent_response.json()
            dashboard_body = client.get("/api/v1/commercial-leads").json()
    finally:
        app.dependency_overrides.clear()

    assert recent_body["window_hours"] == 48
    assert recent_body["items"][0]["new_offer_count_in_window"] == 1
    assert len(recent_body["items"][0]["active_job_offers"][0]["evidence"]) == 1
    assert "new_offer_count_in_window" not in dashboard_body["items"][0]


@pytest.mark.parametrize("window", [1, 25, 169, 721])
def test_recent_api_rejects_unsupported_manual_windows(session, window):
    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/commercial-leads/recent", params={"window_hours": window})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422
