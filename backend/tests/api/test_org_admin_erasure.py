"""Scheduled organization and member erasure over HTTP (#1105).

``POST /api/org-admin/gdpr/erase-user/{uuid}`` and ``/erase-organization`` no longer erase: they
re-authenticate the caller and schedule the erasure (202). ``GET /api/org-admin/gdpr/erasures``
lists the organization's open ones and ``POST .../erasures/{uuid}/cancel`` withdraws one before
it runs. The confirm gate, the membership 403 and the capability 404 are in
``api/endpoints/test_org_admin_endpoints.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.services import erasure_ledger_service as ledger

ERASE_ORG = "/api/org-admin/gdpr/erase-organization?confirm=true"
LIST = "/api/org-admin/gdpr/erasures"
SETTINGS = "/api/admin/self-erasure"
REAUTH = {"password": "password123"}

pytestmark = pytest.mark.usefixtures("organizations_capability_on")


def _erase_user(user_uuid) -> str:
    return f"/api/org-admin/gdpr/erase-user/{user_uuid}"


def _cancel(ledger_uuid) -> str:
    return f"/api/org-admin/gdpr/erasures/{ledger_uuid}/cancel"


@pytest.fixture(autouse=True)
def _journal(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


def _org(db, label: str) -> Organization:
    row = Organization(
        external_org_id=f"org_sched_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"Sched {label}",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def org(db_session) -> Organization:
    return _org(db_session, "mine")


@pytest.fixture
def admin_headers(org, normal_user, user_token_headers, org_context, db_session):
    """``normal_user`` is an ``org:admin`` of ``org``; ``other_user`` a plain member."""
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=normal_user.id, role="org:admin")
    )
    db_session.commit()
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    return user_token_headers


@pytest.fixture
def member(org, other_user, db_session):
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=other_user.id, role="org:member")
    )
    db_session.commit()
    return other_user


def _open_entries(db, org_id: int) -> list[ErasureLedgerEntry]:
    db.expire_all()
    return list(
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_organization_id == org_id,
            ErasureLedgerEntry.status == "deferred",
            ErasureLedgerEntry.deferred_reason == "grace_period",
        )
        .all()
    )


# --- requesting --------------------------------------------------------------------------------


def test_a_member_erasure_is_scheduled_72_hours_out_and_nothing_is_erased(
    client, db_session, org, member, admin_headers, monkeypatch
):
    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("nothing may be erased at request time")

    monkeypatch.setattr("app.services.gdpr_erasure_service.erase_org_member_data", _must_not_run)

    response = client.post(_erase_user(member.uuid), headers=admin_headers, json=REAUTH)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["subject_type"] == "org_member"
    assert body["subject_user_uuid"] == str(member.uuid)
    requested = datetime.fromisoformat(body["requested_at"])
    scheduled_for = datetime.fromisoformat(body["scheduled_for"])
    assert scheduled_for - requested == timedelta(hours=72)
    [entry] = _open_entries(db_session, org.id)
    assert (entry.subject_type, entry.subject_user_id, entry.actor_kind) == (
        "org_member",
        member.id,
        "org_admin",
    )


@pytest.mark.parametrize("body", [None, {}, {"password": "wrong-password"}, {"mfa_code": "000000"}])
def test_without_a_valid_reauth_nothing_is_scheduled(
    client, db_session, org, member, admin_headers, body
):
    kwargs = {"json": body} if body is not None else {}
    response = client.post(_erase_user(member.uuid), headers=admin_headers, **kwargs)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "reauth_required"
    assert _open_entries(db_session, org.id) == []


def test_an_account_that_cannot_reauthenticate_gets_409(
    client, db_session, org, normal_user, admin_headers
):
    normal_user.auth_type = "oidc"
    normal_user.allow_local_fallback = False
    db_session.commit()

    response = client.post(ERASE_ORG, headers=admin_headers, json=REAUTH)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "reauth_unavailable"
    assert _open_entries(db_session, org.id) == []


def test_a_second_request_for_the_same_subject_is_409(client, db_session, org, admin_headers):
    assert client.post(ERASE_ORG, headers=admin_headers, json=REAUTH).status_code == 202

    response = client.post(ERASE_ORG, headers=admin_headers, json=REAUTH)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "request_exists"
    assert len(_open_entries(db_session, org.id)) == 1


# --- listing and cancelling --------------------------------------------------------------------


def test_the_list_shows_this_organizations_open_erasures_and_who_asked(
    client, db_session, org, member, admin_headers, normal_user
):
    client.post(_erase_user(member.uuid), headers=admin_headers, json=REAUTH)
    other = _org(db_session, "theirs")
    ledger.record_request(
        db_session,
        subject_type="organization",
        subject_organization_id=other.id,
        subject_organization_uuid=other.uuid,
        actor_kind="org_admin",
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=datetime.now(UTC) + timedelta(hours=72),
    )

    response = client.get(LIST, headers=admin_headers)

    assert response.status_code == 200, response.text
    [item] = response.json()
    assert item["subject_type"] == "org_member"
    assert item["subject_user_uuid"] == str(member.uuid)
    assert item["requested_by"] == (normal_user.full_name or normal_user.email)


def test_cancel_before_the_time_withdraws_it(client, db_session, org, member, admin_headers):
    scheduled = client.post(_erase_user(member.uuid), headers=admin_headers, json=REAUTH).json()

    response = client.post(_cancel(scheduled["ledger_uuid"]), headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert _open_entries(db_session, org.id) == []
    assert client.get(LIST, headers=admin_headers).json() == []


def test_cancel_after_the_time_is_409_and_the_entry_stays_due(
    client, db_session, org, member, admin_headers
):
    entry = ledger.record_request(
        db_session,
        subject_type="org_member",
        subject_user_id=member.id,
        subject_user_uuid=member.uuid,
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
        actor_kind="org_admin",
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert entry is not None

    response = client.post(_cancel(entry.uuid), headers=admin_headers)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "already_executing"
    assert [e.id for e in _open_entries(db_session, org.id)] == [entry.id]


def test_another_organizations_entry_and_a_self_request_are_404(
    client, db_session, admin_headers, other_user
):
    other = _org(db_session, "foreign")
    foreign = ledger.record_request(
        db_session,
        subject_type="organization",
        subject_organization_id=other.id,
        subject_organization_uuid=other.uuid,
        actor_kind="org_admin",
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=datetime.now(UTC) + timedelta(hours=72),
    )
    own_request = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=other_user.id,
        subject_user_uuid=other_user.uuid,
        actor_kind="data_subject",
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=datetime.now(UTC) + timedelta(days=14),
    )
    assert foreign is not None and own_request is not None

    for entry in (foreign, own_request):
        response = client.post(_cancel(entry.uuid), headers=admin_headers)
        assert response.status_code == 404, response.text
        db_session.refresh(entry)
        assert entry.status == "deferred"
    assert client.post(_cancel(uuid_pkg.uuid4()), headers=admin_headers).status_code == 404
    # Control: the same caller CAN cancel its own organization's entry, so the 404s above are
    # the scope rule and not a missing route.
    mine = client.post(ERASE_ORG, headers=admin_headers, json=REAUTH).json()
    assert client.post(_cancel(mine["ledger_uuid"]), headers=admin_headers).status_code == 200


@pytest.mark.parametrize("method_path", [("get", LIST), ("post", _cancel(uuid_pkg.uuid4()))])
def test_a_plain_member_can_neither_list_nor_cancel(
    client, org, normal_user, user_token_headers, org_context, method_path
):
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
    method, path = method_path

    response = getattr(client, method)(path, headers=user_token_headers)

    assert response.status_code == 403, response.text


# --- the delay settings (super_admin) ----------------------------------------------------------


def test_the_delays_are_super_admin_settings_inside_their_range(client, super_admin_token_headers):
    current = client.get(SETTINGS, headers=super_admin_token_headers).json()
    assert current["org_erasure_delay_hours"] == 72
    assert current["org_member_erasure_delay_hours"] == 72
    assert (current["min_org_erasure_delay_hours"], current["max_org_erasure_delay_hours"]) == (
        24,
        168,
    )

    updated = client.put(
        SETTINGS, headers=super_admin_token_headers, json={"org_member_erasure_delay_hours": 24}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["org_member_erasure_delay_hours"] == 24
    assert updated.json()["org_erasure_delay_hours"] == 72

    too_long = client.put(
        SETTINGS, headers=super_admin_token_headers, json={"org_erasure_delay_hours": 169}
    )
    assert too_long.status_code == 422, too_long.text


def test_the_delay_setting_is_what_the_next_request_waits(
    client, db_session, org, member, admin_headers, super_admin_token_headers
):
    client.put(
        SETTINGS, headers=super_admin_token_headers, json={"org_member_erasure_delay_hours": 30}
    )

    body = client.post(_erase_user(member.uuid), headers=admin_headers, json=REAUTH).json()

    gap = datetime.fromisoformat(body["scheduled_for"]) - datetime.fromisoformat(
        body["requested_at"]
    )
    assert gap == timedelta(hours=30)


# --- the audit trail ---------------------------------------------------------------------------


def test_schedule_and_cancel_write_org_stamped_events(
    client, db_session, org, member, admin_headers, normal_user, monkeypatch
):
    """Both go through the outbox in the ledger change's transaction, stamped with the
    organization so its admins read them in ``GET /org-admin/audit-logs``."""
    from sqlalchemy import text

    from app.core.config import settings
    from app.services import audit_outbox

    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(audit_outbox, "deliver_staged", lambda db: None)
    scheduled = client.post(_erase_user(member.uuid), headers=admin_headers, json=REAUTH).json()
    client.post(_cancel(scheduled["ledger_uuid"]), headers=admin_headers)

    rows = db_session.execute(
        text(
            "SELECT event_type, (payload->>'user_id')::int, (payload->>'target_user_id')::int, "
            "(payload->>'organization_id')::int FROM audit_outbox "
            "WHERE payload->>'resource_uuid' = :u ORDER BY id"
        ),
        {"u": scheduled["ledger_uuid"]},
    ).all()
    assert [tuple(r) for r in rows] == [
        ("gdpr.erasure.scheduled", normal_user.id, member.id, org.id),
        ("gdpr.erasure.cancelled", normal_user.id, member.id, org.id),
    ]
