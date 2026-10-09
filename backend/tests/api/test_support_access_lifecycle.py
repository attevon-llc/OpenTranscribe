"""Requesting, deciding, opening and revoking support-access grants (issue #1122).

The grant API in multi-tenant mode: who may ask, who may decide, what a decision can and
cannot change, and what each party is told. What a grant lets a request DO is
``test_support_access_enforcement.py``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.core.config import settings
from app.models.support_access import SupportAccessGrant
from app.services import support_access_lifecycle as lifecycle
from app.services.platform_access import reset_tenancy_mode_cache


def _code(response) -> str:
    return str(response.json()["detail"]["code"])


def _notified(world) -> list[tuple[int, str]]:
    return [(c.args[0], c.args[1]) for c in world.sends.call_args_list]


# ---------------------------------------------------------------------------
# Single-tenant mode: nothing to grant
# ---------------------------------------------------------------------------


@pytest.fixture
def community(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "community")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


def test_every_route_is_404_in_single_tenant_mode(client, admin_token_headers, community):
    """No organization rows: the surface does not exist, so a probe learns nothing."""
    for method, path in (
        ("get", "/api/support-access/grants"),
        ("get", "/api/support-access/targets/organizations"),
        ("get", "/api/users/me/support-access"),
        ("post", "/api/support-access/grants"),
    ):
        response = getattr(client, method)(path, headers=admin_token_headers)
        assert response.status_code == 404, (method, path)


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


def test_a_normal_user_cannot_request_access(support_world):
    w = support_world
    response = w.client.post(
        "/api/support-access/grants",
        headers=w.owner_headers,
        json={
            "organization_uuid": str(w.org.uuid),
            "access_level": "read",
            "reason": "Pretending to be support staff",
            "duration_minutes": 60,
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"organization_uuid": "not-a-uuid"},
        {"organization_uuid": "00000000-0000-0000-0000-000000000000"},
        {"subject_user_uuid": "00000000-0000-0000-0000-000000000000"},
    ],
    ids=["no-target", "malformed", "unknown-org", "unknown-user"],
)
def test_an_invalid_target_is_422(support_world, target):
    w = support_world
    response = w.client.post(
        "/api/support-access/grants",
        headers=w.admin_headers,
        json={
            "access_level": "read",
            "reason": "Investigating a customer-reported problem",
            "duration_minutes": 60,
            **target,
        },
    )
    assert response.status_code == 422
    assert _code(response) == "support_grant_invalid_target"


def test_both_targets_at_once_is_422(support_world):
    w = support_world
    response = w.client.post(
        "/api/support-access/grants",
        headers=w.admin_headers,
        json={
            "organization_uuid": str(w.org.uuid),
            "subject_user_uuid": str(w.owner.uuid),
            "access_level": "read",
            "reason": "Investigating a customer-reported problem",
            "duration_minutes": 60,
        },
    )
    assert response.status_code == 422
    assert _code(response) == "support_grant_invalid_target"


def test_a_request_starts_pending_and_tells_the_tenants_admins(support_world):
    w = support_world
    grant = w.request()
    assert grant["status"] == "pending"
    assert grant["grant_mode"] == "approved"
    assert grant["starts_at"] is None
    assert grant["pending_expires_at"] is not None
    assert (w.org_admin.id, lifecycle.WS_REQUESTED) in _notified(w)


def test_the_approver_sees_who_is_asking(support_world):
    """Decision D7: the tenant is shown the grantee's name and email."""
    w = support_world
    grant = w.request()
    listing = w.client.get("/api/org-admin/support-access", headers=w.org_admin_headers)
    assert listing.status_code == 200
    item = next(i for i in listing.json()["items"] if i["uuid"] == grant["uuid"])
    assert item["grantee"]["email"] == w.admin.email
    assert item["grantee"]["full_name"] == w.admin.full_name
    assert listing.json()["server_time"]


# ---------------------------------------------------------------------------
# Who may decide
# ---------------------------------------------------------------------------


def test_self_approval_is_refused(support_world):
    """A platform admin who is also an org admin may not approve their own request."""
    w = support_world
    w.roles[w.admin.id] = (w.org.id, "org:admin")
    grant = w.request()
    response = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/approve", headers=w.admin_headers, json={}
    )
    assert response.status_code == 403
    assert _code(response) == "support_grant_self_approval"
    assert w.row(grant["uuid"]).decision is None


def test_a_platform_admin_cannot_approve_another_platform_admins_request(support_world):
    """Decision D2: the tenant decides, not other staff, even holding org:admin."""
    w = support_world
    w.roles[w.super_admin.id] = (w.org.id, "org:admin")
    grant = w.request()
    response = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/approve",
        headers=w.super_headers,
        json={},
    )
    assert response.status_code == 403
    assert _code(response) == "support_grant_self_approval"
    assert w.row(grant["uuid"]).decision is None


def test_another_orgs_admin_sees_the_grant_as_not_found(support_world):
    w = support_world
    grant = w.request()
    w.roles[w.owner.id] = (w.other_org.id, "org:admin")
    for path in ("approve", "deny"):
        response = w.client.post(
            f"/api/org-admin/support-access/{grant['uuid']}/{path}",
            headers=w.owner_headers,
            json={},
        )
        assert response.status_code == 404, path
    assert w.row(grant["uuid"]).decision is None


def test_an_org_member_who_is_not_an_admin_cannot_decide(support_world):
    w = support_world
    grant = w.request()
    w.roles[w.owner.id] = (w.org.id, "org:member")
    response = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/approve",
        headers=w.owner_headers,
        json={},
    )
    assert response.status_code == 403
    assert w.row(grant["uuid"]).decision is None


def test_approve_may_shorten_but_never_extend(support_world):
    w = support_world
    grant = w.request(minutes=60)
    too_long = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/approve",
        headers=w.org_admin_headers,
        json={"duration_minutes": 120},
    )
    assert too_long.status_code == 422
    assert _code(too_long) == "support_grant_duration_exceeds_request"
    assert w.row(grant["uuid"]).decision is None

    approved = w.approve(grant["uuid"], minutes=30)
    starts = datetime.fromisoformat(approved["starts_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(approved["expires_at"].replace("Z", "+00:00"))
    assert (expires - starts).total_seconds() == pytest.approx(30 * 60, abs=2)
    assert approved["status"] == "active"
    assert (w.admin.id, lifecycle.WS_DECIDED) in _notified(w)


def test_deny_ends_the_request_and_cannot_be_reversed(support_world):
    w = support_world
    grant = w.request()
    denied = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/deny",
        headers=w.org_admin_headers,
        json={"note": "not expecting support today"},
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"
    again = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/approve",
        headers=w.org_admin_headers,
        json={},
    )
    assert again.status_code == 409
    assert _code(again) == "support_grant_already_decided"
    assert w.row(grant["uuid"]).decision == "denied"


def test_a_decision_that_lost_the_race_is_refused_and_changes_nothing(support_world):
    """The conditional UPDATE, not the in-memory row, decides who won.

    The approver's row was read while the request was still undecided; the request is then
    denied underneath it. The approve must not overwrite the denial.
    """
    w = support_world
    grant = w.request()
    stale = w.row(grant["uuid"])
    assert stale.decision is None
    w.db.execute(
        update(SupportAccessGrant)
        .where(SupportAccessGrant.id == stale.id)
        .values(decision="denied", decided_at=datetime.now(UTC)),
        execution_options={"synchronize_session": False},
    )
    with pytest.raises(HTTPException) as caught:
        lifecycle.approve(w.db, w.org_admin, stale, duration_minutes=None, org_route=True)
    assert caught.value.status_code == 409
    detail: Any = caught.value.detail
    assert detail["code"] == "support_grant_already_decided"
    assert w.row(grant["uuid"]).decision == "denied"
    assert w.row(grant["uuid"]).starts_at is None


def test_a_lapsed_request_can_no_longer_be_decided(support_world):
    w = support_world
    grant = w.request()
    w.set_times(grant["uuid"], requested_at=-73)
    response = w.client.post(
        f"/api/org-admin/support-access/{grant['uuid']}/approve",
        headers=w.org_admin_headers,
        json={},
    )
    assert response.status_code == 409
    assert _code(response) == "support_grant_lapsed"
    listed = w.client.get(
        "/api/org-admin/support-access", headers=w.org_admin_headers, params={"status": "lapsed"}
    )
    assert [i["uuid"] for i in listed.json()["items"]] == [grant["uuid"]]


# ---------------------------------------------------------------------------
# Personal workspaces: the subject decides
# ---------------------------------------------------------------------------


def test_a_person_approves_access_to_their_own_workspace(support_world):
    w = support_world
    grant = w.request(subject_user_uuid=str(w.owner.uuid))
    assert grant["target_kind"] == "personal"
    assert (w.owner.id, lifecycle.WS_REQUESTED) in _notified(w)

    pending = w.client.get("/api/users/me/support-access", headers=w.owner_headers)
    assert [i["uuid"] for i in pending.json()["items"]] == [grant["uuid"]]

    approved = w.client.post(
        f"/api/users/me/support-access/{grant['uuid']}/approve", headers=w.owner_headers, json={}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"


def test_someone_elses_personal_grant_is_not_found(support_world):
    w = support_world
    grant = w.request(subject_user_uuid=str(w.owner.uuid))
    response = w.client.post(
        f"/api/users/me/support-access/{grant['uuid']}/approve",
        headers=w.org_admin_headers,
        json={},
    )
    assert response.status_code == 404


def test_an_admin_cannot_target_their_own_workspace(support_world):
    w = support_world
    response = w.client.post(
        "/api/support-access/grants",
        headers=w.admin_headers,
        json={
            "subject_user_uuid": str(w.admin.uuid),
            "access_level": "read",
            "reason": "Investigating my own workspace",
            "duration_minutes": 60,
        },
    )
    assert response.status_code == 422
    assert _code(response) == "support_grant_invalid_target"


# ---------------------------------------------------------------------------
# Break glass
# ---------------------------------------------------------------------------


def _break_glass(w, headers, **overrides):
    body = {
        "organization_uuid": str(w.org.uuid),
        "access_level": "read",
        "reason": "Customer outage, investigating production data",
        "ticket_ref": "INC-1042",
        "duration_minutes": 60,
        **overrides,
    }
    return w.client.post("/api/support-access/grants/break-glass", headers=headers, json=body)


def test_break_glass_is_super_admin_only(support_world):
    w = support_world
    assert _break_glass(w, w.admin_headers).status_code == 403
    assert _break_glass(w, w.owner_headers).status_code == 403


def test_break_glass_needs_a_ticket_a_reason_and_a_short_ttl(support_world):
    w = support_world
    assert _break_glass(w, w.super_headers, ticket_ref="").status_code == 422
    assert _break_glass(w, w.super_headers, ticket_ref=None).status_code == 422
    assert _break_glass(w, w.super_headers, reason="too short").status_code == 422
    assert _break_glass(w, w.super_headers, duration_minutes=300).status_code == 422
    assert w.db.query(SupportAccessGrant).count() == 0


def test_break_glass_is_immediately_usable_audited_and_visible_to_the_tenant(support_world):
    w = support_world
    with patch("app.auth.audit.audit_logger.log") as audit:
        response = _break_glass(w, w.super_headers)
    assert response.status_code == 201
    grant = response.json()
    assert grant["grant_mode"] == "break_glass"
    assert grant["status"] == "active"
    assert grant["ticket_ref"] == "INC-1042"

    opened = [c for c in audit.call_args_list if c.args[0] == "support_access.break_glass"]
    assert len(opened) == 1
    assert opened[0].kwargs["organization_id"] == w.org.id

    # told in real time, and can read straight away
    assert (w.org_admin.id, lifecycle.WS_BREAK_GLASS) in _notified(w)
    read = w.client.get(
        f"/api/files/{w.file.uuid}", headers=w.with_grant(grant["uuid"], w.super_headers)
    )
    assert read.status_code == 200

    # and the tenant's admin can see it
    listing = w.client.get("/api/org-admin/support-access", headers=w.org_admin_headers)
    assert grant["uuid"] in [i["uuid"] for i in listing.json()["items"]]


def test_personal_break_glass_notifies_the_subject_and_refuses_your_own_workspace(support_world):
    w = support_world
    response = _break_glass(
        w, w.super_headers, organization_uuid=None, subject_user_uuid=str(w.owner.uuid)
    )
    assert response.status_code == 201
    assert (w.owner.id, lifecycle.WS_BREAK_GLASS) in _notified(w)
    assert response.json()["subject_user"]["email"] == w.owner.email

    own = _break_glass(
        w, w.super_headers, organization_uuid=None, subject_user_uuid=str(w.super_admin.uuid)
    )
    assert own.status_code == 422
    assert _code(own) == "support_grant_invalid_target"


# ---------------------------------------------------------------------------
# Revoking, listing, and who may look
# ---------------------------------------------------------------------------


def test_revoke_is_idempotent_and_ends_the_grant(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    first = w.client.post(
        f"/api/support-access/grants/{grant_uuid}/revoke", headers=w.admin_headers
    )
    second = w.client.post(
        f"/api/support-access/grants/{grant_uuid}/revoke", headers=w.admin_headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "revoked"
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


@pytest.mark.parametrize("who", ["org_admin", "super", "subject"])
def test_the_tenant_and_super_admin_can_revoke(support_world, who):
    w = support_world
    if who == "subject":
        grant = w.request(subject_user_uuid=str(w.owner.uuid))
        headers = w.owner_headers
    else:
        grant = w.request()
        headers = w.org_admin_headers if who == "org_admin" else w.super_headers
    response = w.client.post(f"/api/support-access/grants/{grant['uuid']}/revoke", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


def test_a_stranger_cannot_revoke(support_world):
    w = support_world
    grant = w.request()
    response = w.client.post(
        f"/api/support-access/grants/{grant['uuid']}/revoke", headers=w.owner_headers
    )
    assert response.status_code == 404
    assert w.row(grant["uuid"]).revoked_at is None


def test_grant_detail_and_uses_are_not_found_for_another_admin(support_world):
    from tests.fixtures.support_world import login_headers
    from tests.fixtures.support_world import make_user

    w = support_world
    grant = w.request()
    rival = make_user(w.db, role="admin", label="rival")
    rival_headers = login_headers(w.client, rival)
    for suffix in ("", "/uses"):
        mine = w.client.get(
            f"/api/support-access/grants/{grant['uuid']}{suffix}", headers=w.admin_headers
        )
        theirs = w.client.get(
            f"/api/support-access/grants/{grant['uuid']}{suffix}", headers=rival_headers
        )
        assert mine.status_code == 200, suffix
        assert theirs.status_code == 404, suffix


def test_scope_all_is_super_admin_only(support_world):
    w = support_world
    grant = w.request()
    denied = w.client.get(
        "/api/support-access/grants", headers=w.admin_headers, params={"scope": "all"}
    )
    assert denied.status_code == 403
    allowed = w.client.get(
        "/api/support-access/grants", headers=w.super_headers, params={"scope": "all"}
    )
    assert allowed.status_code == 200
    assert grant["uuid"] in [i["uuid"] for i in allowed.json()["items"]]
    mine = w.client.get("/api/support-access/grants", headers=w.super_headers)
    assert grant["uuid"] not in [i["uuid"] for i in mine.json()["items"]]


def test_target_search_lists_active_organizations(support_world):
    w = support_world
    response = w.client.get(
        "/api/support-access/targets/organizations",
        headers=w.admin_headers,
        params={"q": w.org.name},
    )
    assert response.status_code == 200
    assert [o["uuid"] for o in response.json()] == [str(w.org.uuid)]
    assert (
        w.client.get(
            "/api/support-access/targets/organizations", headers=w.owner_headers
        ).status_code
        == 403
    )


def test_a_grant_survives_the_erasure_of_its_grantee(support_world):
    """SET NULL, not CASCADE: the evidence outlives the account (decision D5)."""
    w = support_world
    grant_uuid = w.active_grant()
    row = w.row(grant_uuid)
    w.db.delete(w.db.get(type(w.admin), w.admin.id))
    w.db.commit()
    kept = w.row(grant_uuid)
    assert kept.id == row.id
    assert kept.grantee_user_id is None
