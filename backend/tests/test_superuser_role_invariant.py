"""Security tests for the unified privilege model.

`User.role` is the single source of truth for authorization; `is_superuser`
is a derived mirror of (role == "super_admin"). These tests lock in the
invariant AND prove the privilege boundaries cannot be crossed from the
client side (the critical property for the AWS cloud deployment):

- Authorization is enforced server-side on the DB-loaded user.
- A tampered/forged token role claim is ignored (DB is source of truth).
- A regular admin cannot create or self-promote to admin/super_admin.
- is_superuser can never be set directly by a client; it is always derived.
"""

from __future__ import annotations

import pytest

from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.core.security import create_access_token
from app.models.user import User


# --------------------------------------------------------------------------- #
# Pure invariant
# --------------------------------------------------------------------------- #
def test_role_implies_superuser_only_for_super_admin():
    assert role_implies_superuser(ROLE_SUPER_ADMIN) is True
    assert role_implies_superuser(ROLE_ADMIN) is False
    assert role_implies_superuser(ROLE_USER) is False
    assert role_implies_superuser(None) is False
    assert role_implies_superuser("root") is False


def test_db_check_constraint_blocks_divergent_rows(db_session):
    """The DB CHECK constraint forbids is_superuser != (role == super_admin)."""
    from sqlalchemy.exc import IntegrityError

    from app.core.security import get_password_hash

    bad = User(
        email="divergent@example.com",
        full_name="Divergent",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        role=ROLE_ADMIN,
        is_superuser=True,  # violates is_superuser == (role == super_admin)
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --------------------------------------------------------------------------- #
# create_user derives is_superuser from role and validates it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "role,expected_superuser",
    [(ROLE_USER, False), (ROLE_ADMIN, False), (ROLE_SUPER_ADMIN, True)],
)
def test_create_user_derives_is_superuser(db_session, role, expected_superuser):
    import uuid

    from app.api.endpoints.users import create_user
    from app.schemas.user import UserCreate

    uid = str(uuid.uuid4())[:8]
    # Client tries to set is_superuser=True regardless of role — must be ignored.
    payload = UserCreate(
        email=f"derive_{uid}@example.com",
        full_name="Derive Test",
        password="Sup3rStr0ng!pass",
        role=role,
        is_superuser=True,
    )
    user = create_user(payload, db_session)
    assert user.role == role
    assert user.is_superuser is expected_superuser


def test_create_user_rejects_invalid_role(db_session):
    import uuid

    from fastapi import HTTPException

    from app.api.endpoints.users import create_user
    from app.schemas.user import UserCreate

    uid = str(uuid.uuid4())[:8]
    payload = UserCreate(
        email=f"badrole_{uid}@example.com",
        full_name="Bad Role",
        password="Sup3rStr0ng!pass",
        role="root",
    )
    with pytest.raises(HTTPException) as exc:
        create_user(payload, db_session)
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# C1: privilege-escalation via the admin create endpoint is blocked
# --------------------------------------------------------------------------- #
def _create_payload(role: str | None = None, is_superuser: bool | None = None) -> dict:
    import uuid

    uid = str(uuid.uuid4())[:8]
    body: dict = {
        "email": f"created_{uid}@example.com",
        "password": "Sup3rStr0ng!pass",
        "full_name": "Created User",
        "is_active": True,
    }
    if role is not None:
        body["role"] = role
    if is_superuser is not None:
        body["is_superuser"] = is_superuser
    return body


def test_regular_admin_cannot_create_admin(client, admin_token_headers):
    """A non-super_admin admin must not be able to mint admin accounts."""
    resp = client.post(
        "/api/admin/users",
        json=_create_payload(role=ROLE_ADMIN),
        headers=admin_token_headers,
    )
    assert resp.status_code == 403


def test_regular_admin_cannot_create_super_admin(client, admin_token_headers):
    resp = client.post(
        "/api/admin/users",
        json=_create_payload(role=ROLE_SUPER_ADMIN),
        headers=admin_token_headers,
    )
    assert resp.status_code == 403


def test_regular_admin_cannot_escalate_via_is_superuser_flag(client, admin_token_headers):
    """Even sending is_superuser=True with role=user must not grant privilege."""
    resp = client.post(
        "/api/admin/users",
        json=_create_payload(role=ROLE_USER, is_superuser=True),
        headers=admin_token_headers,
    )
    # Allowed to create a plain user, but the derived flag must be False.
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_superuser"] is False
    assert resp.json()["role"] == ROLE_USER


def test_super_admin_can_create_admin_with_derived_flag(client, super_admin_token_headers):
    resp = client.post(
        "/api/admin/users",
        json=_create_payload(role=ROLE_ADMIN),
        headers=super_admin_token_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == ROLE_ADMIN
    assert body["is_superuser"] is False  # admin is not a superuser


# --------------------------------------------------------------------------- #
# Token tampering: the role claim is never trusted (DB is source of truth)
# --------------------------------------------------------------------------- #
def test_forged_role_claim_is_ignored(client, normal_user):
    """A validly-signed token whose role claim says super_admin must NOT grant
    super_admin access — get_current_user re-loads the role from the DB."""
    forged = create_access_token(
        subject=str(normal_user.uuid),
        additional_claims={"role": ROLE_SUPER_ADMIN},
    )
    headers = {"Authorization": f"Bearer {forged}"}
    # /admin/users is admin-gated; a forged super_admin claim on a 'user' must 403.
    resp = client.get("/api/admin/users", headers=headers)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Role change keeps is_superuser in sync
# --------------------------------------------------------------------------- #
def test_role_change_syncs_is_superuser(client, super_admin_token_headers, normal_user, db_session):
    # Promote to super_admin -> is_superuser becomes True
    resp = client.put(
        f"/api/admin/users/{normal_user.uuid}/role",
        params={"new_role": ROLE_SUPER_ADMIN},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == 200
    db_session.refresh(normal_user)
    assert normal_user.role == ROLE_SUPER_ADMIN
    assert normal_user.is_superuser is True

    # Demote to admin -> is_superuser becomes False
    resp = client.put(
        f"/api/admin/users/{normal_user.uuid}/role",
        params={"new_role": ROLE_ADMIN},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == 200
    db_session.refresh(normal_user)
    assert normal_user.role == ROLE_ADMIN
    assert normal_user.is_superuser is False


# --------------------------------------------------------------------------- #
# super_admin-tier gate requires role == super_admin (not the is_superuser bool)
# --------------------------------------------------------------------------- #
def test_super_admin_gate_denies_regular_admin(client, admin_token_headers):
    # auth-config is super_admin-gated.
    resp = client.get("/api/admin/auth-config/status", headers=admin_token_headers)
    assert resp.status_code == 403


def test_super_admin_gate_allows_super_admin(client, super_admin_token_headers):
    resp = client.get("/api/admin/auth-config/status", headers=super_admin_token_headers)
    assert resp.status_code != 403
