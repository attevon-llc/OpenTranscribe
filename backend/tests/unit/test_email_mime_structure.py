"""Outbound mail is well-formed: dated, identified, multipart/alternative, one recipient each.

The messages are parsed back with the ``email`` package, so these assert on the structure
a mail client would see rather than on how it was built.
"""

from __future__ import annotations

import base64
import email
import smtplib
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
import requests
from pydantic import ValidationError

from app.schemas.email_notification import EmailConfigCreate
from app.schemas.email_notification import EmailConfigUpdate
from app.services import email_service as mod
from app.services import watch_email_service as wes

SENDER = "OpenTranscribe <noreply@mail.example.org>"


def _parts(msg: Any) -> list[tuple[str, str]]:
    return [(p.get_content_type(), p.get_content()) for p in msg.iter_parts()]


def test_build_message_is_multipart_alternative_with_text_first():
    msg = wes.build_message(SENDER, "a@example.com", "s", "<p>hi</p>", "hi")
    assert msg.get_content_type() == "multipart/alternative"
    kinds = [kind for kind, _ in _parts(msg)]
    assert kinds == ["text/plain", "text/html"]


def test_build_message_has_date_and_a_message_id_in_the_sender_domain():
    msg = wes.build_message(SENDER, "a@example.com", "s", "<p>hi</p>", "hi")
    assert parsedate_to_datetime(msg["Date"]).tzinfo is not None
    msgid = msg["Message-ID"]
    assert msgid.startswith("<") and msgid.endswith("@mail.example.org>")


def test_message_ids_are_unique_per_message():
    ids = {
        wes.build_message(SENDER, "a@example.com", "s", "h", "t")["Message-ID"] for _ in range(5)
    }
    assert len(ids) == 5


class _RecordingSMTP:
    sent: list[EmailMessage] = []
    envelopes: list[list[str]] = []

    def __init__(self, host: str, port: int, timeout: int = 0) -> None:
        pass

    def ehlo(self) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        return None

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        type(self).sent.append(msg)
        type(self).envelopes.append(list(to_addrs))

    def quit(self) -> None:
        return None


@pytest.fixture
def smtp(monkeypatch):
    _RecordingSMTP.sent = []
    _RecordingSMTP.envelopes = []
    monkeypatch.setattr(smtplib, "SMTP", _RecordingSMTP)
    return _RecordingSMTP


def _cfg(**over: Any) -> Any:
    base = {
        "provider": "smtp",
        "smtp_host": "mail.example.org",
        "smtp_port": 587,
        "smtp_use_tls": True,
        "smtp_username": None,
        "encrypted_smtp_password": None,
        "from_address": SENDER,
        "name": "cfg",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_watch_mail_is_one_message_per_recipient(smtp):
    recipients = ["a@example.com", "b@example.com", "c@example.com"]
    ok, _ = wes.send_email(_cfg(), recipients, "s", "<p>x</p>", text_body="x")
    assert ok is True
    assert smtp.envelopes == [[r] for r in recipients]
    for msg, rcpt in zip(smtp.sent, recipients, strict=True):
        assert msg["To"] == rcpt
        assert msg["Cc"] is None and msg["Bcc"] is None
        assert msg.get_content_type() == "multipart/alternative"
    assert len({m["Message-ID"] for m in smtp.sent}) == 3


def test_db_config_path_sends_the_plain_text_part(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_send(config, recipients, subject, html_body, timeout=30, text_body=None):
        seen.update(text=text_body, html=html_body)
        return True, "ok"

    monkeypatch.setattr(wes, "send_email", fake_send)
    mod.EmailService._send_via_config(
        cast(Any, SimpleNamespace(name="cfg")), "a@example.com", "subject", "<p>h</p>", "plain body"
    )
    assert seen["text"] == "plain body"


def test_env_path_message_is_dated_identified_and_multipart(smtp, monkeypatch):
    monkeypatch.setattr(mod.settings, "SMTP_HOST", "mail.example.org")
    monkeypatch.setattr(mod.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(mod.settings, "SMTP_USER", "")
    monkeypatch.setattr(mod.settings, "SMTP_FROM", "noreply@example.org")
    monkeypatch.setattr(mod.settings, "SMTP_USE_TLS", True)

    mod.EmailService._send_via_env_smtp("a@example.com", "s", "<p>h</p>", "plain")

    (msg,) = smtp.sent
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Date"] and msg["Message-ID"].endswith("@example.org>")
    assert [k for k, _ in _parts(msg)] == ["text/plain", "text/html"]


def test_graph_sends_mime_per_recipient_and_encodes_the_sender(monkeypatch):
    monkeypatch.setattr(wes, "_m365_token", lambda _c: "tok")
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, headers: dict[str, str], data: bytes, timeout: int) -> Any:
        calls.append({"url": url, "headers": headers, "data": data})
        return SimpleNamespace(status_code=202, text="")

    monkeypatch.setattr(requests, "post", fake_post)
    cfg = _cfg(provider="m365", from_address="odd name+tag@example.org")

    ok, _ = wes.send_email(cfg, ["a@example.com", "b@example.com"], "s", "<p>h</p>", text_body="t")

    assert ok is True
    assert len(calls) == 2
    for call, rcpt in zip(calls, ["a@example.com", "b@example.com"], strict=True):
        assert "/users/odd%20name%2Btag%40example.org/sendMail" in call["url"]
        assert call["headers"]["Content-Type"] == "text/plain"
        parsed = email.message_from_bytes(base64.b64decode(call["data"]), policy=policy.default)
        assert parsed.get_content_type() == "multipart/alternative"
        assert parseaddr(parsed["To"])[1] == rcpt
        assert parsed["Date"] and parsed["Message-ID"]


def test_scan_summary_has_a_plain_text_form():
    text = wes.build_scan_summary_text("My <source>", {"found": 3, "imported": 2})
    assert "My <source>" in text
    assert "Files found: 3" in text


def _create(**over: Any) -> EmailConfigCreate:
    base: dict[str, Any] = {
        "name": "n",
        "provider": "m365",
        "from_address": "noreply@example.org",
        "m365_tenant_id": "t",
        "m365_client_id": "c",
    }
    base.update(over)
    return EmailConfigCreate(**base)


@pytest.mark.parametrize(
    "bad", ["not-an-address", "a@example.com, nope", "a@", "a@example.com\r\nBcc: x@y.z"]
)
def test_schema_rejects_recipients_that_are_not_addresses(bad):
    with pytest.raises(ValidationError):
        _create(default_recipients=bad)
    with pytest.raises(ValidationError):
        EmailConfigUpdate(default_recipients=bad)


def test_schema_accepts_a_csv_of_addresses():
    cfg = _create(default_recipients="a@example.com,  b@example.org")
    assert cfg.default_recipients == "a@example.com, b@example.org"


def test_schema_rejects_a_bad_from_address():
    with pytest.raises(ValidationError):
        _create(from_address="nope")
