"""MSAL reports a refused token as an error dict; the email service turns it into a clean failure.

Test connection must answer ``(False, ...)`` rather than raise, and ``send_email`` must return
``(False, ...)`` so the sender's bounded retry applies. The tenant-bearing ``error_description``
never reaches the returned or persisted message.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import msal

from app.services import watch_email_service as wes

DESCRIPTION = "AADSTS7000215: Invalid client secret for tenant 11111111-2222-3333-4444-555555555555"


class _RefusingApp:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def acquire_token_for_client(self, scopes: list[str]) -> dict[str, str]:
        return {"error": "invalid_client", "error_description": DESCRIPTION}


def _config() -> Any:
    return SimpleNamespace(
        provider="m365",
        from_address="noreply@example.org",
        m365_client_id="client-id",
        m365_tenant_id="tenant-id",
        encrypted_m365_client_secret=None,
    )


def test_connection_test_reports_failure_when_msal_refuses_the_token(monkeypatch, caplog):
    monkeypatch.setattr(msal, "ConfidentialClientApplication", _RefusingApp)

    with caplog.at_level(logging.WARNING):
        ok, message = wes.test_connection(_config())

    assert ok is False
    assert DESCRIPTION not in message
    assert "11111111-2222-3333-4444-555555555555" not in caplog.text


def test_send_returns_failure_when_msal_refuses_the_token(monkeypatch, caplog):
    monkeypatch.setattr(msal, "ConfidentialClientApplication", _RefusingApp)

    with caplog.at_level(logging.WARNING):
        ok, message = wes.send_email(_config(), ["ops@example.com"], "s", "<p>x</p>")

    assert ok is False
    assert DESCRIPTION not in message
    assert "11111111-2222-3333-4444-555555555555" not in caplog.text
