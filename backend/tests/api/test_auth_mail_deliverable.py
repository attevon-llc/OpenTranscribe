"""The admin UI's "can auth mail be delivered" answer must match the sender's.

``GET /api/admin/auth-config/email/designation`` reports ``deliverable`` so the
admin screens can say "password resets and invitations will not be sent" before
anyone finds out the hard way. That answer is only worth showing if it is the
*same* answer ``EmailService._send_email`` acts on: a UI that re-derived it from
``SMTP_HOST`` alone, or from the designation alone, would be green while the
sender raised (or red while it delivered).

So the core test here drives the real sender and the real description from the
same rows and asserts they agree, across every designation state crossed with
env SMTP set / unset. The transports themselves are stubbed to a no-op: the
question is whether a message is *handed to* one, not whether a mail server
answers.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import status

from app.api.endpoints import auth_email_delivery
from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.models.email_notification_config import EmailNotificationConfig
from app.models.system_settings import SystemSettings
from app.services import auth_mail_config_service as designation
from app.services import email_service as email_mod
from app.services.email_service import EmailDeliveryError
from app.services.email_service import EmailService

DESIGNATION = "/api/admin/auth-config/email/designation"
RESET_URL = "https://transcribe.example.org/reset-password?token=abc"


class _KeepOpen:
    """Hands the sender the test's session, ignoring its ``close()``.

    ``EmailService._designated_config`` opens and closes its own session; closing
    the savepoint-bound test session would end the test's transaction.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def close(self) -> None:
        pass


def _config(db_session, *, enabled: bool) -> EmailNotificationConfig:
    config = EmailNotificationConfig(
        name=f"smtp-{uuid_pkg.uuid4().hex[:8]}",
        provider="smtp",
        smtp_host="smtp.invalid",
        smtp_port=587,
        from_address="noreply@example.com",
        is_enabled=enabled,
    )
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)
    return config


def _set_designation_raw(db_session, value: str) -> None:
    """Write the setting directly, bypassing ``set_designation``'s validation.

    The validated writer refuses a missing or disabled target, and those are the
    exact dangling states this test has to reach.
    """
    db_session.query(SystemSettings).filter(
        SystemSettings.key == AUTH_EMAIL_CONFIG_SETTING_KEY
    ).delete(synchronize_session=False)
    db_session.add(SystemSettings(key=AUTH_EMAIL_CONFIG_SETTING_KEY, value=value))
    db_session.commit()


def _arrange(db_session, state: str) -> None:
    if state == "not_designated":
        _set_designation_raw(db_session, "")
    elif state == "active":
        _set_designation_raw(db_session, str(_config(db_session, enabled=True).uuid))
    elif state == "disabled":
        _set_designation_raw(db_session, str(_config(db_session, enabled=False).uuid))
    elif state == "missing":
        _set_designation_raw(db_session, str(uuid_pkg.uuid4()))
    else:  # pragma: no cover - guards a typo in the parametrization
        raise AssertionError(state)


@pytest.mark.parametrize("env_smtp", [True, False], ids=["env_smtp", "no_env_smtp"])
@pytest.mark.parametrize("state", ["not_designated", "active", "disabled", "missing"])
def test_deliverable_agrees_with_what_the_sender_does(db_session, monkeypatch, state, env_smtp):
    """``deliverable`` is true exactly when a password reset would not raise.

    Catches a description computed from ``SMTP_HOST`` alone (wrong for a working
    designation with no env SMTP), from the designation alone (wrong for a dangling
    one with env SMTP), or from "a row is designated" (wrong for disabled/missing).
    Eight rows; any of those shortcuts disagrees with the sender on at least two.
    """
    monkeypatch.setattr(email_mod.settings, "SMTP_HOST", "mail.example.org" if env_smtp else "")
    _arrange(db_session, state)

    sent: list[str] = []
    service = EmailService(session_factory=lambda: _KeepOpen(db_session))  # type: ignore[arg-type,return-value]
    monkeypatch.setattr(service, "_send_via_config", lambda *a, **k: sent.append("config"))
    monkeypatch.setattr(service, "_send_via_env_smtp", lambda *a, **k: sent.append("env"))

    try:
        service.send_password_reset("someone@example.com", RESET_URL)
        raised = False
    except EmailDeliveryError:
        raised = True

    described = designation.describe_designation(db_session)

    assert described.status == state
    assert described.deliverable is (not raised)
    assert bool(sent) is described.deliverable


def test_designation_route_reports_deliverable_and_verification(
    client, super_admin_token_headers, db_session, monkeypatch
):
    """The route exposes both derived flags, and they follow their sources.

    ``verification_required`` is what lets the UI escalate: with it on and no
    transport, new local users cannot log in at all, which is worse than a reset
    mail not arriving.
    """
    monkeypatch.setattr(designation.settings, "SMTP_HOST", "")
    _set_designation_raw(db_session, "")
    monkeypatch.setattr(auth_email_delivery, "email_verification_required", lambda _db: True)

    body = client.get(DESIGNATION, headers=super_admin_token_headers).json()
    assert body["deliverable"] is False
    assert body["verification_required"] is True

    monkeypatch.setattr(auth_email_delivery, "email_verification_required", lambda _db: False)
    monkeypatch.setattr(designation.settings, "SMTP_HOST", "mail.example.org")

    response = client.get(DESIGNATION, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["deliverable"] is True
    assert body["verification_required"] is False


def test_the_put_response_carries_the_same_flags(
    client, super_admin_token_headers, db_session, monkeypatch
):
    """Saving a designation must refresh the flags, not just the status.

    The panel rebinds its state from the PUT response; if only the GET carried
    ``deliverable`` the warning would stay up after the operator fixed the problem.
    """
    monkeypatch.setattr(designation.settings, "SMTP_HOST", "")
    _set_designation_raw(db_session, "")
    config = _config(db_session, enabled=True)

    response = client.put(
        DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": str(config.uuid)}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deliverable"] is True
