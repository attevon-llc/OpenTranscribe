"""SMTP credentials never cross the wire unencrypted, and plaintext mail only goes local.

Covers the shared policy (``utils/smtp_transport``), both senders (the DB-backed
``watch_email_service`` and the env-configured ``email_service``) and the config schema,
which refuses the same combination when an administrator saves it.
"""

from __future__ import annotations

import smtplib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.exceptions import EmailDeliveryError
from app.schemas.email_notification import EmailConfigCreate
from app.services import email_service as mod
from app.services import watch_email_service as wes
from app.utils.smtp_transport import is_loopback_host
from app.utils.smtp_transport import smtp_transport_problem


@pytest.mark.parametrize(
    ("host", "port", "starttls", "creds", "refused"),
    [
        ("mail.example.org", 587, True, True, False),
        ("mail.example.org", 465, False, True, False),
        ("mail.example.org", 25, False, True, True),
        ("mail.example.org", 25, False, False, True),
        ("localhost", 25, False, False, False),
        ("127.0.0.1", 25, False, False, False),
        ("::1", 25, False, False, False),
        ("localhost", 25, False, True, True),
        ("10.0.0.5", 25, False, False, True),
        ("localhost.evil.example", 25, False, False, True),
    ],
)
def test_policy(host, port, starttls, creds, refused):
    assert (smtp_transport_problem(host, port, starttls, creds) is not None) is refused


def test_loopback_detection_rejects_lookalikes():
    assert is_loopback_host("LocalHost")
    assert not is_loopback_host("127.0.0.1.example.com")
    assert not is_loopback_host(None)


class _FakeSMTP:
    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: int = 0) -> None:
        self.logged_in = False
        self.sent: list[Any] = []
        _FakeSMTP.instances.append(self)

    def starttls(self, context: Any = None) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.logged_in = True

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        self.sent.append(to_addrs)

    def sendmail(self, sender: str, rcpts: list[str], data: Any) -> None:
        self.sent.append(rcpts)

    def quit(self) -> None:
        return None


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(
        smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("implicit TLS not expected here")
    )
    monkeypatch.setattr(wes, "decrypt_api_key", lambda _v: "pw")
    return _FakeSMTP


def _cfg(**over: Any) -> Any:
    base = {
        "provider": "smtp",
        "smtp_host": "mail.example.org",
        "smtp_port": 25,
        "smtp_use_tls": False,
        "smtp_username": "user",
        "encrypted_smtp_password": "enc",
        "from_address": "noreply@example.org",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_config_sender_refuses_credentials_over_plaintext(fake_smtp):
    ok, msg = wes.send_email(_cfg(), ["a@example.org"], "s", "<p>x</p>")
    assert ok is False
    assert "credentials" in msg
    assert fake_smtp.instances == []


def test_config_test_connection_refuses_credentials_over_plaintext(fake_smtp):
    ok, msg = wes.test_connection(_cfg())
    assert ok is False
    assert fake_smtp.instances == []
    assert "credentials" in msg


def test_config_sender_refuses_plaintext_to_a_remote_relay_even_without_credentials(fake_smtp):
    cfg = _cfg(smtp_username=None, encrypted_smtp_password=None)
    ok, _ = wes.send_email(cfg, ["a@example.org"], "s", "<p>x</p>")
    assert ok is False
    assert fake_smtp.instances == []


def test_config_sender_allows_unauthenticated_plaintext_to_loopback(fake_smtp):
    cfg = _cfg(smtp_host="localhost", smtp_username=None, encrypted_smtp_password=None)
    ok, _ = wes.send_email(cfg, ["a@example.org"], "s", "<p>x</p>")
    assert ok is True
    assert len(fake_smtp.instances) == 1


def test_config_sender_still_logs_in_over_starttls(fake_smtp):
    ok, _ = wes.send_email(_cfg(smtp_port=587, smtp_use_tls=True), ["a@example.org"], "s", "x")
    assert ok is True
    assert fake_smtp.instances[0].logged_in is True


def test_env_sender_refuses_credentials_over_plaintext(fake_smtp, monkeypatch):
    monkeypatch.setattr(mod.settings, "SMTP_HOST", "mail.example.org")
    monkeypatch.setattr(mod.settings, "SMTP_PORT", 25)
    monkeypatch.setattr(mod.settings, "SMTP_USER", "user")
    monkeypatch.setattr(mod.settings, "SMTP_PASSWORD", "pw")
    monkeypatch.setattr(mod.settings, "SMTP_USE_TLS", False)
    with pytest.raises(EmailDeliveryError):
        mod.EmailService._send_via_env_smtp("a@example.org", "s", "<p>x</p>", "x")
    assert fake_smtp.instances == []


def _create(**over: Any) -> EmailConfigCreate:
    base: dict[str, Any] = {
        "name": "n",
        "provider": "smtp",
        "from_address": "noreply@example.org",
        "smtp_host": "mail.example.org",
        "smtp_port": 25,
        "smtp_use_tls": False,
        "smtp_username": "user",
        "smtp_password": "pw",
    }
    base.update(over)
    return EmailConfigCreate(**base)


def test_schema_rejects_credentials_without_tls():
    with pytest.raises(ValidationError, match="credentials"):
        _create()


def test_schema_rejects_plaintext_to_a_remote_host():
    with pytest.raises(ValidationError, match="loopback"):
        _create(smtp_username=None, smtp_password=None)


def test_schema_accepts_tls_and_loopback_plaintext():
    assert _create(smtp_port=465)
    assert _create(smtp_port=587, smtp_use_tls=True)
    assert _create(smtp_host="127.0.0.1", smtp_username=None, smtp_password=None)
