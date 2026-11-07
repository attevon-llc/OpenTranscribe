"""Organization owner (v468, #1105): only the owner schedules the organization's erasure, and the
owner can hand the role to another administrator of the same organization.

Request mechanics of the erasure itself (re-auth, delay, cancel) are in
``test_org_admin_erasure.py``; this file is about WHO may.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.auth.audit_events import AuditEventType
from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.organization import OrganizationMembership

ERASE_ORG = "/api/org-admin/gdpr/erase-organization?confirm=true"
TRANSFER = "/api/org-admin/ownership/transfer"
REAUTH = {"password": "password123"}

pytestmark = pytest.mark.usefixtures("organizations_capability_on")


@pytest.fixture(autouse=True)
def _journal(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


def _org(db, label: str) -> Organization:
    row = Organization(
        external_org_id=f"org_own_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"Owner {label}",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _join(db, org, user, role: str) -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db.commit()


@pytest.fixture
def org(db_session) -> Organization:
    return _org(db_session, "mine")


@pytest.fixture
def as_admin(org, normal_user, user_token_headers, org_context, db_session):
    """``normal_user`` is an ``org:admin`` of ``org``; the owner is set per test."""
    _join(db_session, org, normal_user, "org:admin")
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    return user_token_headers


def _own(db, org, user) -> None:
    org.owner_user_id = user.id
    db.commit()


def _open_org_erasures(db, org_id: int) -> int:
    db.expire_all()
    count: int = (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_organization_id == org_id,
            ErasureLedgerEntry.subject_type == "organization",
        )
        .count()
    )
    return count


# --- who may schedule the erasure of the organization ---------------------------------------


def test_an_admin_who_is_not_the_owner_gets_403_and_nothing_is_scheduled(
    client, db_session, org, as_admin, other_user
):
    _join(db_session, org, other_user, "org:admin")
    _own(db_session, org, other_user)

    response = client.post(ERASE_ORG, headers=as_admin, json=REAUTH)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "org_owner_only"
    assert _open_org_erasures(db_session, org.id) == 0


def test_the_owner_with_reauth_gets_202(client, db_session, org, as_admin, normal_user):
    _own(db_session, org, normal_user)

    response = client.post(ERASE_ORG, headers=as_admin, json=REAUTH)

    assert response.status_code == 202, response.text
    assert _open_org_erasures(db_session, org.id) == 1


def test_the_owner_still_needs_reauth(client, db_session, org, as_admin, normal_user):
    _own(db_session, org, normal_user)

    response = client.post(ERASE_ORG, headers=as_admin, json={"password": "wrong-password"})

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "reauth_required"
    assert _open_org_erasures(db_session, org.id) == 0


def test_an_organization_without_an_owner_gets_409_org_owner_required(
    client, db_session, org, as_admin
):
    assert org.owner_user_id is None

    response = client.post(ERASE_ORG, headers=as_admin, json=REAUTH)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "org_owner_required"
    assert _open_org_erasures(db_session, org.id) == 0


def test_a_member_erasure_stays_open_to_any_org_admin(
    client, db_session, org, as_admin, other_user
):
    _join(db_session, org, other_user, "org:member")

    response = client.post(
        f"/api/org-admin/gdpr/erase-user/{other_user.uuid}", headers=as_admin, json=REAUTH
    )

    assert response.status_code == 202, response.text


def test_the_owner_losing_their_account_leaves_the_organization_ownerless(
    db_session, org, other_user
):
    from sqlalchemy import text

    _join(db_session, org, other_user, "org:admin")
    _own(db_session, org, other_user)

    db_session.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": other_user.id})
    db_session.expire_all()

    assert db_session.get(Organization, org.id).owner_user_id is None


# --- handing the organization over ----------------------------------------------------------


def test_the_owner_hands_the_organization_to_another_admin(
    client, db_session, org, as_admin, normal_user, other_user
):
    _join(db_session, org, other_user, "org:admin")
    _own(db_session, org, normal_user)

    response = client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(other_user.uuid)})

    assert response.status_code == 200, response.text
    assert response.json()["owner_user_uuid"] == str(other_user.uuid)
    db_session.expire_all()
    assert db_session.get(Organization, org.id).owner_user_id == other_user.id
    # The former owner can no longer schedule the erasure.
    again = client.post(ERASE_ORG, headers=as_admin, json=REAUTH)
    assert again.status_code == 403, again.text


def test_the_hand_over_is_audited(
    client, db_session, org, as_admin, normal_user, other_user, monkeypatch
):
    staged: list[dict] = []

    def _capture(_db, **kw):
        staged.append(kw)

    monkeypatch.setattr("app.services.audit_outbox.stage", _capture)
    _join(db_session, org, other_user, "org:admin")
    _own(db_session, org, normal_user)

    client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(other_user.uuid)})

    [event] = staged
    assert event["event_type"] is AuditEventType.ORG_OWNER_CHANGE
    assert event["user_id"] == normal_user.id
    assert event["target_user_id"] == other_user.id
    assert event["organization_id"] == org.id
    assert event["details"]["new_owner_uuid"] == str(other_user.uuid)
    assert event["details"]["previous_owner_uuid"] == str(normal_user.uuid)


def test_a_transfer_to_a_plain_member_is_refused(
    client, db_session, org, as_admin, normal_user, other_user
):
    _join(db_session, org, other_user, "org:member")
    _own(db_session, org, normal_user)

    response = client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(other_user.uuid)})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "not_an_org_admin"
    db_session.expire_all()
    assert db_session.get(Organization, org.id).owner_user_id == normal_user.id


def test_a_transfer_to_an_admin_of_another_organization_is_refused(
    client, db_session, org, as_admin, normal_user, other_user
):
    elsewhere = _org(db_session, "elsewhere")
    _join(db_session, elsewhere, other_user, "org:admin")
    _own(db_session, org, normal_user)

    response = client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(other_user.uuid)})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "not_an_org_admin"
    db_session.expire_all()
    assert db_session.get(Organization, org.id).owner_user_id == normal_user.id


def test_a_transfer_to_an_unknown_user_is_the_same_refusal(
    client, db_session, org, as_admin, normal_user
):
    _own(db_session, org, normal_user)

    response = client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(uuid_pkg.uuid4())})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "not_an_org_admin"


def test_only_the_owner_can_transfer(client, db_session, org, as_admin, normal_user, other_user):
    _join(db_session, org, other_user, "org:admin")
    _own(db_session, org, other_user)

    response = client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(normal_user.uuid)})

    assert response.status_code == 403, response.text
    db_session.expire_all()
    assert db_session.get(Organization, org.id).owner_user_id == other_user.id


def test_a_transfer_to_the_current_owner_is_refused(client, db_session, org, as_admin, normal_user):
    _own(db_session, org, normal_user)

    response = client.post(TRANSFER, headers=as_admin, json={"to_user_uuid": str(normal_user.uuid)})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "already_owner"


def test_the_operator_can_name_an_owner_where_there_is_none(db_session, org, normal_user):
    from app.services import organization_owner

    _join(db_session, org, normal_user, "org:admin")

    organization_owner.set_owner(
        db_session, organization_id=org.id, new_owner=normal_user, actor=None
    )

    db_session.expire_all()
    assert db_session.get(Organization, org.id).owner_user_id == normal_user.id
