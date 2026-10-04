"""The anonymous local-account surface when local authentication is OFF (issue #997).

A deployment whose identity lives in an external provider turns ``local_enabled`` off.
Before this, the anonymous local-account routes still did full work for anyone who
asked: ``password-reset/request`` looked the address up and wrote an audit event,
``verify-email/resend`` looked it up and could mint a token, and so on — each one a
free DB session plus an audit write on the request path, for a feature the
deployment has switched off.

What must hold with local auth off:

* ``verify-email`` / ``verify-email/resend`` answer 404 without reaching the service.
  Email verification only ever gates local-password login (see
  ``app/auth/email_verification.py``), so there is nothing for them to do.
* ``register`` is refused even when the env fallback for open registration is on.
* ``password-reset/*`` keep serving exactly one account class — the active
  ``super_admin`` break-glass account (``app/auth/CLAUDE.md``, issue #910) — and for
  every other caller answer the same generic response *without* running the reset
  service or writing an audit event.
* Invitations are NOT gated on ``local_enabled``: they also provision accounts for
  external identity providers, which is exactly the deployment shape that turns
  local auth off.
"""

from __future__ import annotations

import hashlib
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.auth import audit as audit_module
from app.models.invitation import UserInvitation
from app.models.password_reset import PasswordResetToken
from app.services.auth_config_service import AuthConfigService

STRONG_PASSWORD = "Correct-Horse-9Battery!"  # noqa: S105 - test fixture, not a credential

RESET_REQUEST_PATH = "/api/auth/password-reset/request"
RESET_CONFIRM_PATH = "/api/auth/password-reset/confirm"
VERIFY_EMAIL_PATH = "/api/auth/verify-email"
RESEND_VERIFY_PATH = "/api/auth/verify-email/resend"
REGISTER_PATH = "/api/auth/register"
INVITE_LOOKUP_PATH = "/api/auth/invitations/lookup"


class _Mailer:
    def __init__(self) -> None:
        self.resets: list[str] = []
        self.verifications: list[str] = []

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        self.resets.append(reset_url)

    def send_email_verification(self, to_email: str, verify_url: str, expires_in_hours) -> None:
        self.verifications.append(verify_url)


@pytest.fixture
def mailer(monkeypatch) -> _Mailer:
    recorder = _Mailer()
    monkeypatch.setattr("app.auth.password_reset.email_service", recorder)
    monkeypatch.setattr("app.auth.email_verification.email_service", recorder)
    return recorder


@pytest.fixture
def audit_events(monkeypatch) -> list[str]:
    """Every audit event written during the test, by event type."""
    events: list[str] = []
    real_log = audit_module.audit_logger.log

    def _recording_log(event_type, *args, **kwargs):
        events.append(getattr(event_type, "value", str(event_type)))
        return real_log(event_type, *args, **kwargs)

    monkeypatch.setattr(audit_module.audit_logger, "log", _recording_log)
    return events


@pytest.fixture
def local_auth_off(db_session, super_admin_user):
    """Turn local password authentication off through the DB-backed setting."""
    AuthConfigService.bulk_update_category(
        db_session,
        "local",
        {"allow_registration": False, "local_enabled": False},
        user_id=int(super_admin_user.id),
    )
    yield


def _forbid(monkeypatch, target: str) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError(f"{target} ran although local authentication is disabled")

    monkeypatch.setattr(target, _boom)


def _issue_reset_token(db_session, user) -> str:
    raw = f"forged-{uuid_pkg.uuid4().hex}"
    db_session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            ip_address="10.0.0.1",
        )
    )
    db_session.commit()
    return raw


class TestEmailVerificationIsGone:
    def test_verify_email_is_404_and_never_reaches_the_service(
        self, client, local_auth_off, monkeypatch
    ):
        _forbid(monkeypatch, "app.api.endpoints.auth.email_verification.verify_email")
        response = client.post(VERIFY_EMAIL_PATH, json={"token": "anything"})
        assert response.status_code == 404, response.text

    def test_resend_is_404_and_never_reaches_the_service(
        self, client, local_auth_off, normal_user, monkeypatch, audit_events
    ):
        _forbid(monkeypatch, "app.api.endpoints.auth.email_verification.resend_verification")
        response = client.post(RESEND_VERIFY_PATH, json={"email": normal_user.email})
        assert response.status_code == 404, response.text
        assert audit_events == []

    def test_resend_still_works_with_local_auth_on(self, client, normal_user, mailer):
        response = client.post(RESEND_VERIFY_PATH, json={"email": normal_user.email})
        assert response.status_code == 200, response.text


class TestRegistrationIsClosed:
    def test_env_fallback_cannot_reopen_registration_with_local_auth_off(self, client, monkeypatch):
        """No DB row for either key here — the env fallback is what is read. Open
        registration with local auth off would mint accounts that cannot sign in."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "LOCAL_AUTH_ENABLED", False)
        monkeypatch.setattr(settings, "ALLOW_OPEN_REGISTRATION", True)
        monkeypatch.setattr(
            AuthConfigService, "get_effective_config", staticmethod(lambda db, key: None)
        )
        response = client.post(
            REGISTER_PATH,
            json={
                "email": f"newcomer-{uuid_pkg.uuid4().hex[:10]}@example.com",
                "full_name": "New Comer",
                "password": STRONG_PASSWORD,
            },
        )
        assert response.status_code == 403, response.text


class TestPasswordResetKeepsOnlyTheBreakGlassPath:
    def test_an_ordinary_account_does_no_work_and_writes_no_audit_event(
        self, client, local_auth_off, normal_user, mailer, monkeypatch, audit_events
    ):
        _forbid(monkeypatch, "app.auth.password_reset.request_password_reset")
        response = client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        assert response.status_code == 200, response.text
        assert mailer.resets == []
        assert audit_events == []

    def test_an_unknown_address_is_answer_identical(
        self, client, local_auth_off, normal_user, super_admin_user, mailer
    ):
        """Anti-enumeration still holds: unknown, ordinary and break-glass
        addresses all get the same answer."""
        unknown = client.post(RESET_REQUEST_PATH, json={"email": "nobody-x@example.com"})
        ordinary = client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        break_glass = client.post(RESET_REQUEST_PATH, json={"email": super_admin_user.email})
        assert (
            (unknown.status_code, unknown.json())
            == (ordinary.status_code, ordinary.json())
            == (break_glass.status_code, break_glass.json())
        )

    def test_the_break_glass_super_admin_completes_the_chain_over_http(
        self, client, local_auth_off, super_admin_user, mailer
    ):
        response = client.post(RESET_REQUEST_PATH, json={"email": super_admin_user.email})
        assert response.status_code == 200, response.text
        assert len(mailer.resets) == 1
        token = mailer.resets[0].split("token=")[1]

        confirm = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert confirm.status_code == 200, confirm.text

    def test_an_ordinary_accounts_token_is_refused_without_an_audit_write(
        self, client, local_auth_off, normal_user, db_session, monkeypatch, audit_events
    ):
        """A token issued before local auth was switched off must not be redeemable
        once it is off, and a probe must not cost an audit write."""
        token = _issue_reset_token(db_session, normal_user)
        _forbid(monkeypatch, "app.auth.password_reset.confirm_password_reset")
        response = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert response.status_code == 400, response.text
        assert audit_events == []

    def test_an_unknown_token_is_refused_identically(self, client, local_auth_off, normal_user):
        response = client.post(
            RESET_CONFIRM_PATH, json={"token": "no-such-token", "new_password": STRONG_PASSWORD}
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Invalid or expired reset token"


class TestInvitationsStillServeExternalAccounts:
    def test_an_external_invitation_is_still_redeemable_with_local_auth_off(
        self, client, local_auth_off, db_session, admin_user
    ):
        raw = f"invite-{uuid_pkg.uuid4().hex}"
        db_session.add(
            UserInvitation(
                email=f"sso-{uuid_pkg.uuid4().hex[:10]}@example.com",
                role="user",
                auth_type="oidc",
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
                created_by_id=admin_user.id,
                ip_address="10.0.0.1",
            )
        )
        db_session.commit()

        response = client.post(INVITE_LOOKUP_PATH, json={"token": raw})
        assert response.status_code == 200, response.text
        assert response.json()["requires_password"] is False
