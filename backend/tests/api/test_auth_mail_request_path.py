"""The request does the same work for every address; mail is sent by a task.

Password reset and verification resend used to send SMTP inside the request, and only when
the account existed, so response time told a caller which addresses have an account. These
tests pin the replacement: the endpoint looks the account up and dispatches one task, in
both cases, and touches no mail transport itself. They assert on what was dispatched and
what the request wrote, never on elapsed time.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.core.exceptions import EmailTransientError
from app.models.invitation import EmailVerificationToken
from app.models.password_reset import PasswordResetToken
from app.tasks import auth_mail_tasks

RESET_PATH = "/api/auth/password-reset/request"
RESEND_PATH = "/api/auth/verify-email/resend"


class _ForbiddenTransport:
    """Any use of the mail transport from the request path fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the request touched the mail transport: {name}")


@pytest.fixture
def no_transport_in_request(monkeypatch):
    forbidden = _ForbiddenTransport()
    monkeypatch.setattr("app.auth.password_reset.email_service", forbidden)
    monkeypatch.setattr("app.auth.email_verification.email_service", forbidden)
    return forbidden


def _nobody() -> str:
    return f"nobody-{uuid_pkg.uuid4().hex[:12]}@example.com"


def test_password_reset_request_dispatches_one_task_for_known_and_unknown(
    client, normal_user, auth_mail_dispatches, no_transport_in_request
):
    known = client.post(RESET_PATH, json={"email": normal_user.email})
    unknown = client.post(RESET_PATH, json={"email": _nobody()})

    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert [name for name, _, _ in auth_mail_dispatches] == ["auth_mail.password_reset"] * 2
    (_, known_args, _), (_, unknown_args, _) = auth_mail_dispatches
    assert known_args[0] == normal_user.id
    assert unknown_args[0] is None
    # The same shape: an id (or None) and an address, nothing else.
    assert len(known_args) == len(unknown_args) == 2


def test_password_reset_request_creates_no_token_and_no_secret_travels(
    client, db_session, normal_user, auth_mail_dispatches, no_transport_in_request
):
    client.post(RESET_PATH, json={"email": normal_user.email})

    assert db_session.query(PasswordResetToken).filter_by(user_id=normal_user.id).count() == 0
    (_, args, kwargs) = auth_mail_dispatches[0]
    assert all(isinstance(a, int | str | type(None)) for a in args)
    assert not kwargs
    assert not any("token" in str(a).lower() for a in args)


def test_resend_verification_dispatches_for_known_unknown_and_verified(
    client, db_session, normal_user, auth_mail_dispatches, no_transport_in_request
):
    client.post(RESEND_PATH, json={"email": normal_user.email})
    client.post(RESEND_PATH, json={"email": _nobody()})
    normal_user.email_verified = True
    db_session.commit()
    client.post(RESEND_PATH, json={"email": normal_user.email})

    names = [name for name, _, _ in auth_mail_dispatches]
    assert names == ["auth_mail.email_verification"] * 3
    assert db_session.query(EmailVerificationToken).filter_by(user_id=normal_user.id).count() == 0


def test_the_auth_mail_tasks_retry_transient_failures_with_bounded_backoff():
    for task in (
        auth_mail_tasks.send_password_reset_mail,
        auth_mail_tasks.send_email_verification_mail,
        auth_mail_tasks.send_invitation_mail,
        auth_mail_tasks.send_security_notice_mail,
    ):
        assert task.autoretry_for == (EmailTransientError,)
        assert task.max_retries == auth_mail_tasks.MAX_RETRIES
        assert task.retry_backoff
        assert task.retry_backoff_max


def test_a_transient_failure_is_retried_then_audited_once(
    db_session, normal_user, run_auth_mail_inline, monkeypatch
):
    calls: list[str] = []

    def always_transient(_to: str, _url: str) -> None:
        calls.append("send")
        raise EmailTransientError("connection reset")

    audits: list[dict] = []
    monkeypatch.setattr(
        "app.auth.password_reset.email_service.send_password_reset", always_transient
    )
    monkeypatch.setattr("app.auth.password_reset.audit_logger.log", lambda **kw: audits.append(kw))

    # Eager retries run nested and each level re-raises Retry, so errors are not thrown here.
    result = auth_mail_tasks.send_password_reset_mail.apply(
        args=(normal_user.id, "203.0.113.5"), throw=False
    )

    assert result.state == "SUCCESS"
    assert len(calls) == auth_mail_tasks.MAX_RETRIES + 1
    assert [a["error_code"] for a in audits] == ["DELIVERY_FAILED"]
    assert db_session.query(PasswordResetToken).filter_by(user_id=normal_user.id).count() == 0


def test_a_permanent_failure_is_not_retried(
    db_session, normal_user, run_auth_mail_inline, monkeypatch
):
    from app.core.exceptions import EmailDeliveryError

    calls: list[str] = []

    def permanent(_to: str, _url: str) -> None:
        calls.append("send")
        raise EmailDeliveryError("no transport")

    monkeypatch.setattr("app.auth.password_reset.email_service.send_password_reset", permanent)
    monkeypatch.setattr("app.auth.password_reset.audit_logger.log", lambda **kw: None)

    auth_mail_tasks.send_password_reset_mail.apply(args=(normal_user.id, "203.0.113.5"), throw=True)

    assert calls == ["send"]


def test_the_task_is_routed_to_a_light_queue():
    from app.core.celery_routing import TASK_ROUTES

    for name in (
        "auth_mail.password_reset",
        "auth_mail.email_verification",
        "auth_mail.invitation",
        "auth_mail.security_notice",
    ):
        assert TASK_ROUTES[name]["queue"] == "utility"


def test_an_invitation_with_no_mail_transport_is_refused_before_anything_is_created(
    db_session, super_admin_user, auth_mail_dispatches, monkeypatch
):
    from app.auth.invitations import create_invitation
    from app.core.exceptions import EmailDeliveryError
    from app.models.invitation import UserInvitation
    from app.services import email_service as email_module

    monkeypatch.setattr(email_module.settings, "SMTP_HOST", "")
    monkeypatch.setattr(email_module.email_service, "_designated_config", lambda: None)
    address = _nobody()

    with pytest.raises(EmailDeliveryError):
        create_invitation(
            db_session,
            email=address,
            full_name=None,
            role="user",
            auth_type="local",
            expires_in_hours=24,
            created_by=super_admin_user,
            ip_address="203.0.113.5",
        )

    assert db_session.query(UserInvitation).filter_by(email=address).count() == 0
    assert auth_mail_dispatches == []


def test_an_invitation_dispatches_a_task_carrying_ids_not_the_link(
    db_session, super_admin_user, auth_mail_dispatches, monkeypatch
):
    from app.auth.invitations import create_invitation
    from app.services import email_service as email_module

    monkeypatch.setattr(email_module.settings, "SMTP_HOST", "mail.example.org")
    monkeypatch.setattr(email_module.settings, "FRONTEND_URL", "https://app.example.org")
    address = _nobody()

    invitation, error = create_invitation(
        db_session,
        email=address,
        full_name=None,
        role="user",
        auth_type="local",
        expires_in_hours=24,
        created_by=super_admin_user,
        ip_address="203.0.113.5",
    )

    assert error is None and invitation is not None
    assert auth_mail_dispatches == [("auth_mail.invitation", (invitation.id, 24), {})]


def test_an_email_change_notice_is_queued_not_sent_inline(auth_mail_dispatches):
    from app.services.account_security_service import notify_email_changed

    notify_email_changed("old@example.com", "new@example.com")

    ((name, args, _),) = auth_mail_dispatches
    assert name == "auth_mail.security_notice"
    assert args[0] == "old@example.com"
