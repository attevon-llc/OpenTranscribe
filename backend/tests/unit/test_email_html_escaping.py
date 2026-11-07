"""Every value interpolated into an outbound HTML body is escaped.

A watch-source name, an inviter address and a security-notice message all come from a
person, and the rendered HTML is read in a mail client, so markup in any of them must
arrive as text.
"""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest

from app.services import email_service as mod
from app.services.email_service import EmailService
from app.services.watch_email_service import build_scan_summary_html

PAYLOAD = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


class _Capture:
    def __init__(self) -> None:
        self.html = ""
        self.text = ""

    def send(self, to: str, subject: str, html_body: str, text_body: str, **_: Any) -> None:
        self.html, self.text = html_body, text_body


@pytest.fixture
def service(monkeypatch) -> tuple[EmailService, _Capture]:
    cap = _Capture()
    svc = EmailService(session_factory=lambda: cast(Any, None))
    monkeypatch.setattr(svc, "_send_email", cap.send)
    return svc, cap


def test_scan_summary_escapes_source_name():
    body = build_scan_summary_html(PAYLOAD, {"found": 1})
    assert "<script>" not in body
    assert ESCAPED in body


def test_scan_summary_escapes_summary_values():
    body = build_scan_summary_html("ok", {"found": PAYLOAD})
    assert "<script>" not in body
    assert ESCAPED in body


def test_security_notice_escapes_message_and_subject(service):
    svc, cap = service
    svc.send_security_notice("a@example.com", PAYLOAD, PAYLOAD)
    assert "<script>" not in cap.html
    assert cap.html.count(ESCAPED) == 2


def test_invitation_escapes_inviter(service):
    svc, cap = service
    svc.send_invitation("a@example.com", "https://x.example/a?t=1", PAYLOAD, 72, True)
    assert "<script>" not in cap.html
    assert ESCAPED in cap.html


def test_link_is_escaped_in_the_attribute(service):
    svc, cap = service
    svc.send_password_reset("a@example.com", 'https://x.example/r?t=1"><script>')
    assert '"><script>' not in cap.html
    assert "&quot;&gt;&lt;script&gt;" in cap.html


def test_project_name_is_escaped(service, monkeypatch):
    svc, cap = service
    monkeypatch.setattr(mod.settings, "PROJECT_NAME", PAYLOAD)
    svc.send_email_verification("a@example.com", "https://x.example/v?t=1", 24)
    assert "<script>" not in cap.html
