"""Schedule -> cancel and schedule -> execute of a member erasure over HTTP (#1105).

The community stack does not serve ``/api/org-admin`` (the ``organizations`` capability is off and
the edition has no memberships), so the whole journey cannot run as a browser e2e there. This runs
it in-process on the real routes and database: the router, auth chain, ledger, executor and the
notices, with only the erasure's object-store work and the delivery channels replaced.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.config import settings
from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.services import audit_outbox
from app.services import gdpr_erasure_service
from app.services import scheduled_erasure_service as scheduled

ERASE = "/api/org-admin/gdpr/erase-user/{}"
LIST = "/api/org-admin/gdpr/erasures"
CANCEL = "/api/org-admin/gdpr/erasures/{}/cancel"
REAUTH = {"password": "password123"}

pytestmark = pytest.mark.usefixtures("organizations_capability_on")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(audit_outbox, "deliver_staged", lambda db: None)


def _record(into: list[tuple[int, dict]], item: tuple[int, dict]) -> bool:
    into.append(item)
    return True


@pytest.fixture
def frames(monkeypatch) -> list[tuple[int, dict]]:
    seen: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        "app.utils.websocket_notify.send_ws_event",
        lambda user_id, event, data: _record(seen, (user_id, data)),
    )
    monkeypatch.setattr("app.services.email_service.email_service.has_transport", lambda: False)
    return seen


@pytest.fixture
def erased(monkeypatch) -> list[tuple[int, int]]:
    """Records the member erasure the executor runs, in place of the object-store work."""
    ran: list[tuple[int, int]] = []

    def fake(db, user_id, org_id, **kwargs):
        ran.append((user_id, org_id))
        return {"complete": True}

    monkeypatch.setattr(gdpr_erasure_service, "erase_org_member_data", fake)
    return ran


@pytest.fixture
def org(db_session, normal_user, other_user, org_context, user_token_headers):
    row = Organization(
        external_org_id=f"org_flow_{uuid_pkg.uuid4().hex[:8]}", name="Flow", is_active=True
    )
    db_session.add(row)
    db_session.commit()
    for user, role in ((normal_user, "org:admin"), (other_user, "org:member")):
        db_session.add(OrganizationMembership(organization_id=row.id, user_id=user.id, role=role))
    db_session.commit()
    org_context(org_id=row.id, org_role="org:admin", only_for=normal_user.id)
    return row


def _request(client, headers, member) -> dict:
    response = client.post(ERASE.format(member.uuid), headers=headers, json=REAUTH)
    assert response.status_code == 202, response.text
    receipt: dict = response.json()
    return receipt


def test_schedule_then_cancel_deletes_nothing_and_tells_both_people(
    client, user_token_headers, org, other_user, normal_user, frames, erased
):
    receipt = _request(client, user_token_headers, other_user)
    assert receipt["status"] == "scheduled"
    assert [e["ledger_uuid"] for e in client.get(LIST, headers=user_token_headers).json()] == [
        receipt["ledger_uuid"]
    ]

    cancelled = client.post(CANCEL.format(receipt["ledger_uuid"]), headers=user_token_headers)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert client.get(LIST, headers=user_token_headers).json() == []
    assert erased == []
    events = sorted((uid, d["event"]) for uid, d in frames)
    assert events == sorted(
        [(normal_user.id, "requested"), (other_user.id, "requested")]
        + [(normal_user.id, "cancelled"), (other_user.id, "cancelled")]
    )


def test_schedule_then_execute_runs_the_member_erasure_and_reports_completion(
    client, db_session, user_token_headers, org, other_user, normal_user, frames, erased
):
    receipt = _request(client, user_token_headers, other_user)
    entry = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.uuid == uuid_pkg.UUID(receipt["ledger_uuid"]))
        .one()
    )
    assert erased == []  # nothing runs at request time
    due = entry.scheduled_for + timedelta(minutes=1)
    assert due > datetime.now(UTC)
    frames.clear()

    assert scheduled._execute_one(db_session, entry.id, due) is True

    assert erased == [(other_user.id, org.id)]
    assert sorted((uid, d["event"]) for uid, d in frames) == sorted(
        [(normal_user.id, "completed"), (other_user.id, "completed")]
    )
