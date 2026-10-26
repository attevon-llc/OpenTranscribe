"""A finished DOCX / PDF export is one ``content.export`` audit event (issues #1278, #1277).

The synchronous export records the event when the bytes exist. The document worker has no
request, so it records the same event itself after the upload, with no client address, and only
while content auditing is on. A refused export wrote no document, so it records nothing.
"""

from __future__ import annotations

import pytest
from fixtures.redaction_export_world import make_redactable_file

from app.auth.audit import TENANT_SCOPE_PERSONAL
from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
from app.services import content_audit
from app.services.system_settings_service import set_setting

# The audit switch is a SystemSettings key shared by every test that flips it.
pytestmark = pytest.mark.xdist_group("content_audit_settings")


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


def _exports(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event_type"] is AuditEventType.CONTENT_EXPORT]


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
def test_a_finished_document_is_one_content_export(
    client, user_token_headers, normal_user, db_session, document_export, events, monkeypatch, fmt
):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: True)
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, True)
    media_file = make_redactable_file(db_session, normal_user)

    document_export.export(client, user_token_headers, media_file, format=fmt)

    [event] = _exports(events)
    assert event["user_id"] == normal_user.id
    assert event["resource_type"] == "media_file"
    assert event["resource_uuid"] == str(media_file.uuid)
    assert event["tenant_scope"] == TENANT_SCOPE_PERSONAL
    assert event["details"] == {"surface": "transcript", "format": fmt, "file_count": 1}


def test_nothing_is_recorded_while_content_auditing_is_off(
    client, user_token_headers, normal_user, db_session, document_export, events, monkeypatch
):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: True)
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, False)
    media_file = make_redactable_file(db_session, normal_user)

    document_export.export(client, user_token_headers, media_file, format="docx")

    assert _exports(events) == []
