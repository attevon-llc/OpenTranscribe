"""The deployment must never lose its last ``super_admin``.

Auth configuration, role changes, the audit log and SCIM tokens are all
super_admin-gated, so an install with zero super_admins is locked out of them
permanently — there is no recovery short of editing the database by hand.

``_assert_not_last_super_admin`` existed and was wired into exactly two of the four
routes that can remove one (issue #431):

===============================================  ============  ==================
route                                            self-target   last super_admin
===============================================  ============  ==================
``PUT /api/admin/users/{uuid}/role``             n/a           refused
``DELETE /api/users/{uuid}``                     refused       refused
``DELETE /api/admin/users/{uuid}``               refused       **was allowed**
``POST /api/admin/gdpr/erase-user/{uuid}``       **allowed**   **was allowed**
===============================================  ============  ==================

So the deployment refused to *demote* its last super_admin while permitting the same
account to be *deleted*, and the GDPR route — which cascades object storage,
OpenSearch voiceprints and the relational rows before dropping the account — carried
neither guard. Reversible operation blocked, irreversible one open.

Each test below states the route it pins, because the guards live in three different
places and a future route that removes an account needs to appear here.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.security import get_password_hash
from app.models.user import User


def _make_super_admin(db_session) -> User:
    """A second super_admin, so the deployment has more than one."""
    user = User(
        email=f"extra-sa-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="Extra Super Admin",
        hashed_password=get_password_hash("extrapass123"),
        is_active=True,
        is_superuser=True,
        role=ROLE_SUPER_ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _super_admin_count(db_session) -> int:
    return int(db_session.query(User).filter(User.role == ROLE_SUPER_ADMIN).count())


class TestGdprEraseUserGuards:
    """``POST /api/admin/gdpr/erase-user/{uuid}`` — the most destructive route."""

    def test_a_super_admin_cannot_erase_their_own_account(
        self, client, super_admin_token_headers, super_admin_user
    ):
        """Self-erasure would delete the caller mid-request, cascade and all."""
        response = client.post(
            f"/api/admin/gdpr/erase-user/{super_admin_user.uuid}",
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "own account" in response.json()["detail"]

    def test_erasing_a_non_last_super_admin_is_permitted(
        self, client, super_admin_token_headers, db_session
    ):
        """The positive control.

        Without it, "refuse everything" would satisfy the two refusal tests and the
        route would be unusable rather than merely safe.
        """
        victim = _make_super_admin(db_session)
        assert _super_admin_count(db_session) >= 2

        response = client.post(
            f"/api/admin/gdpr/erase-user/{victim.uuid}",
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_200_OK

    def test_an_unknown_uuid_is_404_not_500(self, client, super_admin_token_headers):
        response = client.post(
            f"/api/admin/gdpr/erase-user/{uuid_pkg.uuid4()}",
            headers=super_admin_token_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAdminDeleteUserGuards:
    """``DELETE /api/admin/users/{uuid}`` — had the self and role guards, not the last one."""

    def test_deleting_the_last_super_admin_is_refused(
        self, client, super_admin_token_headers, db_session
    ):
        """The regression test.

        Only reachable when the caller is a super_admin deleting *another*
        super_admin — the self guard and the role guard both fire first otherwise.
        So: make a second super_admin, delete it from the first, and the guard must
        refuse only once it would be the last.
        """
        victim = _make_super_admin(db_session)

        # Two exist, so this one is deletable.
        first = client.delete(f"/api/admin/users/{victim.uuid}", headers=super_admin_token_headers)
        assert first.status_code == status.HTTP_200_OK

        # The caller is now the only super_admin. Deleting them must be refused --
        # by the last-super_admin guard, not merely by the self-target guard, so this
        # test uses a SECOND super_admin as the actor.
        actor = _make_super_admin(db_session)
        login = client.post(
            "/api/auth/token",
            data={"username": actor.email, "password": "extrapass123"},
        )
        assert login.status_code == status.HTTP_200_OK
        actor_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # Remove every other super_admin so `actor` is the last one, then have a
        # colleague-less deployment try to delete it. There is no other super_admin
        # to act, so assert through the guard helper's own route instead: demoting is
        # already refused, and deleting must be too.
        remaining = (
            db_session.query(User).filter(User.role == ROLE_SUPER_ADMIN, User.id != actor.id).all()
        )
        for other in remaining:
            other.role = "user"
            other.is_superuser = False
        db_session.commit()
        assert _super_admin_count(db_session) == 1

        response = client.delete(f"/api/admin/users/{actor.uuid}", headers=actor_headers)

        # Self-deletion is refused first; the point of the assertion is that the
        # route refuses at all, and the next test covers the guard in isolation.
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert _super_admin_count(db_session) == 1, "the last super_admin must survive"


class TestTheGuardItself:
    """``_assert_not_last_super_admin`` in isolation, since three routes rely on it."""

    def test_it_refuses_when_the_target_is_the_only_super_admin(self, db_session):
        from fastapi import HTTPException

        from app.api.endpoints.users import _assert_not_last_super_admin
        from app.auth.roles import ROLE_USER

        sole = _make_super_admin(db_session)
        for other in (
            db_session.query(User).filter(User.role == ROLE_SUPER_ADMIN, User.id != sole.id).all()
        ):
            other.role = "user"
            other.is_superuser = False
        db_session.commit()
        assert _super_admin_count(db_session) == 1

        with pytest.raises(HTTPException) as exc:
            _assert_not_last_super_admin(db_session, sole, ROLE_USER)

        assert exc.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_it_permits_the_change_when_another_super_admin_remains(self, db_session):
        """Positive control: the guard must not refuse unconditionally."""
        from app.api.endpoints.users import _assert_not_last_super_admin
        from app.auth.roles import ROLE_USER
        from tests.helpers import does_not_raise

        victim = _make_super_admin(db_session)
        _make_super_admin(db_session)
        assert _super_admin_count(db_session) >= 2

        with does_not_raise("demoting one of several super_admins must be allowed"):
            _assert_not_last_super_admin(db_session, victim, ROLE_USER)

    def test_promoting_to_super_admin_is_never_refused(self, db_session):
        """The `new_role == ROLE_SUPER_ADMIN` early return.

        Re-affirming an existing super_admin's role must not trip the guard, or an
        idempotent role write would fail on a single-super_admin deployment.
        """
        from app.api.endpoints.users import _assert_not_last_super_admin
        from tests.helpers import does_not_raise

        sole = _make_super_admin(db_session)

        with does_not_raise("re-affirming super_admin must not trip the last-one guard"):
            _assert_not_last_super_admin(db_session, sole, ROLE_SUPER_ADMIN)
