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


class TestAdminDeleteUserCompositionKeepsOneSuperAdmin:
    """``DELETE /api/admin/users/{uuid}`` — safe by COMPOSITION, not by the last-one guard.

    Worth stating plainly, because the first version of this class asserted something it
    could not reach. The two pre-existing guards already make losing the last super_admin
    impossible:

    * deleting a super_admin requires **being** one (403 otherwise), and
    * you cannot target yourself (400).

    So the caller is always a surviving super_admin, and ``_assert_not_last_super_admin``
    on this route can never fire. It is kept as a backstop for a future change to either
    premise (see the comment at its call site), but a test named "deleting the last
    super_admin is refused" would really be asserting the self-target guard while
    appearing to cover the other one — and it passed against the unfixed code, which is
    how this was caught. These tests pin the two premises the safety actually rests on.
    """

    def test_a_plain_admin_cannot_delete_a_super_admin(
        self, client, admin_token_headers, db_session
    ):
        """Premise 1. Lose this 403 and a plain admin can delete every super_admin."""
        victim = _make_super_admin(db_session)

        response = client.delete(f"/api/admin/users/{victim.uuid}", headers=admin_token_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert _super_admin_count(db_session) >= 1

    def test_a_super_admin_cannot_delete_their_own_account(
        self, client, super_admin_token_headers, super_admin_user
    ):
        """Premise 2. Together with premise 1, this is what guarantees a survivor."""
        response = client.delete(
            f"/api/admin/users/{super_admin_user.uuid}",
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_super_admin_can_delete_another_super_admin(
        self, client, super_admin_token_headers, db_session
    ):
        """The positive control: the route must still work for its real use.

        Without it, tightening either premise into "refuse everything" would satisfy both
        tests above.
        """
        victim = _make_super_admin(db_session)

        response = client.delete(
            f"/api/admin/users/{victim.uuid}", headers=super_admin_token_headers
        )

        assert response.status_code == status.HTTP_200_OK


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
