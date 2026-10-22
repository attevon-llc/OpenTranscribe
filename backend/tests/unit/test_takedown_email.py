"""The takedown email: escaped, never logged, honest about having no transport (issue #1254)."""

from __future__ import annotations

import logging

import pytest

from app.core.config import settings
from app.core.enums import TakedownNotifyEvent
from app.core.exceptions import EmailDeliveryError
from app.services import takedown_notification_service as notify_mod
from app.services.email_service import EmailService

HOSTILE_TITLE = '<script>alert("t")</script> & "Quarterly" <b>'
HOSTILE_REASON = "<img src=x onerror=alert(1)> & more"


@pytest.fixture
def smtp(monkeypatch):
    """Env SMTP configured, the transport itself captured."""
    sent = []
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(EmailService, "_designated_config", lambda self: None)
    monkeypatch.setattr(
        EmailService,
        "_send_via_env_smtp",
        staticmethod(
            lambda to, subject, html_body, text_body: sent.append(
                (to, subject, html_body, text_body)
            )
        ),
    )
    return sent


def test_every_interpolated_value_is_html_escaped(smtp):
    EmailService().send_takedown_notice(
        "owner@example.com",
        'Files <taken> & "down"',
        "Heading <u>x</u>",
        [f"- {HOSTILE_TITLE}: {HOSTILE_REASON}", "Dispute: a&b@example.com"],
    )

    (_to, subject, html_body, text_body) = smtp[0]
    for raw in ("<script>", "<img", "<b>", "<u>x</u>", "onerror=alert(1)>"):
        assert raw not in html_body, raw
    assert "&lt;script&gt;" in html_body and "&amp; more" in html_body
    assert "&lt;u&gt;x&lt;/u&gt;" in html_body
    assert "a&amp;b@example.com" in html_body
    assert HOSTILE_TITLE in text_body, "the plain-text part is not HTML and stays literal"


def test_the_notice_does_not_say_the_recipient_made_the_change(smtp):
    EmailService().send_takedown_notice("o@example.com", "s", "h", ["p"])
    assert "did not make this change" not in smtp[0][2]


def test_has_transport_follows_the_designation_and_env_smtp(monkeypatch):
    monkeypatch.setattr(EmailService, "_designated_config", lambda self: None)
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    assert EmailService().has_transport() is False
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
    assert EmailService().has_transport() is True
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(EmailService, "_designated_config", lambda self: object())
    assert EmailService().has_transport() is True


def test_with_no_transport_the_body_is_never_logged_and_the_send_raises(monkeypatch, caplog):
    monkeypatch.setattr(EmailService, "_designated_config", lambda self: None)
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(EmailDeliveryError):
        EmailService().send_takedown_notice(
            "owner@example.com",
            "Files taken down",
            "Heading",
            [f"- {HOSTILE_TITLE}: SECRET-REASON"],
        )

    assert "SECRET-REASON" not in caplog.text
    assert "Quarterly" not in caplog.text
    assert "owner@example.com" not in caplog.text


def test_a_transport_that_rejects_the_message_raises_without_the_body(monkeypatch, caplog, smtp):
    def boom(to, subject, html_body, text_body):
        raise OSError("connection refused")

    monkeypatch.setattr(EmailService, "_send_via_env_smtp", staticmethod(boom))
    with pytest.raises(OSError):
        EmailService().send_takedown_notice("o@example.com", "s", "h", ["SECRET-REASON"])
    assert "SECRET-REASON" not in caplog.text


@pytest.mark.parametrize("organization", [False, True])
def test_the_dispute_route_matches_who_placed_the_takedown(monkeypatch, organization):
    monkeypatch.setattr(settings, "ABUSE_CONTACT_EMAIL", "abuse@operator.example")
    _subject, _heading, paragraphs = notify_mod._compose(
        TakedownNotifyEvent.QUARANTINED, organization, [("A file", "the reason")]
    )
    text = " ".join(paragraphs)
    assert "the reason" in text
    if organization:
        assert "organization's administrators" in text and "abuse@operator.example" not in text
    else:
        assert "abuse@operator.example" in text
