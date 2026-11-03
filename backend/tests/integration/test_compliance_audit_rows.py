"""Each compliance action writes an audit row a real OpenSearch accepts and an admin can read.

The register of which route writes which event is ``fixtures/compliance_audit_routes.py``; its
static half (``unit/test_compliance_audit_route_coverage.py``) fails on a new compliance route
nobody classified and on a removed emitter. This half drives every route the register marks
``live`` through the real app (``support_world``: two organizations, real tokens, the real
dependency chain) and lets ``AuditLogger.log`` run for real against the stack's OpenSearch, into
ONE throwaway index with the production audit mapping, shared by every case in the module the
way a month of production events share one index. Then it reads the event back through
``query_audit_logs`` (the function behind both audit screens and both exports):

* the event exists, with a non-empty integer actor (``user_id``) equal to who acted;
* its target (the file, hold, grant or person acted on) is the right one;
* an organization's event is visible to that organization's admin through the org scope.

A write OpenSearch rejects (a mapping conflict between two event shapes) falls back to a file
and never reaches the index, so it fails here instead of vanishing.

Quarantine/release are asserted on actor + target + event name ONLY: their payload is being
reshaped on another branch, and the target is accepted from ``resource_uuid`` or
``details.file_uuid`` so this stays valid across that change.

Run against an isolated stack, never shared data::

    POSTGRES_PORT=5876 OPENSEARCH_PORT=5880 MINIO_PORT=5878 REDIS_PORT=5877 REDIS_PASSWORD=... \\
        pytest tests/integration/test_compliance_audit_rows.py -m integration -o addopts=""
"""

from __future__ import annotations

import os
import uuid as uuid_pkg
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fixtures.compliance_audit_routes import AUDITED_ROUTES
from fixtures.redaction_export_world import make_redactable_file

from app.auth.audit import AuditEventType
from app.models.organization import OrganizationMembership
from tests.fixtures.support_world import make_user

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    # One shared index and one SystemSettings audit switch: keep the module on one worker.
    pytest.mark.xdist_group("compliance_audit_rows"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason="No OpenSearch reachable (SKIP_OPENSEARCH): the audit row is written to a cluster.",
    ),
]

ALL_ON = {
    "organizations": True,
    "org.takedown": True,
    "org.legal_hold": True,
    "org.audit_logs": True,
    "audit.logs": True,
    "audit.content_access": True,
    "exports.documents": True,
}
#: What this module allows the calls AROUND the write: creating, refreshing and deleting the
#: throwaway index. Measured 2026-11-01 on a loaded host (load average 40-80): creating one empty
#: index took 29 s while the cluster manager worked through queued put-mapping tasks. The audit
#: WRITE keeps the production 2 s deadline (``_AUDIT_WRITE_TIMEOUT_SECONDS``): it is the thing
#: under test, and a write that misses it lands in the fallback file in production too.
OPENSEARCH_CALL_SECONDS = 180
#: The write deadline the test runs the writer with: the production value unless an operator
#: on an overloaded host says otherwise. Measured 2026-11-01 at load average 45: 38 of ~40
#: writes missed the 2 s deadline (ConnectionTimeout, read timeout=2) and every case failed
#: for the host, not the code. Raising it here never hides a type clash or a missing emitter:
#: those are a 400 or no call at all, and the negative control still has to find nothing.
WRITE_TIMEOUT_OVERRIDE = os.environ.get("OT_TEST_AUDIT_WRITE_TIMEOUT_SECONDS")
HOLD_REASON = "Litigation matter 2026-77 (compliance audit test)"


# --------------------------------------------------------------------------- the sink


@pytest.fixture(scope="module")
def audit_index() -> Iterator[str]:
    from opensearchpy import OpenSearch

    from app.auth.audit import AuditLogger
    from app.core.opensearch_auth import opensearch_connection_kwargs

    client = OpenSearch(**opensearch_connection_kwargs(timeout=OPENSEARCH_CALL_SECONDS))
    name = f"test_compliance_audit_{uuid_pkg.uuid4().hex[:12]}"
    # The production creation path (mapping, settings, typed fields), not a copy of it.
    AuditLogger()._ensure_index(client, name)
    try:
        yield name
    finally:
        client.indices.delete(index=name, ignore=[404], request_timeout=OPENSEARCH_CALL_SECONDS)


@pytest.fixture
def audited(audit_index, monkeypatch, db_session):
    """The real audit writer pointed at the throwaway index, content auditing switched on."""
    from app.auth import audit
    from app.core.capabilities import COMMUNITY_CAPABILITIES
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver
    from app.core.config import settings
    from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
    from app.services import content_audit
    from app.services.system_settings_service import set_setting

    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_FALLBACK_ENABLED", False)
    monkeypatch.setattr(audit, "_audit_index_name", lambda: audit_index)
    # The fixture already ran the production ``_ensure_index``; the writer need not repeat it.
    monkeypatch.setattr(audit.audit_logger, "_known_indices", {audit_index})
    if WRITE_TIMEOUT_OVERRIDE:
        monkeypatch.setattr(audit, "_AUDIT_WRITE_TIMEOUT_SECONDS", int(WRITE_TIMEOUT_OVERRIDE))
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: True)
    set_capability_resolver(lambda _r: {**COMMUNITY_CAPABILITIES, **ALL_ON})
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, True)
    try:
        yield audit_index
    finally:
        reset_capability_resolver()


def _read(index: str, **filters: Any) -> list[dict[str, Any]]:
    from app.auth.audit import query_audit_logs
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    assert client is not None
    client.indices.refresh(index=index, request_timeout=OPENSEARCH_CALL_SECONDS)
    result = query_audit_logs(index_pattern=index, limit=500, **filters)
    assert "error" not in result, result
    logs: list[dict[str, Any]] = result["logs"]
    return logs


# --------------------------------------------------------------------------- the cases


@dataclass(frozen=True)
class Expect:
    """What one action must have written."""

    event: AuditEventType
    actor_id: int
    #: (field, value): a top-level field, or ``details.<path>``; several alternatives allowed.
    target: tuple[tuple[str, Any], ...]
    #: The organization whose admins must see the event through the org scope.
    org_id: int | None = None


def _field(doc: dict[str, Any], path: str) -> Any:
    value: Any = doc
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _hits_target(doc: dict[str, Any], target: tuple[tuple[str, Any], ...]) -> bool:
    for path, want in target:
        got = _field(doc, path)
        if got == want or (isinstance(got, list) and want in got):
            return True
    return False


def _detached(db, media_file) -> Any:
    """No storage object behind the file: object tagging stays a no-op on any branch."""
    media_file.storage_path = ""
    db.commit()
    return media_file


def _member(w, label: str) -> Any:
    user = make_user(w.db, role="user", label=label)
    w.db.add(OrganizationMembership(organization_id=w.org.id, user_id=user.id, role="org:member"))
    w.db.commit()
    w.roles[user.id] = (w.org.id, "org:member")
    return user


def _ok(response, code: int = 200) -> Any:
    assert response.status_code == code, response.text
    return (
        response.json()
        if response.content and "json" in response.headers.get("content-type", "")
        else None
    )


def case_quarantine(w, h) -> Expect:
    f = _detached(w.db, w.file)
    _ok(
        w.client.post(
            f"/api/admin/files/{f.uuid}/quarantine",
            headers=w.admin_headers,
            json={"reason": "dmca-1", "legal_hold": True, "operator_action_reason": "test"},
        )
    )
    file_target = (("resource_uuid", str(f.uuid)), ("details.file_uuid", str(f.uuid)))
    return Expect(AuditEventType.ADMIN_FILE_QUARANTINE, w.admin.id, file_target, w.org.id)


def case_release(w, h) -> Expect:
    case_quarantine(w, h)
    f = w.file
    _ok(
        w.client.post(
            f"/api/admin/files/{f.uuid}/release",
            headers=w.admin_headers,
            json={"clear_legal_hold": True},
        )
    )
    file_target = (("resource_uuid", str(f.uuid)), ("details.file_uuid", str(f.uuid)))
    return Expect(AuditEventType.ADMIN_FILE_RELEASE, w.admin.id, file_target, w.org.id)


def case_org_quarantine(w, h) -> Expect:
    f = _detached(w.db, w.file)
    _ok(
        w.client.post(
            f"/api/org-admin/files/{f.uuid}/quarantine",
            headers=w.org_admin_headers,
            json={"reason": "org notice"},
        )
    )
    file_target = (("resource_uuid", str(f.uuid)), ("details.file_uuid", str(f.uuid)))
    return Expect(AuditEventType.ADMIN_FILE_QUARANTINE, w.org_admin.id, file_target, w.org.id)


def case_org_release(w, h) -> Expect:
    case_org_quarantine(w, h)
    f = w.file
    _ok(
        w.client.post(
            f"/api/org-admin/files/{f.uuid}/release", headers=w.org_admin_headers, json={}
        )
    )
    file_target = (("resource_uuid", str(f.uuid)), ("details.file_uuid", str(f.uuid)))
    return Expect(AuditEventType.ADMIN_FILE_RELEASE, w.org_admin.id, file_target, w.org.id)


def _place_hold(w) -> str:
    body = _ok(
        w.client.post(
            "/api/org-admin/holds",
            headers=w.org_admin_headers,
            json={"scope": "file", "file_uuids": [str(w.file.uuid)], "reason": HOLD_REASON},
        ),
        201,
    )
    return str(body["uuid"])


def case_hold_place(w, h) -> Expect:
    _place_hold(w)
    # The held FILE must be named: the hold row's file links cascade away with the file, so the
    # audit trail is the only record of what a hold covered once the file is gone.
    target = (("details.file_uuids", str(w.file.uuid)),)
    return Expect(AuditEventType.CONTENT_HOLD_PLACE, w.org_admin.id, target, w.org.id)


def case_hold_release(w, h) -> Expect:
    hold_uuid = _place_hold(w)
    _ok(
        w.client.post(
            f"/api/org-admin/holds/{hold_uuid}/release",
            headers=w.org_admin_headers,
            json={"reason": HOLD_REASON},
        )
    )
    target = (("details.file_uuids", str(w.file.uuid)),)
    return Expect(AuditEventType.CONTENT_HOLD_RELEASE, w.org_admin.id, target, w.org.id)


def case_transcript_export(w, h) -> Expect:
    f = make_redactable_file(w.db, w.owner)
    _ok(w.client.get(f"/api/files/{f.uuid}/export?format=txt", headers=w.owner_headers))
    return Expect(AuditEventType.CONTENT_EXPORT, w.owner.id, (("resource_uuid", str(f.uuid)),))


def case_subtitles_export(w, h) -> Expect:
    f = make_redactable_file(w.db, w.owner)
    _ok(w.client.get(f"/api/files/{f.uuid}/subtitles?subtitle_format=srt", headers=w.owner_headers))
    return Expect(AuditEventType.CONTENT_EXPORT, w.owner.id, (("resource_uuid", str(f.uuid)),))


def case_document_export(w, h) -> Expect:
    f = make_redactable_file(w.db, w.owner)
    for fmt in ("docx", "pdf"):
        h.document_export.export(w.client, w.owner_headers, f, format=fmt)
    return Expect(AuditEventType.CONTENT_EXPORT, w.owner.id, (("resource_uuid", str(f.uuid)),))


def case_privacy_purge(w, h) -> Expect:
    _ok(
        w.client.put(
            "/api/privacy/me",
            headers=w.owner_headers,
            json={"voiceprint_matching_enabled": False},
        )
    )
    _ok(
        w.client.post(
            "/api/privacy/me/purge",
            headers=w.owner_headers,
            json={"targets": ["voiceprints"], "confirm": "PURGE"},
        ),
        202,
    )
    target = (("details.purge_scope.user_id", w.owner.id),)
    return Expect(AuditEventType.PRIVACY_VOICEPRINT_PURGE, w.owner.id, target)


def case_grant_request(w, h) -> Expect:
    grant = w.request()
    target = (("details.grant_uuid", str(grant["uuid"])),)
    return Expect(AuditEventType.SUPPORT_ACCESS_REQUESTED, w.admin.id, target, w.org.id)


def case_grant_approve(w, h) -> Expect:
    grant = w.request()
    w.approve(grant["uuid"])
    target = (("target_user_id", w.admin.id),)
    return Expect(AuditEventType.SUPPORT_ACCESS_APPROVED, w.org_admin.id, target, w.org.id)


def case_grant_revoke(w, h) -> Expect:
    grant_uuid = w.active_grant()
    _ok(
        w.client.post(
            f"/api/support-access/grants/{grant_uuid}/revoke", headers=w.admin_headers, json={}
        )
    )
    target = (("target_user_id", w.admin.id),)
    return Expect(AuditEventType.SUPPORT_ACCESS_REVOKED, w.admin.id, target, w.org.id)


def case_grant_use(w, h) -> Expect:
    grant_uuid = w.active_grant()
    _ok(w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid)))
    # The typed top-level field, so "everything that happened to this file" finds staff access.
    target = (("resource_uuid", str(w.file.uuid)),)
    return Expect(AuditEventType.SUPPORT_ACCESS_USED, w.admin.id, target, w.org.id)


def case_gdpr_erase(w, h) -> Expect:
    victim = make_user(w.db, role="user", label="erase")
    _ok(w.client.post(f"/api/admin/gdpr/erase-user/{victim.uuid}", headers=w.super_headers))
    target = (("target_user_id", victim.id),)
    return Expect(AuditEventType.ADMIN_USER_DELETE, w.super_admin.id, target)


def case_org_member_erase(w, h) -> Expect:
    member = _member(w, "orgerase")
    _ok(w.client.post(f"/api/org-admin/gdpr/erase-user/{member.uuid}", headers=w.org_admin_headers))
    target = (("target_user_id", member.id),)
    return Expect(AuditEventType.ADMIN_USER_DELETE, w.org_admin.id, target, w.org.id)


def case_admin_user_delete(w, h) -> Expect:
    victim = make_user(w.db, role="user", label="del")
    _ok(w.client.delete(f"/api/admin/users/{victim.uuid}", headers=w.super_headers))
    target = (("target_user_id", victim.id),)
    return Expect(AuditEventType.ADMIN_USER_DELETE, w.super_admin.id, target)


def case_audit_export(w, h) -> Expect:
    _ok(w.client.get("/api/admin/audit-logs/export?export_format=json", headers=w.super_headers))
    return Expect(AuditEventType.AUDIT_EXPORT, w.super_admin.id, (("details.format", "json"),))


def case_users_delete(w, h) -> Expect:
    victim = make_user(w.db, role="user", label="udel")
    _ok(w.client.delete(f"/api/users/{victim.uuid}", headers=w.super_headers), 204)
    target = (("target_user_id", victim.id),)
    return Expect(AuditEventType.ADMIN_USER_DELETE, w.super_admin.id, target)


def case_org_erase(w, h) -> Expect:
    _ok(
        w.client.post(
            "/api/org-admin/gdpr/erase-organization?confirm=true", headers=w.org_admin_headers
        )
    )
    target = (("details.action", "gdpr_erasure_organization"),)
    return Expect(AuditEventType.ADMIN_USER_DELETE, w.org_admin.id, target, w.org.id)


def _personal_grant(w) -> str:
    return str(w.request(subject_user_uuid=str(w.owner.uuid))["uuid"])


def case_grant_deny(w, h) -> Expect:
    grant = w.request()
    _ok(
        w.client.post(
            f"/api/org-admin/support-access/{grant['uuid']}/deny",
            headers=w.org_admin_headers,
            json={},
        )
    )
    target = (("details.grant_uuid", str(grant["uuid"])),)
    return Expect(AuditEventType.SUPPORT_ACCESS_DENIED, w.org_admin.id, target, w.org.id)


def case_personal_approve(w, h) -> Expect:
    grant_uuid = _personal_grant(w)
    _ok(
        w.client.post(
            f"/api/users/me/support-access/{grant_uuid}/approve", headers=w.owner_headers, json={}
        )
    )
    target = (("target_user_id", w.admin.id),)
    return Expect(AuditEventType.SUPPORT_ACCESS_APPROVED, w.owner.id, target)


def case_personal_deny(w, h) -> Expect:
    grant_uuid = _personal_grant(w)
    _ok(
        w.client.post(
            f"/api/users/me/support-access/{grant_uuid}/deny", headers=w.owner_headers, json={}
        )
    )
    target = (("details.grant_uuid", grant_uuid),)
    return Expect(AuditEventType.SUPPORT_ACCESS_DENIED, w.owner.id, target)


def case_break_glass(w, h) -> Expect:
    body = _ok(
        w.client.post(
            "/api/support-access/grants/break-glass",
            headers=w.super_headers,
            json={
                "organization_uuid": str(w.org.uuid),
                "access_level": "read",
                "reason": "Customer outage, investigating production data",
                "ticket_ref": "INC-1042",
                "duration_minutes": 60,
            },
        ),
        201,
    )
    target = (("details.grant_uuid", str(body["uuid"])),)
    return Expect(AuditEventType.SUPPORT_ACCESS_BREAK_GLASS, w.super_admin.id, target, w.org.id)


def case_thumbnail_use(w, h) -> Expect:
    grant_uuid = w.active_grant()
    response = w.client.get(f"/api/files/{w.file.uuid}/thumbnail", headers=w.with_grant(grant_uuid))
    # The seeded file has no thumbnail object, so the read ends in 404 AFTER the grant was
    # used and recorded; what is asserted is the use row, not the image.
    assert response.status_code == 404, response.text
    target = (("resource_uuid", str(w.file.uuid)),)
    return Expect(AuditEventType.SUPPORT_ACCESS_USED, w.admin.id, target, w.org.id)


def case_data_integrity_purge(w, h) -> Expect:
    _ok(w.client.post("/api/admin/data-integrity?force=true&confirm=true", headers=w.super_headers))
    target = (("details.action", "forced_orphan_purge_requested"),)
    return Expect(AuditEventType.ADMIN_DATA_INTEGRITY_PURGE, w.super_admin.id, target)


NOTICE = {
    "notice_type": "dmca",
    "requester_reference": "CASE-42",
    "basis": "17 USC 512(c) notice, complete",
}


def _notice_with_link(w) -> tuple[str, Any]:
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    f = _detached(w.db, w.personal_file)
    notice = _ok(
        w.client.post(
            "/api/admin/takedown-notices",
            headers=w.admin_headers,
            json={**NOTICE, "received_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
        ),
        201,
    )
    _ok(
        w.client.post(
            f"/api/admin/files/{f.uuid}/quarantine",
            headers=w.admin_headers,
            json={"reason": "notice", "notice_uuid": notice["uuid"]},
        )
    )
    return str(notice["uuid"]), f


def case_notice_update(w, h) -> Expect:
    notice_uuid, f = _notice_with_link(w)
    _ok(
        w.client.patch(
            f"/api/admin/takedown-notices/{notice_uuid}/files/{f.uuid}",
            headers=w.admin_headers,
            json={"status": "upheld"},
        )
    )
    target = (("details.notice_uuid", notice_uuid),)
    return Expect(AuditEventType.ADMIN_TAKEDOWN_NOTICE_UPDATE, w.admin.id, target)


def case_notice_notify(w, h) -> Expect:
    notice_uuid, _f = _notice_with_link(w)
    _ok(
        w.client.post(
            f"/api/admin/takedown-notices/{notice_uuid}/notify", headers=w.admin_headers, json={}
        )
    )
    target = (("details.notice_uuid", notice_uuid),)
    return Expect(AuditEventType.ADMIN_TAKEDOWN_NOTICE_NOTIFY, w.admin.id, target)


def case_notice_export(w, h) -> Expect:
    _notice_with_link(w)
    _ok(w.client.get("/api/admin/takedown-notices/export", headers=w.admin_headers))
    target = (("details.format", "csv"), ("details.format", "json"))
    return Expect(AuditEventType.ADMIN_TAKEDOWN_NOTICE_EXPORT, w.admin.id, target)


def _held_org_file(w) -> Any:
    """``w.file`` under an organization hold, its owner acting in the organization."""
    w.roles[w.owner.id] = (w.org.id, "org:member")
    _place_hold(w)
    return w.file


def _refused(w, response) -> Expect:
    assert response.status_code == 409, response.text
    target = (("resource_uuid", str(w.file.uuid)),)
    return Expect(AuditEventType.CONTENT_HOLD_REFUSED, w.owner.id, target, w.org.id)


def case_hold_refused_delete(w, h) -> Expect:
    f = _held_org_file(w)
    return _refused(w, w.client.delete(f"/api/files/{f.uuid}", headers=w.owner_headers))


def case_hold_refused_reprocess(w, h) -> Expect:
    f = _held_org_file(w)
    return _refused(
        w, w.client.post(f"/api/files/{f.uuid}/reprocess", headers=w.owner_headers, json={})
    )


def case_hold_refused_retry(w, h) -> Expect:
    f = _held_org_file(w)
    # Retry answers 400 for a completed file before it asks about holds; a failed one reaches it.
    f.status = "error"
    w.db.commit()
    return _refused(w, w.client.post(f"/api/files/{f.uuid}/retry", headers=w.owner_headers))


def case_hold_refused_bulk(w, h) -> Expect:
    f = _held_org_file(w)
    response = w.client.post(
        "/api/files/management/bulk-action",
        headers=w.owner_headers,
        json={"action": "delete", "file_uuids": [str(f.uuid)]},
    )
    assert response.status_code == 200, response.text
    target = (("resource_uuid", str(f.uuid)),)
    return Expect(AuditEventType.CONTENT_HOLD_REFUSED, w.owner.id, target, w.org.id)


def case_file_delete_denied(w, h) -> Expect:
    stranger = make_user(w.db, role="user", label="deny")
    w.db.add(
        OrganizationMembership(organization_id=w.org.id, user_id=stranger.id, role="org:member")
    )
    w.db.commit()
    w.roles[stranger.id] = (w.org.id, "org:member")
    from tests.fixtures.support_world import login_headers

    response = w.client.delete(
        f"/api/files/{w.file.uuid}", headers=login_headers(w.client, stranger)
    )
    assert response.status_code in (403, 404), response.text
    target = (("resource_uuid", str(w.file.uuid)), ("details.file_uuid", str(w.file.uuid)))
    return Expect(AuditEventType.FILE_DELETE_DENIED, stranger.id, target)


CASES: dict[str, Callable[[Any, Any], Expect]] = {
    "users-delete": case_users_delete,
    "org-erase": case_org_erase,
    "grant-deny": case_grant_deny,
    "personal-approve": case_personal_approve,
    "personal-deny": case_personal_deny,
    "break-glass": case_break_glass,
    "thumbnail-use": case_thumbnail_use,
    "data-integrity-purge": case_data_integrity_purge,
    "notice-update": case_notice_update,
    "notice-notify": case_notice_notify,
    "notice-export": case_notice_export,
    "hold-refused-delete": case_hold_refused_delete,
    "hold-refused-reprocess": case_hold_refused_reprocess,
    "hold-refused-retry": case_hold_refused_retry,
    "hold-refused-bulk": case_hold_refused_bulk,
    "file-delete-denied": case_file_delete_denied,
    "quarantine": case_quarantine,
    "release": case_release,
    "org-quarantine": case_org_quarantine,
    "org-release": case_org_release,
    "hold-place": case_hold_place,
    "hold-release": case_hold_release,
    "transcript-export": case_transcript_export,
    "subtitles-export": case_subtitles_export,
    "document-export": case_document_export,
    "privacy-purge": case_privacy_purge,
    "grant-request": case_grant_request,
    "grant-approve": case_grant_approve,
    "grant-revoke": case_grant_revoke,
    "gdpr-erase": case_gdpr_erase,
    "org-member-erase": case_org_member_erase,
    "admin-user-delete": case_admin_user_delete,
    "audit-export": case_audit_export,
}
#: Driven here although no single route owns them (a dependency writes the event).
DEPENDENCY_CASES = {"grant-use": case_grant_use}


@dataclass
class Helpers:
    document_export: Any


@pytest.fixture
def organizations_capability_on():
    """``support_world`` asks for this; the ``audited`` fixture enables the whole set."""
    yield


def test_every_live_register_entry_has_a_case():
    live = {case for entry in AUDITED_ROUTES.values() for case in entry.live_cases}
    assert live == set(CASES), {"missing": live - set(CASES), "unregistered": set(CASES) - live}


@pytest.mark.parametrize("case", sorted({**CASES, **DEPENDENCY_CASES}))
def test_action_writes_a_readable_audit_row(case, support_world, audited, document_export):
    w = support_world
    expect = {**CASES, **DEPENDENCY_CASES}[case](w, Helpers(document_export=document_export))

    rows = _read(audited, event_type=expect.event.value, user_id=expect.actor_id)
    assert rows, f"no {expect.event.value} row by actor {expect.actor_id} reached the index"
    actors = {row.get("user_id") for row in rows}
    assert actors == {expect.actor_id}, actors
    hits = [row for row in rows if _hits_target(row, expect.target)]
    assert hits, f"{expect.event.value} rows name the wrong target ({expect.target}): {rows}"

    if expect.org_id is not None:
        members = [
            m.user_id
            for m in w.db.query(OrganizationMembership)
            .filter(OrganizationMembership.organization_id == expect.org_id)
            .all()
        ]
        org_rows = _read(
            audited,
            event_type=expect.event.value,
            scope_org_id=expect.org_id,
            scope_user_ids=members,
        )
        assert any(_hits_target(row, expect.target) for row in org_rows), (
            f"the organization's admin cannot see {expect.event.value}: {org_rows}"
        )


def test_without_its_emitter_the_action_leaves_no_row(audited, support_world, monkeypatch):
    """Negative control: the same quarantine with its emitter removed must find NOTHING.

    Proves a passing case is satisfied by the row its own action wrote, not by another case's
    row in the shared index or by anything else the request does.
    """
    monkeypatch.setattr("app.services.takedown_service.audit_takedown", lambda *a, **k: None)
    expect = case_quarantine(support_world, Helpers(document_export=None))
    assert _read(audited, event_type=expect.event.value, user_id=expect.actor_id) == []
