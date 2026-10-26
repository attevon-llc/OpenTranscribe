"""The super-admin audit export shares its reader and writer with the org export (#1277).

``tests/test_admin_endpoints.py`` skips its export tests whenever the OpenSearch audit sink is off
(conftest forces it off), so nothing else covers what the route does with the rows. Here the
reader is replaced, and what is pinned is the shared writer's output, the cap passed to the query,
the truncation header and the export's own audit event.
"""

from __future__ import annotations

import csv
import io

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.auth.audit_export import EXPORT_COLUMNS
from app.core.config import settings

EXPORT = "/api/admin/audit-logs/export"


def _stub(monkeypatch, logs, total):
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return {"logs": logs, "total": total, "offset": 0, "limit": kwargs["limit"]}

    monkeypatch.setattr("app.auth.audit.query_audit_logs", _fake)
    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", True)
    return calls


def test_the_platform_export_uses_the_shared_columns_cap_and_audit(
    client, super_admin_token_headers, super_admin_user, monkeypatch
):
    event = {
        "timestamp": "2026-10-25T12:00:00Z",
        "event_type": "auth.login.success",
        "outcome": "success",
        "username": "=cmd()",
        "resource_type": "media_file",
        "details": {"k": "v"},
    }
    calls = _stub(monkeypatch, [event], total=10_001)
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))

    response = client.get(
        EXPORT, params={"export_format": "csv"}, headers=super_admin_token_headers
    )

    assert response.status_code == 200, response.text
    assert response.headers["x-export-truncated"] == "true"
    reader = csv.DictReader(io.StringIO(response.text))
    assert tuple(reader.fieldnames or ()) == EXPORT_COLUMNS
    row = next(reader)
    assert row["username"] == "'=cmd()"
    assert row["resource_type"] == "media_file"
    assert calls[0]["limit"] == 10_000 and "scope_org_id" not in calls[0]
    exported = [e for e in written if e["event_type"] is AuditEventType.AUDIT_EXPORT]
    assert len(exported) == 1
    assert exported[0]["details"] == {
        "scope": "platform",
        "format": "csv",
        "rows": 1,
        "truncated": True,
    }
    assert exported[0]["user_id"] == super_admin_user.id


def test_an_unknown_platform_format_is_still_a_400(client, super_admin_token_headers, monkeypatch):
    _stub(monkeypatch, [], total=0)
    response = client.get(
        EXPORT, params={"export_format": "xml"}, headers=super_admin_token_headers
    )
    assert response.status_code == 400
