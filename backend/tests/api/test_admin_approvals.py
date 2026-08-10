"""The account-approval admin surface (``v379``): ``/api/admin/user-approvals``.

Tier is **admin**, not super_admin: deciding who gets an account is user
management, and only deployment configuration is super_admin. The switch that
creates the queue (``require_account_approval``) is the super_admin half and lives
under ``/admin/auth-config``. ``tests/unit/test_route_privilege_tiers.py`` walks the
live dependency tree and would fail if these landed at the wrong tier; the test
here checks the other direction — that a plain user is actually refused.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (dict payloads, fake sessions, fake
# users) to signatures declaring the real dataclasses. Declared once here
# rather than as a cast at every call site — casts bury the assertion, and
# widening a production signature to suit a test is worse.
from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.auth.approval import APPROVAL_APPROVED
from app.auth.approval import APPROVAL_PENDING
from app.auth.approval import APPROVAL_REJECTED
from app.core.security import get_password_hash
from app.models.user import User

pytestmark = pytest.mark.xdist_group("admin_approvals")

BASE = "/api/admin/user-approvals"


@pytest.fixture
def pending_account(db_session) -> User:
    """An account sitting in the queue, as JIT provisioning would leave it."""
    user = User(
        email=f"held-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="Held Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="oidc",
        is_active=True,
        is_superuser=False,
        approval_status=APPROVAL_PENDING,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestTheQueue:
    def test_lists_only_pending_accounts(self, client, admin_token_headers, pending_account):
        response = client.get(BASE, headers=admin_token_headers)

        assert response.status_code == 200, response.text
        rows = response.json()
        assert str(pending_account.email) in {row["email"] for row in rows}
        # Nothing already decided may appear: the queue is the work list, and a
        # decided account showing up invites a second, conflicting decision.
        assert "approval_status" not in rows[0], "the queue is pending by construction"

    def test_the_row_says_where_the_account_came_from(
        self, client, admin_token_headers, pending_account
    ):
        """ "Someone signed up" and "an IdP minted this" are different decisions."""
        response = client.get(BASE, headers=admin_token_headers)
        row = next(r for r in response.json() if r["email"] == str(pending_account.email))

        assert row["auth_type"] == "oidc"
        assert row["role"] == "user"
        assert "created_at" in row
        assert "email_verified" in row

    def test_an_approved_account_is_not_in_the_queue(
        self, client, admin_token_headers, normal_user
    ):
        response = client.get(BASE, headers=admin_token_headers)
        assert str(normal_user.email) not in {row["email"] for row in response.json()}


class TestDecisions:
    def test_approve_admits_the_account(
        self, client, db_session, admin_token_headers, admin_user, pending_account
    ):
        response = client.post(
            f"{BASE}/{pending_account.uuid}/approve", headers=admin_token_headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["approval_status"] == APPROVAL_APPROVED
        assert body["approved_by"] == str(admin_user.uuid)

        db_session.refresh(pending_account)
        assert pending_account.approval_status == APPROVAL_APPROVED
        assert pending_account.approved_at is not None
        assert pending_account.approved_by == admin_user.id

    def test_reject_keeps_the_row(self, client, db_session, admin_token_headers, pending_account):
        """Deleting it would let the same person sign up again looking new."""
        user_id = pending_account.id

        response = client.post(
            f"{BASE}/{pending_account.uuid}/reject",
            headers=admin_token_headers,
            json={"reason": "not a member of staff"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["approval_status"] == APPROVAL_REJECTED
        assert db_session.query(User).filter(User.id == user_id).first() is not None

    def test_reject_records_who_decided(
        self, client, db_session, admin_token_headers, admin_user, pending_account
    ):
        client.post(f"{BASE}/{pending_account.uuid}/reject", headers=admin_token_headers)
        db_session.refresh(pending_account)
        assert pending_account.approved_by == admin_user.id
        assert pending_account.approved_at is not None

    def test_deciding_twice_is_a_conflict_not_a_silent_rewrite(
        self, client, admin_token_headers, pending_account
    ):
        assert (
            client.post(
                f"{BASE}/{pending_account.uuid}/approve", headers=admin_token_headers
            ).status_code
            == 200
        )
        second = client.post(f"{BASE}/{pending_account.uuid}/reject", headers=admin_token_headers)
        assert second.status_code == 409, second.text

    def test_an_unknown_account_is_a_404(self, client, admin_token_headers):
        response = client.post(f"{BASE}/{uuid_pkg.uuid4()}/approve", headers=admin_token_headers)
        assert response.status_code == 404


class TestApprovalUnblocksTheAccount:
    def test_the_pending_account_can_use_the_app_once_approved(
        self, client, db_session, admin_token_headers, super_admin_user
    ):
        """End to end: held -> 403 with the code -> approved -> 200, same token."""
        from app.services.auth_config_service import AuthConfigService

        AuthConfigService.bulk_update_category(
            db=db_session,
            category="local",
            config_dict={"require_account_approval": True},
            user_id=super_admin_user.id,
        )

        email = f"e2e-{uuid_pkg.uuid4().hex[:8]}@example.com"
        password = "Str0ng!Passphrase99"  # noqa: S105 # nosec B105
        assert (
            client.post(
                "/api/auth/register",
                json={"email": email, "password": password, "full_name": "E2E"},
            ).status_code
            == 200
        )

        tokens = client.post(
            "/api/auth/token",
            data={"username": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert tokens.status_code == 200, tokens.text
        headers = {"Authorization": f"Bearer {tokens.json()['access_token']}"}

        held = client.get("/api/users/me", headers=headers)
        assert held.status_code == 403
        assert held.json()["detail"]["code"] == "account_pending_approval"

        user = db_session.query(User).filter(User.email == email).first()
        assert (
            client.post(f"{BASE}/{user.uuid}/approve", headers=admin_token_headers).status_code
            == 200
        )

        # Same credential, no re-login: the gate was the only thing refusing it.
        assert client.get("/api/users/me", headers=headers).status_code == 200
        db_session.rollback()


class TestTier:
    def test_a_plain_user_is_refused(self, client, user_token_headers):
        assert client.get(BASE, headers=user_token_headers).status_code == 403

    def test_anonymous_is_refused(self, client):
        assert client.get(BASE).status_code == 401

    def test_an_admin_does_not_need_super_admin(self, client, admin_token_headers):
        """Pinned deliberately: managing users is admin, configuring is super_admin."""
        assert client.get(BASE, headers=admin_token_headers).status_code == 200
