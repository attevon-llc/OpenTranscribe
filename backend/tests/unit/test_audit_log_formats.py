"""The audit line format is a SIEM contract: pin the CEF and JSON output byte for byte.

``AuditLogger`` writes every event to the ``audit`` logger as one line, CEF or JSON per
``AUDIT_LOG_FORMAT``, and SIEM parsers key on its exact shape (the CEF header fields, the
severity per outcome, the extension keys, the JSON key order and non-ASCII text kept as-is).
Nothing else pinned that shape; these tests do, so a refactor of ``auth/audit.py`` or of the
event enums it reads (``auth/audit_events.py``) cannot change it silently.
"""

from __future__ import annotations

import json

import pytest

from app.auth import audit as audit_module
from app.auth.audit import AuditLogger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome

_FULL = {
    "event_type": "auth.login.failure",
    "source_ip": "10.0.0.1",
    "user_id": 7,
    "username": "a@example.com",
    "request_id": "req-1",
}


@pytest.mark.parametrize(
    ("outcome", "severity"),
    [("success", 3), ("failure", 7), ("partial", 5), ("something-else", 5), (None, 5)],
)
def test_cef_severity_per_outcome(outcome, severity):
    line = AuditLogger()._format_cef({**_FULL, "outcome": outcome})
    assert line == (
        "CEF:0|OpenTranscribe|AuthService|1.0|auth.login.failure|auth.login.failure|"
        f"{severity}|src=10.0.0.1 duid=7 suser=a@example.com externalId=req-1"
    )


def test_cef_without_extensions_or_event_type():
    assert AuditLogger()._format_cef({"outcome": "success"}) == (
        "CEF:0|OpenTranscribe|AuthService|1.0|unknown|unknown|3|"
    )


@pytest.fixture()
def emitted(monkeypatch):
    """Lines the audit logger writes, with OpenSearch off."""
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_TO_OPENSEARCH", False)
    logger = AuditLogger()
    lines: list[str] = []
    monkeypatch.setattr(logger._logger, "info", lines.append)
    return logger, lines


def test_json_line_key_order_and_values(emitted, monkeypatch):
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_FORMAT", "json")
    logger, lines = emitted

    logger.log(
        AuditEventType.ADMIN_ROLE_CHANGE,
        AuditOutcome.PARTIAL,
        user_id=1,
        username="zoë@example.com",
        source_ip="10.0.0.2",
        user_agent="ua",
        error_code="E1",
        details={"role": "admin"},
        organization_id=9,
        target_user_id=2,
        target_username="bob@example.com",
        resource_type="user",
        resource_uuid="u-2",
        tenant_scope="org",
    )

    (line,) = lines
    assert "zoë@example.com" in line  # ensure_ascii=False: never the \u00eb escape
    event = json.loads(line)
    assert list(event) == [
        "timestamp",
        "request_id",
        "event_type",
        "outcome",
        "user_id",
        "organization_id",
        "username",
        "source_ip",
        "user_agent",
        "error_code",
        "details",
        "target_user_id",
        "target_username",
        "resource_type",
        "resource_uuid",
        "tenant_scope",
    ]
    del event["timestamp"], event["request_id"]
    assert event == {
        "event_type": "admin.role.change",
        "outcome": "partial",
        "user_id": 1,
        "organization_id": 9,
        "username": "zoë@example.com",
        "source_ip": "10.0.0.2",
        "user_agent": "ua",
        "error_code": "E1",
        "details": {"role": "admin"},
        "target_user_id": 2,
        "target_username": "bob@example.com",
        "resource_type": "user",
        "resource_uuid": "u-2",
        "tenant_scope": "org",
    }


def test_cef_format_is_selected_case_insensitively(emitted, monkeypatch):
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_FORMAT", "CEF")
    logger, lines = emitted

    logger.log(AuditEventType.AUTH_LOGOUT, AuditOutcome.SUCCESS, user_id=3, source_ip="10.0.0.3")

    (line,) = lines
    head, request_id = line.split("externalId=")
    assert (
        head
        == "CEF:0|OpenTranscribe|AuthService|1.0|auth.logout|auth.logout|3|src=10.0.0.3 duid=3 "
    )
    assert request_id
