"""End-to-end synthetic coverage for the commercial follow-up loop."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models import CommercialInteraction, CommercialRelationship, ContactPoint, ObservedJobOffer
from test_commercial_angle import configured
from test_commercial_relationships_api import NOW, client, seed_lead, session  # noqa: F401


def interaction(outcome="conversation", **overrides):
    value = {
        "request_id": str(uuid4()), "company_key": "acme sas",
        "happened_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "channel": "phone", "outcome": outcome, "next_action_mode": "preserve",
    }
    value.update(overrides)
    return value


def add_offer(session, offer_id, title, *, first_seen_at=None, active=True):
    observed = first_seen_at or datetime.now(timezone.utc)
    session.add(ObservedJobOffer(
        source="france_travail", source_offer_id=offer_id, title=title,
        company_name="ACME SAS", location_label="Créteil", commune="94028",
        department_code="94", created_at=observed.isoformat(),
        source_url=f"https://source.test/{offer_id}", first_seen_at=observed,
        last_seen_at=observed, last_changed_at=observed, is_active=active,
        observation_count=1,
    ))
    session.commit()


def test_next_action_preserve_replace_and_clear(client, session):
    configured(session); seed_lead(session)
    due = datetime.now(timezone.utc) + timedelta(days=2)
    created = client.post("/api/v1/commercial-interactions", json=interaction(
        "callback_requested", next_action_mode="replace", next_action="Rappeler",
        next_action_at=due.isoformat(),
    ))
    assert created.status_code == 201, created.text
    preserved = client.post("/api/v1/commercial-interactions", json=interaction("conversation"))
    assert preserved.json()["relationship"]["next_action"] == "Rappeler"
    assert preserved.json()["relationship"]["next_action_at"] is not None
    replaced = client.post("/api/v1/commercial-interactions", json=interaction(
        "conversation", next_action_mode="replace", next_action="Envoyer un email",
    ))
    assert replaced.json()["relationship"]["next_action"] == "Envoyer un email"
    cleared = client.post("/api/v1/commercial-interactions", json=interaction(
        "conversation", next_action_mode="clear",
    ))
    assert cleared.json()["relationship"]["next_action"] is None
    assert cleared.json()["relationship"]["next_action_at"] is None


def test_antedated_event_stays_in_history_without_corrupting_current_state(client, session):
    configured(session); seed_lead(session)
    recent_at = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = client.post("/api/v1/commercial-interactions", json=interaction(
        "interested", happened_at=recent_at.isoformat(),
    ))
    old = client.post("/api/v1/commercial-interactions", json=interaction(
        "refused", happened_at=(recent_at - timedelta(days=5)).isoformat(),
    ))
    assert recent.status_code == old.status_code == 201
    assert old.json()["interaction"]["applied_to_current_state"] is False
    assert old.json()["relationship"]["status"] == "interested"
    assert [item["outcome"] for item in client.get(
        "/api/v1/commercial-interactions/acme%20sas"
    ).json()["items"]] == ["interested", "refused"]


def test_inactive_offer_keeps_dossier_and_accepts_real_interaction(client, session):
    configured(session); seed_lead(session)
    first = client.post("/api/v1/commercial-interactions", json=interaction("conversation"))
    need_id = first.json()["interaction"]["need_id"]
    relationship_id = first.json()["relationship"]["id"]
    session.query(ObservedJobOffer).update({ObservedJobOffer.is_active: False})
    session.commit()
    dossier = client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier")
    assert dossier.status_code == 200, dossier.text
    assert dossier.json()["active_lead"] is None
    assert dossier.json()["needs"][0]["active"] is False
    assert dossier.json()["communication_status"] == "historical_only"
    again = client.post("/api/v1/commercial-interactions", json=interaction(
        "conversation", need_id=need_id, note="Rappel entrant synthétique",
    ))
    assert again.status_code == 201, again.text
    assert again.json()["interaction"]["job_title"] == "Technicien"


def test_exchange_context_is_scoped_to_the_exact_need(client, session):
    configured(session); seed_lead(session)
    first = client.post("/api/v1/commercial-interactions", json=interaction(
        "email_requested", priority_expressed="autonomie diagnostic",
    ))
    assert first.status_code == 201
    add_offer(session, "commercial-b2b", "Commercial B2B")
    other_need = "france_travail:commercial-b2b"
    pack = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack", params={"need_id": other_need})
    assert pack.status_code == 200, pack.text
    requested = next(item for item in pack.json()["email"] if item["type"] == "email_requested_after_call")
    assert requested["body"] is None
    assert all("autonomie diagnostic" not in (item.get("meeting_transition_after_response") or "") for item in pack.json()["phone"])


def test_history_survives_company_key_change_when_siren_is_stable(client, session):
    configured(session); seed_lead(session)
    created = client.post("/api/v1/commercial-interactions", json=interaction("conversation"))
    relationship = session.get(CommercialRelationship, created.json()["relationship"]["id"])
    event = session.query(CommercialInteraction).one()
    relationship.siren = event.siren = "123456789"
    relationship.company_key = "acme nouvelle denomination"
    relationship.company_name_snapshot = "ACME NOUVELLE DENOMINATION"
    session.commit()
    dossier = client.get(f"/api/v1/commercial-relationships/{relationship.id}/dossier").json()
    assert dossier["needs"][0]["title"] == "Technicien"


def test_new_need_is_signaled_without_reopening_cold_opportunity(client, session):
    configured(session); seed_lead(session)
    created = client.post("/api/v1/commercial-interactions", json=interaction("conversation"))
    relationship_id = created.json()["relationship"]["id"]
    add_offer(session, "new-need", "Commercial B2B", first_seen_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    dossier = client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier").json()
    assert any(item["title"] == "Commercial B2B" and item["newly_observed"] for item in dossier["needs"])
    assert client.get("/api/v1/commercial-leads/recent", params={"window_hours": 48}).json()["items"] == []


def test_human_contact_and_need_verification_are_scoped_and_auditable(client, session):
    configured(session); seed_lead(session)
    created = client.post("/api/v1/commercial-interactions", json=interaction("conversation"))
    relationship_id = created.json()["relationship"]["id"]
    need_id = created.json()["interaction"]["need_id"]
    contact = client.post(f"/api/v1/commercial-relationships/{relationship_id}/human-contacts", json={
        "channel_type": "phone", "value": "01 23 45 67 89", "person_name": "Mme Exemple",
        "role_title": "Recrutement", "verified_at": datetime.now(timezone.utc).isoformat(),
        "provenance": "switchboard",
    })
    assert contact.status_code == 201, contact.text
    point = session.get(ContactPoint, contact.json()["contact_point_id"])
    assert point.verification_status == "manually_verified"
    pack = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack").json()
    assert pack["recommended_contact"]["id"] == point.id
    assert pack["recommended_contact"]["verification_status"] == "manually_verified"
    verification = client.post(f"/api/v1/commercial-relationships/{relationship_id}/need-verifications", json={
        "need_id": need_id, "verified_at": datetime.now(timezone.utc).isoformat(),
        "channel": "phone", "note": "Besoin confirmé synthétiquement",
    })
    assert verification.status_code == 201, verification.text
    assert verification.json()["scope"] == "need_exists"


def test_human_need_confirmation_only_lifts_offer_freshness_block(client, session):
    configured(session); seed_lead(session)
    relationship = client.post("/api/v1/commercial-relationships/from-lead", json={
        "company_key": "acme sas", "status": "contacted",
    }).json()
    offer = session.query(ObservedJobOffer).one()
    offer.last_seen_at = datetime.now(timezone.utc) - timedelta(days=12)
    session.commit()
    before = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack").json()
    assert before["status"] == "verify_offer"
    need_id = f"{before['entry_offer']['source']}:{before['entry_offer']['offer_id']}"
    verified = client.post(f"/api/v1/commercial-relationships/{relationship['id']}/need-verifications", json={
        "need_id": need_id, "verified_at": datetime.now(timezone.utc).isoformat(),
        "channel": "phone", "note": "Confirmation synthétique",
    })
    assert verified.status_code == 201, verified.text
    after = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack").json()
    assert after["status"] != "verify_offer"
    assert after["commercial_angle"]["readiness"] not in {"verify_employer", "intermediary_not_employer"}


def test_position_filled_is_scoped_to_one_need_and_planning_view_is_deterministic(client, session):
    configured(session); seed_lead(session); add_offer(session, "other-role", "Commercial B2B")
    filled = client.post("/api/v1/commercial-interactions", json=interaction(
        "position_filled", need_id="france_travail:offer-ACME SAS",
    ))
    assert filled.status_code == 201, filled.text
    assert filled.json()["interaction"]["need_status"] == "filled"
    assert filled.json()["relationship"]["status"] == "contacted"
    planning = client.get("/api/v1/commercial-relationships", params={"view": "planning"}).json()
    assert [item["id"] for item in planning["items"]] == [filled.json()["relationship"]["id"]]


def test_do_not_contact_dossier_remains_readable_and_cannot_be_reactivated_by_event(client, session):
    configured(session); seed_lead(session)
    created = client.post("/api/v1/commercial-interactions", json=interaction("do_not_contact"))
    relationship_id = created.json()["relationship"]["id"]
    dossier = client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier")
    assert dossier.status_code == 200
    assert dossier.json()["can_prepare_new_outreach"] is False
    assert dossier.json()["can_record_interaction"] is False
    event = client.post("/api/v1/commercial-interactions", json=interaction("conversation"))
    assert event.status_code == 201
    assert event.json()["interaction"]["applied_to_current_state"] is False
    assert event.json()["relationship"]["status"] == "do_not_contact"


def test_complete_day_one_to_client_follow_up_loop(client, session):
    configured(session); seed_lead(session)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    day_one = client.post("/api/v1/commercial-interactions", json=interaction(
        "callback_requested", next_action_mode="replace", next_action="Rappeler",
        next_action_at=tomorrow.isoformat(),
    ))
    assert day_one.status_code == 201, day_one.text
    relationship_id = day_one.json()["relationship"]["id"]
    old_need_id = day_one.json()["interaction"]["need_id"]
    assert client.get("/api/v1/commercial-leads").json()["items"] == []
    assert client.get("/api/v1/commercial-relationships", params={"view": "follow_up"}).json()["total"] == 1
    assert client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier").json()["active_lead"] is not None

    day_two = client.post("/api/v1/commercial-interactions", json=interaction("conversation", need_id=old_need_id))
    assert day_two.status_code == 201
    session.query(ObservedJobOffer).update({ObservedJobOffer.is_active: False})
    session.commit()
    historical = client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier").json()
    assert historical["needs"][0]["active"] is False
    inbound = client.post("/api/v1/commercial-interactions", json=interaction(
        "conversation", need_id=old_need_id, note="Échange historique synthétique",
    ))
    assert inbound.status_code == 201

    add_offer(session, "new-commercial", "Commercial B2B")
    new_need_id = "france_travail:new-commercial"
    dossier = client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier").json()
    assert any(item["need_id"] == new_need_id and item["newly_observed"] for item in dossier["needs"])
    new_pack = client.get("/api/v1/commercial-leads/acme%20sas/approach-pack", params={"need_id": new_need_id}).json()
    assert next(item for item in new_pack["email"] if item["type"] == "email_requested_after_call")["body"] is None

    client_result = client.post("/api/v1/commercial-interactions", json=interaction(
        "client", need_id=new_need_id,
    ))
    assert client_result.status_code == 201
    assert client_result.json()["relationship"]["status"] == "client"
    assert client_result.json()["relationship"]["hard_exclusion_id"] is not None
    assert client.get(f"/api/v1/commercial-relationships/{relationship_id}/dossier").status_code == 200
    history = client.get("/api/v1/commercial-interactions/acme%20sas").json()["items"]
    assert {item["need_id"] for item in history} == {old_need_id, new_need_id}
