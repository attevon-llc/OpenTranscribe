"""A rejected Microsoft Graph ``sendMail`` is logged with its status and nothing else."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import requests

from app.services import watch_email_service as wes

BODY_SECRET = "tenant-detail-and-ops@example.com-in-the-response-body"


def test_non_2xx_is_logged_by_status_without_the_body(monkeypatch, caplog):
    monkeypatch.setattr(wes, "_m365_token", lambda _cfg: "tok")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=403, text=BODY_SECRET),
    )
    cfg: Any = SimpleNamespace(provider="m365", from_address="noreply@example.org")

    with caplog.at_level(logging.WARNING):
        ok, message = wes.send_email(cfg, ["ops@example.com"], "s", "<p>x</p>")

    assert ok is False
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "403" in logged
    assert BODY_SECRET not in logged
    assert BODY_SECRET not in message
