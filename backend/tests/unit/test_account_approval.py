"""Administrator approval of newly provisioned accounts (``v379``).

Three things are pinned here:

1. **Default off means nothing changes.** Every creation path still produces an
   immediately usable account unless ``require_account_approval`` is on.
2. **The refusal reuses the account-lifecycle mechanism** — a 403 whose ``detail``
   is an object with a machine-readable ``code``, exactly like
   ``password_change_required`` / ``account_expired`` / ``banner_acknowledgment_required``.
   There is deliberately no second convention for the SPA to learn.
3. **The bootstrap super_admin is never pending.** Only a signed-in administrator
   can clear the queue, so holding the break-glass account is an unrecoverable
   deployment.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (dict payloads, fake sessions, fake
# users) to signatures declaring the real dataclasses. Declared once here
# rather than as a cast at every call site — casts bury the assertion, and
# widening a production signature to suit a test is worse.
from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_PENDING_APPROVAL
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_REJECTED
from app.auth.approval import APPROVAL_APPROVED
from app.auth.approval import APPROVAL_PENDING
from app.auth.approval import APPROVAL_REJECTED
from app.auth.approval import VALID_APPROVAL_STATUSES
from app.auth.approval import initial_approval_status
from app.auth.approval import is_pending
from app.auth.approval import is_rejected
from app.auth.oidc.config import OIDCConfig
from app.auth.oidc.provisioning import sync_oidc_user_to_db
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("account_approval")


def _require_approval(db, actor, enabled: bool) -> None:
    """Flip the setting through the exact path the admin endpoint takes."""
    AuthConfigService.bulk_update_category(
        db=db,
        category="local",
        config_dict={"require_account_approval": enabled},
        user_id=actor.id,
    )


def _oidc_claims() -> dict:
    subject = f"sub-{uuid_pkg.uuid4().hex}"
    return {
        "oidc_subject": subject,
        "email": f"{subject}@example.com",
        "email_verified": True,
        "full_name": "JIT Person",
        "username": subject,
        "is_admin": False,
        "roles": [],
        "cert_dn": None,
        "cert_serial": None,
        "cert_issuer": None,
        "cert_org": None,
        "cert_ou": None,
        "cert_valid_from": None,
        "cert_valid_until": None,
        "cert_fingerprint": None,
    }


class TestTheStateMachine:
    def test_the_valid_set_is_closed_and_ordered_as_documented(self):
        assert set(VALID_APPROVAL_STATUSES) == {
            APPROVAL_PENDING,
            APPROVAL_APPROVED,
            APPROVAL_REJECTED,
        }

    def test_reads_fail_safe_on_an_object_without_the_column(self):
        """A stand-in User (the TESTING fallback in get_current_user) is not pending."""

        class Bare:
            pass

        assert is_pending(Bare()) is False
        assert is_rejected(Bare()) is False

    def test_off_by_default(self, db_session):
        assert initial_approval_status(db_session) == APPROVAL_APPROVED

    def test_on_when_configured(self, db_session, super_admin_user):
        _require_approval(db_session, super_admin_user, True)
        assert initial_approval_status(db_session) == APPROVAL_PENDING
        db_session.rollback()


class TestNewAccountsAreHeld:
    def test_self_registration_lands_pending(self, client, db_session, super_admin_user):
        _require_approval(db_session, super_admin_user, True)
        email = f"pending-{uuid_pkg.uuid4().hex[:8]}@example.com"

        response = client.post(
            "/api/auth/register",
            json={"email": email, "password": "Str0ng!Passphrase99", "full_name": "Held"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["approval_status"] == APPROVAL_PENDING
        row = db_session.query(User).filter(User.email == email).first()
        assert row is not None and row.approval_status == APPROVAL_PENDING
        db_session.rollback()

    def test_self_registration_is_unaffected_while_the_setting_is_off(self, client, db_session):
        email = f"open-{uuid_pkg.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/auth/register",
            json={"email": email, "password": "Str0ng!Passphrase99", "full_name": "Free"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approval_status"] == APPROVAL_APPROVED
        db_session.rollback()

    def test_oidc_jit_lands_pending(self, db_session, super_admin_user):
        _require_approval(db_session, super_admin_user, True)
        user = sync_oidc_user_to_db(db_session, _oidc_claims(), OIDCConfig(enabled=True))
        assert user.approval_status == APPROVAL_PENDING
        db_session.rollback()

    def test_oidc_jit_is_unaffected_while_the_setting_is_off(self, db_session):
        user = sync_oidc_user_to_db(db_session, _oidc_claims(), OIDCConfig(enabled=True))
        assert user.approval_status == APPROVAL_APPROVED
        db_session.rollback()

    def test_ldap_jit_uses_the_same_rule(self, db_session, super_admin_user):
        from app.auth.ldap_auth import _create_ldap_user

        _require_approval(db_session, super_admin_user, True)
        uid = f"ldapuid-{uuid_pkg.uuid4().hex[:8]}"
        user = _create_ldap_user(
            db_session,
            uid,
            f"{uid}@example.com",
            {
                "username": uid,
                "email": f"{uid}@example.com",
                "full_name": "Directory Person",
                "is_admin": False,
                "groups": [],
            },
            is_admin=False,
        )
        assert user.approval_status == APPROVAL_PENDING
        db_session.rollback()

    def test_an_admin_created_account_is_not_held(self, client, db_session, super_admin_user):
        """The administrator typing it in IS the approval; holding it is absurd."""
        _require_approval(db_session, super_admin_user, True)
        from app.api.endpoints.users import create_user
        from app.schemas.user import UserCreate

        email = f"provisioned-{uuid_pkg.uuid4().hex[:8]}@example.com"
        created = create_user(
            user_data=UserCreate(
                email=email, password="Str0ng!Passphrase99", full_name="Provisioned"
            ),
            db=db_session,
        )
        assert created.approval_status == APPROVAL_APPROVED
        db_session.rollback()


class TestTheGate:
    """The refusal is the existing account-lifecycle mechanism, not a new one."""

    def test_a_pending_account_is_refused_with_the_documented_code(
        self, client, db_session, super_admin_user, user_token_headers, normal_user
    ):
        _require_approval(db_session, super_admin_user, True)
        normal_user.approval_status = APPROVAL_PENDING
        db_session.commit()

        response = client.get("/api/users/me", headers=user_token_headers)

        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, dict), "detail must be an object, like the sibling gates"
        assert detail["code"] == ERROR_CODE_ACCOUNT_PENDING_APPROVAL
        db_session.rollback()

    def test_the_code_matches_the_existing_lifecycle_vocabulary(self):
        assert ERROR_CODE_ACCOUNT_PENDING_APPROVAL == "account_pending_approval"

    def test_an_approved_account_is_unaffected(
        self, client, db_session, super_admin_user, user_token_headers
    ):
        _require_approval(db_session, super_admin_user, True)
        response = client.get("/api/users/me", headers=user_token_headers)
        assert response.status_code == 200, response.text
        db_session.rollback()

    def test_turning_the_setting_off_releases_a_pending_account(
        self, client, db_session, user_token_headers, normal_user
    ):
        """The operator's escape hatch: withdrawing the policy empties the queue."""
        normal_user.approval_status = APPROVAL_PENDING
        db_session.commit()

        response = client.get("/api/users/me", headers=user_token_headers)

        assert response.status_code == 200, response.text
        db_session.rollback()

    def test_a_rejected_account_is_refused_even_with_the_setting_off(
        self, client, db_session, user_token_headers, normal_user
    ):
        """Rejection is a decision about one account, not a policy that can lapse."""
        normal_user.approval_status = APPROVAL_REJECTED
        db_session.commit()

        response = client.get("/api/users/me", headers=user_token_headers)

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == ERROR_CODE_ACCOUNT_REJECTED
        db_session.rollback()

    def test_a_pending_admin_gets_no_bypass(
        self, client, db_session, super_admin_user, admin_token_headers, admin_user
    ):
        """The admin tier chains through get_current_active_user, so the gate applies."""
        _require_approval(db_session, super_admin_user, True)
        admin_user.approval_status = APPROVAL_PENDING
        db_session.commit()

        response = client.get("/api/admin/users", headers=admin_token_headers)

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == ERROR_CODE_ACCOUNT_PENDING_APPROVAL
        db_session.rollback()


class TestTheBootstrapAdminIsNeverHeld:
    def test_initial_data_writes_the_status_explicitly(self):
        """Pinned as source, because the failure mode is an unopenable deployment.

        Relying on the column default would be correct today and silently wrong the
        day someone changes the default or adds a creation-time hook.
        """
        import inspect

        from app import initial_data

        source = inspect.getsource(initial_data._ensure_admin_user)
        assert "approval_status=APPROVAL_APPROVED" in source

    def test_the_live_bootstrap_admin_is_approved(self, db_session):
        from app.auth.roles import ROLE_SUPER_ADMIN

        held = (
            db_session.query(User)
            .filter(User.role == ROLE_SUPER_ADMIN, User.approval_status != APPROVAL_APPROVED)
            .all()
        )
        assert not held, f"super_admin account(s) awaiting approval: {[u.email for u in held]}"


class TestTheConfigKeyIsLive:
    def test_the_reader_resolves_the_saved_value(self, db_session, super_admin_user):
        """``test_auth_config_has_readers`` demands a reader; this demands it works."""
        from app.core.auth_settings import get_auth_settings

        _require_approval(db_session, super_admin_user, True)
        assert get_auth_settings(db_session).require_account_approval is True

        _require_approval(db_session, super_admin_user, False)
        assert get_auth_settings(db_session).require_account_approval is False
        db_session.rollback()
