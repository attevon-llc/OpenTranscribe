"""Characterization of the permission / delete / support-access refusals (L2 group G-D).

These sites decide who may see or delete a file. Each refusal is pinned by status, exact
detail, headers and ``str(exc)`` (``"<status>: <detail>"`` is what Celery persists), and the
endpoints that rewrite or report a refusal are pinned over the wire: the 403 -> 404 existence
masks (a non-owner must read exactly like a missing file), the per-file bulk delete / bulk tag
results (the refusal ``detail`` appears in the result rows), and the per-file loops where one
refused file must not abort the batch.

Every direct-call assertion accepts either class, so the same file is green before the
refusals move from ``HTTPException`` to ``DomainError`` and after.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.core.enums import FileStatus
from app.core.exceptions import DomainError
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.sharing import CollectionShare
from app.services.delete_permissions import DELETE_FORBIDDEN_DETAIL
from app.services.delete_permissions import get_deletable_file
from app.services.permission_service import PermissionService
from app.services.platform_bypass import PlatformBypass

REFUSAL = (HTTPException, DomainError)
NOT_FOUND_BODY = json.dumps({"detail": "File not found"}, separators=(",", ":")).encode()


def _file(db, owner, organization_id=None) -> MediaFile:
    file_uuid = uuid.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"l2gd_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _collection(db, owner) -> Collection:
    coll = Collection(user_id=owner.id, name=f"l2gd-{uuid.uuid4().hex[:8]}")
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _share(db, coll, owner, grantee, permission, media_file=None) -> None:
    if media_file is not None:
        db.add(CollectionMember(collection_id=coll.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=coll.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=grantee.id,
            permission=permission,
        )
    )
    db.commit()


def _refusal(call) -> BaseException:
    with pytest.raises(REFUSAL) as caught:
        call()
    return caught.value


def _pin(exc, status: int, detail, headers=None) -> bool:
    """Assert the refusal's status, detail, headers and persisted text; True so call sites assert it."""
    assert exc.status_code == status
    assert exc.detail == detail
    assert getattr(exc, "headers", None) == headers
    assert str(exc) == f"{status}: {detail}"
    return True


# ---------------------------------------------------------------------------
# PermissionService
# ---------------------------------------------------------------------------


def test_check_file_access_refuses_a_user_with_no_access(db_session, normal_user, other_user):
    media_file = _file(db_session, normal_user)
    exc = _refusal(
        lambda: PermissionService.check_file_access(
            db_session, media_file.id, other_user.id, organization_id=None
        )
    )
    assert _pin(exc, 403, "Not authorized to access this file")


def test_check_file_access_refuses_a_viewer_who_needs_editor(db_session, normal_user, other_user):
    media_file = _file(db_session, normal_user)
    _share(
        db_session,
        _collection(db_session, normal_user),
        normal_user,
        other_user,
        "viewer",
        media_file,
    )
    exc = _refusal(
        lambda: PermissionService.check_file_access(
            db_session, media_file.id, other_user.id, "editor", organization_id=None
        )
    )
    assert _pin(exc, 403, "Requires editor permission on this file")
    # Control: the same viewer passes at their own level, the owner passes at any level.
    assert (
        PermissionService.check_file_access(
            db_session, media_file.id, other_user.id, "viewer", organization_id=None
        )
        == "viewer"
    )
    assert (
        PermissionService.check_file_access(
            db_session, media_file.id, normal_user.id, "editor", organization_id=None
        )
        == "owner"
    )


def test_check_collection_access_refuses_no_access_and_low_permission(
    db_session, normal_user, other_user
):
    coll = _collection(db_session, normal_user)
    exc = _refusal(
        lambda: PermissionService.check_collection_access(
            db_session, coll.id, other_user.id, organization_id=None
        )
    )
    assert _pin(exc, 403, "Not authorized to access this collection")

    _share(db_session, coll, normal_user, other_user, "viewer")
    exc = _refusal(
        lambda: PermissionService.check_collection_access(
            db_session, coll.id, other_user.id, "editor", organization_id=None
        )
    )
    assert _pin(exc, 403, "Requires editor permission on this collection")
    assert (
        PermissionService.check_collection_access(
            db_session, coll.id, other_user.id, "viewer", organization_id=None
        )
        == "viewer"
    )


# ---------------------------------------------------------------------------
# get_deletable_file
# ---------------------------------------------------------------------------


def test_deletable_file_quarantined_reads_as_missing(db_session, normal_user, super_admin_user):
    from app.services.takedown_service import TakedownActor
    from app.services.takedown_service import quarantine_file

    media_file = _file(db_session, normal_user)
    quarantine_file(
        db_session,
        media_file,
        actor=TakedownActor.platform(super_admin_user),
        reason="DMCA-l2gd",
        legal_hold=True,
        operator_action_reason="operator action (test)",
    )
    exc = _refusal(lambda: get_deletable_file(db_session, str(media_file.uuid), normal_user))
    assert _pin(exc, 404, "File not found")


def test_deletable_file_outside_the_active_tenant_is_403(db_session, normal_user):
    org = Organization(name=f"l2gd-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    media_file = _file(db_session, normal_user, organization_id=org.id)
    exc = _refusal(
        lambda: get_deletable_file(
            db_session, str(media_file.uuid), normal_user, organization_id=None
        )
    )
    assert _pin(exc, 403, "You do not have permission to access this file")


def test_deletable_file_with_no_visibility_and_with_read_only_share(
    db_session, normal_user, other_user
):
    media_file = _file(db_session, normal_user)
    with patch("app.auth.audit.audit_logger.log"):
        exc = _refusal(lambda: get_deletable_file(db_session, str(media_file.uuid), other_user))
        assert _pin(exc, 403, "You do not have permission to access this file")

        _share(
            db_session,
            _collection(db_session, normal_user),
            normal_user,
            other_user,
            "editor",
            media_file,
        )
        exc = _refusal(lambda: get_deletable_file(db_session, str(media_file.uuid), other_user))
        assert _pin(exc, 403, DELETE_FORBIDDEN_DETAIL)
    # Control: the owner resolves it.
    assert get_deletable_file(db_session, str(media_file.uuid), normal_user).id == media_file.id


# ---------------------------------------------------------------------------
# PlatformBypass.require_deployment_scope
# ---------------------------------------------------------------------------


def test_require_deployment_scope_refuses_a_plain_user(normal_user, super_admin_user):
    exc = _refusal(lambda: PlatformBypass(user=normal_user).require_deployment_scope("Reindexing"))
    assert _pin(exc, 403, "Reindexing covers every tenant and needs a super admin")
    exc = _refusal(lambda: PlatformBypass.none().require_deployment_scope("Purging"))
    assert _pin(exc, 403, "Purging covers every tenant and needs a super admin")
    PlatformBypass(user=super_admin_user).require_deployment_scope("Reindexing")


# ---------------------------------------------------------------------------
# Support access: coded errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 403, 409, 422, 503])
def test_coded_error_shape_per_status(status):
    from app.services.support_access_service import coded_error

    err = coded_error(status, "some_code", "Some message.")
    assert isinstance(err, REFUSAL)
    assert _pin(err, status, {"code": "some_code", "message": "Some message."})


def test_resolve_active_grant_unknown_uuid_is_the_coded_403(db_session, admin_user):
    from app.services.support_access_service import resolve_active_grant

    for bad in ("not-a-uuid", str(uuid.uuid4())):
        exc = _refusal(lambda bad=bad: resolve_active_grant(db_session, bad, admin_user))
        assert _pin(
            exc,
            403,
            {"code": "support_grant_invalid", "message": "Support access grant is not valid."},
        )


def test_record_use_fails_closed_with_the_coded_503():
    from app.services.support_access_service import record_use

    with patch("app.db.session_utils.session_scope", side_effect=RuntimeError("db down")):
        exc = _refusal(
            lambda: record_use(
                MagicMock(),
                actor_user_id=1,
                method="GET",
                route="/x",
                resource_type=None,
                resource_uuid=None,
                need=None,
                organization_id=None,
                owner_user_id=None,
            )
        )
    assert _pin(
        exc,
        503,
        {
            "code": "support_access_audit_unavailable",
            "message": "Support access could not be recorded; the request was not served.",
        },
    )


def test_invalid_target_is_the_coded_422():
    from app.services.support_access_lifecycle import _invalid_target

    err = _invalid_target()
    assert isinstance(err, REFUSAL)
    assert _pin(
        err,
        422,
        {
            "code": "support_grant_invalid_target",
            "message": "Exactly one valid target is required.",
        },
    )


# ---------------------------------------------------------------------------
# Voiceprints disabled (409)
# ---------------------------------------------------------------------------


def test_voiceprints_disabled_error_is_the_coded_409():
    from app.services.privacy.redact_view import voiceprints_disabled_error

    decision = MagicMock(locked_by="organization")
    err = voiceprints_disabled_error(decision)
    assert isinstance(err, REFUSAL)
    assert _pin(err, 409, {"code": "voiceprints_disabled", "locked_by": "organization"})


# ---------------------------------------------------------------------------
# Bulk tag: the refusal detail lands in the per-file result row
# ---------------------------------------------------------------------------


def test_require_editor_returns_the_refusal_detail_as_a_failed_result(
    db_session, normal_user, other_user
):
    from app.services.tag_bulk import PERMISSION_ERROR
    from app.services.tag_bulk import BulkTagOutcome
    from app.services.tag_bulk import _require_editor

    media_file = _file(db_session, normal_user)
    result = _require_editor(db_session, media_file.id, other_user.id, PlatformBypass.none())
    assert result is not None
    assert result.outcome is BulkTagOutcome.FAILED
    assert result.message == "Not authorized to access this file"
    assert result.error == PERMISSION_ERROR
    # Control: the owner is not refused.
    assert _require_editor(db_session, media_file.id, normal_user.id, PlatformBypass.none()) is None


def test_bulk_tag_viewer_row_carries_the_editor_requirement_and_batch_continues(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    read_only = _file(db_session, normal_user)
    _share(
        db_session,
        _collection(db_session, normal_user),
        normal_user,
        other_user,
        "viewer",
        read_only,
    )
    own = _file(db_session, other_user)
    name = f"l2gd-{uuid.uuid4().hex[:8]}"

    response = client.post(
        "/api/files/management/bulk-action",
        headers=other_user_auth_headers,
        json={
            "file_uuids": [str(read_only.uuid), str(own.uuid)],
            "action": "add_tag",
            "tag_name": name,
        },
    )

    assert response.status_code == 200
    rows = {r["file_uuid"]: r for r in response.json()}
    refused = rows[str(read_only.uuid)]
    assert refused["success"] is False
    assert refused["outcome"] == "failed"
    assert refused["error"] == "HTTP_ERROR"
    assert refused["message"] == "Requires editor permission on this file"
    assert rows[str(own.uuid)]["outcome"] == "added"


# ---------------------------------------------------------------------------
# Bulk delete: per-file results
# ---------------------------------------------------------------------------


def test_bulk_delete_reports_forbidden_per_file_and_deletes_the_rest(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    mine_a = _file(db_session, other_user)
    theirs = _file(db_session, normal_user)
    _share(
        db_session, _collection(db_session, normal_user), normal_user, other_user, "editor", theirs
    )
    mine_b = _file(db_session, other_user)

    with patch("app.auth.audit.audit_logger.log"):
        response = client.post(
            "/api/files/management/bulk-action",
            headers=other_user_auth_headers,
            json={
                "action": "delete",
                "file_uuids": [str(mine_a.uuid), str(theirs.uuid), str(mine_b.uuid)],
            },
        )

    assert response.status_code == 200
    rows = response.json()
    assert [r["file_uuid"] for r in rows] == [str(mine_a.uuid), str(theirs.uuid), str(mine_b.uuid)]
    assert [r["success"] for r in rows] == [True, False, True]
    assert rows[1]["error"] == "FORBIDDEN"
    assert rows[1]["message"] == DELETE_FORBIDDEN_DETAIL
    db_session.expire_all()
    assert db_session.get(MediaFile, theirs.id) is not None
    assert db_session.get(MediaFile, mine_a.id) is None
    assert db_session.get(MediaFile, mine_b.id) is None


def test_bulk_delete_of_a_quarantined_file_carries_the_not_found_detail(
    client, user_token_headers, normal_user, super_admin_user, db_session
):
    from app.services.takedown_service import TakedownActor
    from app.services.takedown_service import quarantine_file

    held = _file(db_session, normal_user)
    quarantine_file(
        db_session,
        held,
        actor=TakedownActor.platform(super_admin_user),
        reason="DMCA-l2gd-bulk",
        legal_hold=True,
        operator_action_reason="operator action (test)",
    )
    free = _file(db_session, normal_user)

    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"action": "delete", "file_uuids": [str(held.uuid), str(free.uuid)]},
    )

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["success"] is False
    assert rows[0]["error"] == "HTTP_ERROR"
    assert rows[0]["message"] == "File not found"
    assert rows[1]["success"] is True


# ---------------------------------------------------------------------------
# 403 -> 404 existence masks: a non-owner reads exactly like a missing file
# ---------------------------------------------------------------------------


def test_file_detail_masks_another_users_file_as_a_missing_one(
    client, other_user_auth_headers, normal_user, db_session
):
    foreign = _file(db_session, normal_user)

    denied = client.get(f"/api/files/{foreign.uuid}", headers=other_user_auth_headers)
    missing = client.get(f"/api/files/{uuid.uuid4()}", headers=other_user_auth_headers)

    assert denied.status_code == 404
    assert denied.content == NOT_FOUND_BODY
    assert (missing.status_code, missing.content) == (denied.status_code, denied.content)


def test_effective_privacy_masks_another_users_file_as_a_missing_one(
    client, other_user_auth_headers, normal_user, db_session
):
    foreign = _file(db_session, normal_user)

    denied = client.get(
        "/api/privacy/effective",
        params={"file_uuid": str(foreign.uuid)},
        headers=other_user_auth_headers,
    )
    missing = client.get(
        "/api/privacy/effective",
        params={"file_uuid": str(uuid.uuid4())},
        headers=other_user_auth_headers,
    )

    assert denied.status_code == 404
    assert denied.content == NOT_FOUND_BODY
    assert (missing.status_code, missing.content) == (denied.status_code, denied.content)


# ---------------------------------------------------------------------------
# Per-file loops: one refused file does not abort the batch
# ---------------------------------------------------------------------------


def test_bulk_export_prepare_skips_a_refused_file_and_exports_the_rest(
    client, other_user_auth_headers, other_user, normal_user, db_session, monkeypatch
):
    foreign = _file(db_session, normal_user)
    mine = _file(db_session, other_user)
    captured: dict = {}

    from app.tasks import media_download

    monkeypatch.setattr(
        media_download.prepare_bulk_subtitles_task, "delay", lambda **kw: captured.update(kw)
    )

    response = client.post(
        "/api/files/bulk-export/prepare",
        headers=other_user_auth_headers,
        json={"file_uuids": [str(foreign.uuid), str(mine.uuid)], "subtitle_format": "srt"},
    )

    assert response.status_code == 200
    assert [spec[0] for spec in captured["file_specs"]] == [int(mine.id)]


def test_batch_extract_topics_skips_a_refused_file_and_keeps_the_rest(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    foreign = _file(db_session, normal_user)
    mine = _file(db_session, other_user)
    db_session.add(
        TranscriptSegment(media_file_id=mine.id, start_time=0.0, end_time=1.0, text="hello")
    )
    db_session.commit()

    task = MagicMock(id="task-l2gd")
    with (
        patch(
            "app.api.endpoints.topics.TopicExtractionService.create_from_settings",
            return_value=MagicMock(),
        ),
        patch("app.tasks.topic_extraction.batch_extract_topics_task.delay", return_value=task),
    ):
        response = client.post(
            "/api/files/batch-extract",
            headers=other_user_auth_headers,
            json={"file_uuids": [str(foreign.uuid), str(mine.uuid)]},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["files"] == [str(mine.uuid)]
    assert body["file_count"] == 1
