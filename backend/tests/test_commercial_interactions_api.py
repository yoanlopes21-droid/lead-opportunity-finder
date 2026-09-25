"""Synthetic interaction scenarios; no local prospect records are modified."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.main import app  # noqa: F401 - initialize the established API import order
from app.models import CommercialExclusion, CommercialInteraction, CommercialRelationship
from app.services.commercial_interactions import OUTCOME_STATUSES, status_for_outcome
from test_commercial_angle import configured
from test_commercial_relationships_api import client, seed_lead, session  # noqa: F401


def body(outcome="wrong_contact", **overrides):
    value = {"request_id": str(uuid4()), "company_key": "acme sas",
             "happened_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
             "channel": "phone", "outcome": outcome}
    value.update(overrides)
    return value


def test_mapping_is_explicit_and_complete():
    assert len(OUTCOME_STATUSES) == 15
    for outcome, status in OUTCOME_STATUSES.items():
        assert status_for_outcome(outcome) == status
    assert status_for_outcome("position_filled") == "no_current_need"
    with pytest.raises(ValueError):
        status_for_outcome("invented")


def test_record_history_idempotency_and_reopen(client, session):
    configured(session); seed_lead(session)
    request = body(note="Échange synthétique", contacted_person="Accueil", job_title="Titre inventé")
    created = client.post("/api/v1/commercial-interactions", json=request)
    assert created.status_code == 201, created.text
    assert created.json()["relationship"]["status"] == "wrong_contact"
    assert created.json()["relationship"]["hard_exclusion_id"] is None
    assert created.json()["interaction"]["job_title"] == "Technicien"
    assert client.post("/api/v1/commercial-interactions", json=request).json()["interaction"]["id"] == created.json()["interaction"]["id"]
    assert session.query(CommercialInteraction).count() == 1
    assert client.get("/api/v1/commercial-leads").json()["items"] == []
    relationship_id = created.json()["relationship"]["id"]
    assert client.post(f"/api/v1/commercial-relationships/{relationship_id}/reopen-opportunity").status_code == 204
    assert client.get("/api/v1/commercial-interactions/acme%20sas").json()["items"][0]["outcome"] == "wrong_contact"


@pytest.mark.parametrize("outcome,status", [
    ("no_answer", "contacted"), ("callback_requested", "follow_up"),
    ("email_requested", "awaiting_reply"), ("interested", "interested"),
    ("refused", "refused"), ("position_filled", "no_current_need"),
    ("client", "client"), ("do_not_contact", "do_not_contact"),
])
def test_real_outcomes_update_existing_status(client, session, outcome, status):
    configured(session); seed_lead(session)
    next_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    result = client.post("/api/v1/commercial-interactions", json=body(outcome,
        next_action="Rappeler" if outcome == "callback_requested" else None,
        next_action_at=next_at if outcome == "callback_requested" else None))
    assert result.status_code == 201, result.text
    assert result.json()["relationship"]["status"] == status
    assert bool(result.json()["relationship"]["hard_exclusion_id"]) == (outcome in {"client", "do_not_contact"})
    assert session.query(CommercialExclusion).count() == int(outcome in {"client", "do_not_contact"})
    if outcome == "callback_requested":
        assert client.get("/api/v1/commercial-relationships?view=follow_up").json()["items"][0]["next_action"] == "Rappeler"


def test_email_request_and_priority_unlock_only_after_saved_event(client, session):
    configured(session); seed_lead(session)
    before = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack").json()
    assert next(item for item in before["email"] if item["type"] == "email_requested_after_call")["body"] is None
    created = client.post("/api/v1/commercial-interactions", json=body("email_requested",
        priority_expressed="un profil autonome sur diagnostic"))
    assert created.status_code == 201, created.text
    after = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack").json()
    assert "Comme demandé" in next(item for item in after["email"] if item["type"] == "email_requested_after_call")["body"]
    assert "profil autonome" in after["phone"][0]["meeting_transition_after_response"]


def test_validation_and_atomic_write(client, session):
    configured(session); seed_lead(session)
    for invalid in [body("meeting_scheduled"), body("no_answer", priority_expressed="Inventé"),
                    body("unknown"), body("wrong_contact", company_key="inconnue")]:
        response = client.post("/api/v1/commercial-interactions", json=invalid)
        assert response.status_code in {404, 422}, response.text
    assert session.scalar(select(CommercialInteraction)) is None
    assert session.scalar(select(CommercialRelationship)) is None


def test_blocked_pack_cannot_be_recorded_as_contact(client, session):
    seed_lead(session)
    response = client.post("/api/v1/commercial-interactions", json=body())
    assert response.status_code == 409
    assert session.scalar(select(CommercialInteraction)) is None


def test_undated_callback_appears_with_missing_date(client, session):
    configured(session); seed_lead(session)
    created = client.post("/api/v1/commercial-interactions", json=body("callback_requested"))
    assert created.status_code == 201
    followed = client.get("/api/v1/commercial-relationships?view=follow_up").json()["items"]
    assert followed[0]["status"] == "follow_up"
    assert followed[0]["next_action_at"] is None


def test_meeting_date_and_multiple_events_preserve_history(client, session):
    configured(session); seed_lead(session)
    first = client.post("/api/v1/commercial-interactions", json=body("conversation"))
    due = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    second = client.post("/api/v1/commercial-interactions", json=body("meeting_scheduled", next_action="Rendez-vous", next_action_at=due))
    assert first.status_code == second.status_code == 201
    assert second.json()["relationship"]["status"] == "meeting_scheduled"
    assert [item["outcome"] for item in client.get("/api/v1/commercial-interactions/acme%20sas").json()["items"]] == ["meeting_scheduled", "conversation"]
