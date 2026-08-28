"""Behavioral tests for issue #588: viewer-permission users mutating shared files.

``get_file_by_uuid_with_permission`` (``app/utils/uuid_helpers.py``) previously admitted
*any* non-None permission returned by ``PermissionService.get_file_permission`` — including
``viewer`` — onto endpoints that mutate the file (reprocess, summarize, retry, topic
extraction). A read-only collaborator on a shared file could therefore trigger destructive
work (re-transcription, LLM spend, task cancellation) despite having no write grant.

The fix adds a keyword-only ``min_permission`` param (default ``"viewer"``, preserving
existing behavior for read-only call sites) and updates the identified mutating call sites
to require ``"editor"``. These tests prove the bug was real (red against the unfixed
call sites) and that the fix closes it (green) without blocking editors/owners.
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest

from app.core.enums import FileStatus
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.sharing import CollectionShare
from app.services.minio_service import MinIOService
from app.services.video_processing_service import VideoProcessingService

API_ENDPOINTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "api" / "endpoints"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_file(
    db_session,
    owner,
    *,
    with_segment: bool = False,
    status: FileStatus = FileStatus.COMPLETED,
    active_task_id: str | None = None,
) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"mutation_perm_{_suffix()}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=status,
        active_task_id=active_task_id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    if with_segment:
        segment = TranscriptSegment(
            media_file_id=media_file.id,
            start_time=0.0,
            end_time=1.0,
            text="hello world",
        )
        db_session.add(segment)
        db_session.commit()
        db_session.refresh(media_file)

    return media_file


def _share_file(db_session, media_file, owner, viewer, *, permission: str) -> Collection:
    """Put ``media_file`` in a collection shared with ``viewer`` at ``permission``.

    Copied verbatim (pattern-wise) from ``test_bulk_tag_action.py::_share_file``.
    """
    collection = Collection(
        user_id=owner.id, name=f"shared-{_suffix()}", description="mutation perm share"
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=viewer.id,
            permission=permission,
        )
    )
    db_session.commit()
    return collection


def test_viewer_cannot_reprocess_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=other_user_auth_headers,
        json={},
    )

    assert response.status_code == 403


def test_viewer_cannot_summarize_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, with_segment=True)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/files/{media_file.uuid}/summarize",
        headers=other_user_auth_headers,
        json={},
    )

    assert response.status_code == 403


def test_editor_can_reprocess_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=other_user_auth_headers,
        json={},
    )

    # May still fail on missing storage/other preconditions in the test env —
    # what matters here is that permission itself is NOT what blocks it.
    assert response.status_code != 403


def test_owner_still_reprocesses(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)

    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={},
    )

    assert response.status_code != 403


def test_viewer_cannot_retry_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/my-files/{media_file.uuid}/retry",
        headers=other_user_auth_headers,
    )

    assert response.status_code == 403


def test_viewer_cannot_retry_summary_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, with_segment=True)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/files/{media_file.uuid}/retry-summary",
        headers=other_user_auth_headers,
    )

    assert response.status_code == 403


def test_viewer_cannot_auto_label_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, with_segment=True)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/files/{media_file.uuid}/auto-label",
        headers=other_user_auth_headers,
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Anti-drift guard: every ``min_permission="editor"`` call site in the codebase
# must be represented in ``MUTATING_ENDPOINTS`` below, by file:line. This is an
# AST scan (not a text grep) so it cannot be fooled by the string appearing in a
# docstring/comment (``crud.py``'s ``get_media_file_by_uuid`` docstring literally
# contains the text ``min_permission="editor"`` as documentation).
# ---------------------------------------------------------------------------


def _find_min_permission_editor_sites() -> list[tuple[str, int]]:
    """AST-scan every module under ``app/api/endpoints`` for a real
    ``min_permission="editor"`` keyword argument (not a docstring mention).

    Returns ``(relative_path, lineno)`` pairs, sorted, so a failure names the
    exact file:line of any site missing from ``MUTATING_ENDPOINTS``.
    """
    sites: list[tuple[str, int]] = []
    for path in sorted(API_ENDPOINTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "min_permission"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "editor"
                ):
                    sites.append((str(path.relative_to(API_ENDPOINTS_DIR)), node.lineno))
    return sorted(sites)


# One row per real ``min_permission="editor"`` call site (25 as of issue #588).
# The 10 in ``files/{crud,management,__init__,waveform}.py`` are the ones this
# change adds; the rest (``files/reprocess.py``, ``files/summary_status.py``,
# ``media_collections.py``, ``summarization.py``, ``tasks.py``, ``topics.py``,
# ``user_files.py``) were fixed by a prior commit (issue #588 part 1) and are
# listed here only so the guard's count matches the codebase exactly.
MUTATING_ENDPOINTS: list[tuple[str, int]] = [
    ("files/__init__.py", 1137),
    ("files/__init__.py", 1204),
    ("files/crud.py", 874),
    ("files/crud.py", 965),
    ("files/crud.py", 1040),
    ("files/management.py", 206),
    ("files/management.py", 258),
    ("files/management.py", 356),
    ("files/management.py", 907),
    ("files/reprocess.py", 434),
    ("files/summary_status.py", 105),
    ("files/waveform.py", 362),
    ("media_collections.py", 703),
    ("media_collections.py", 800),
    ("media_collections.py", 886),
    ("summarization.py", 86),
    ("summarization.py", 349),
    ("summarization.py", 415),
    ("tasks.py", 769),
    ("topics.py", 79),
    ("topics.py", 241),
    ("topics.py", 385),
    ("topics.py", 460),
    ("topics.py", 519),
    ("user_files.py", 360),
]


def test_min_permission_editor_sites_match_the_codebase():
    """Fails with the exact file:line of any drift — a new mutating call site
    added without ``min_permission="editor"``, or one removed from the code
    without being removed from the table here.
    """
    actual = _find_min_permission_editor_sites()
    expected = sorted(MUTATING_ENDPOINTS)

    missing_from_table = sorted(set(actual) - set(expected))
    missing_from_code = sorted(set(expected) - set(actual))

    assert not missing_from_table, (
        f'Found min_permission="editor" call site(s) not tracked in '
        f"MUTATING_ENDPOINTS: {missing_from_table}. Add them to the table above."
    )
    assert not missing_from_code, (
        f"MUTATING_ENDPOINTS references site(s) no longer present in the code: "
        f"{missing_from_code}. Remove the stale row(s)."
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# New call sites fixed by this change: crud.py (update/delete/segment-edit),
# management.py (cancel/retry/recover/bulk-action), __init__.py
# (cache-clear/analytics-refresh), waveform.py (waveform generation).
# ---------------------------------------------------------------------------


def test_viewer_cannot_update_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=other_user_auth_headers,
        json={"title": "hijacked"},
    )

    assert response.status_code == 403


def test_editor_can_update_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=other_user_auth_headers,
        json={"title": "renamed-by-editor"},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "renamed-by-editor"


def test_owner_still_updates_own_file(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"title": "owner-renamed"},
    )

    assert response.status_code == 200


def test_viewer_cannot_delete_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 403
    # The file must still exist — a 403 that nonetheless deleted the row would be
    # worse than useless. ``client`` shares ``db_session`` (savepoint-isolated) via
    # the ``get_db`` override, so a plain re-fetch on the same session sees exactly
    # what the request committed.
    db_session.expire_all()
    assert db_session.get(MediaFile, media_file.id) is not None


def test_editor_can_delete_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 204


def test_viewer_cannot_edit_transcript_segment(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, with_segment=True)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")
    segment = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .first()
    )

    response = client.put(
        f"/api/files/{media_file.uuid}/transcript/segments/{segment.uuid}",
        headers=other_user_auth_headers,
        json={"text": "hijacked text"},
    )

    assert response.status_code == 403


def test_editor_can_edit_transcript_segment(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, with_segment=True)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")
    segment = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .first()
    )

    response = client.put(
        f"/api/files/{media_file.uuid}/transcript/segments/{segment.uuid}",
        headers=other_user_auth_headers,
        json={"text": "edited by editor"},
    )

    assert response.status_code == 200


def test_viewer_cannot_cancel_processing_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, status=FileStatus.PROCESSING)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(f"/api/files/{media_file.uuid}/cancel", headers=other_user_auth_headers)

    assert response.status_code == 403


def test_editor_can_cancel_processing_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session, monkeypatch
):
    media_file = _make_file(
        db_session,
        normal_user,
        status=FileStatus.PROCESSING,
        active_task_id="11111111-1111-1111-1111-111111111111",
    )
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    # cancel_active_task calls celery_app.control.revoke(), a real broker round trip
    # that SKIP_CELERY does not stub (only .delay()/.apply_async() are patched). Stub
    # just that call so this test exercises the permission gate under test, not the
    # test environment's broker reachability.
    from app.core.celery import celery_app

    monkeypatch.setattr(celery_app.control, "revoke", lambda *a, **k: None)

    response = client.post(f"/api/files/{media_file.uuid}/cancel", headers=other_user_auth_headers)

    assert response.status_code == 200


def test_viewer_cannot_retry_shared_file_via_files_route(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    """The management-router ``POST /files/{uuid}/retry`` — distinct from the
    already-fixed ``POST /my-files/{uuid}/retry``."""
    media_file = _make_file(db_session, normal_user, status=FileStatus.ERROR)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=other_user_auth_headers)

    assert response.status_code == 403


def test_editor_can_retry_shared_file_via_files_route(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, status=FileStatus.ERROR)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=other_user_auth_headers)

    assert response.status_code == 200


def test_viewer_cannot_recover_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, status=FileStatus.ERROR)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(f"/api/files/{media_file.uuid}/recover", headers=other_user_auth_headers)

    assert response.status_code == 403


def test_editor_can_recover_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, status=FileStatus.ERROR)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.post(f"/api/files/{media_file.uuid}/recover", headers=other_user_auth_headers)

    # recover_stuck_file's exact outcome depends on retry-ceiling state; the
    # claim under test is only that permission does not block it.
    assert response.status_code != 403
    assert response.status_code < 500


def test_viewer_cannot_clear_video_cache(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.delete(f"/api/files/{media_file.uuid}/cache", headers=other_user_auth_headers)

    assert response.status_code == 403


@pytest.fixture
def _stub_object_storage(monkeypatch):
    """Neutralize MinIO I/O (unreachable in CI, which runs no MinIO container).

    Same stub as ``test_cache_management.py``'s ``_stub_object_storage`` — this
    test only exercises the permission gate in front of ``clear_video_cache``,
    not cache-key correctness, so a real bucket-existence check and delete
    round trip are not part of the claim under test.
    """
    monkeypatch.setattr(VideoProcessingService, "_ensure_cache_bucket_exists", lambda self: None)
    monkeypatch.setattr(MinIOService, "list_objects", lambda self, b, prefix, recursive=True: [])
    monkeypatch.setattr(MinIOService, "delete_object", lambda self, b, k: None)


def test_editor_can_clear_video_cache(
    client, other_user_auth_headers, other_user, normal_user, db_session, _stub_object_storage
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.delete(f"/api/files/{media_file.uuid}/cache", headers=other_user_auth_headers)

    assert response.status_code == 204


def test_viewer_cannot_refresh_analytics(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/files/{media_file.uuid}/analytics/refresh", headers=other_user_auth_headers
    )

    assert response.status_code == 403


def test_editor_can_refresh_analytics(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.post(
        f"/api/files/{media_file.uuid}/analytics/refresh", headers=other_user_auth_headers
    )

    # AnalyticsService.refresh_analytics may still fail on a file with no
    # transcript segments in the test environment; permission is not the gate.
    assert response.status_code != 403
    assert response.status_code < 500


def test_viewer_cannot_generate_waveform(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

    response = client.post(
        f"/api/files/{media_file.uuid}/waveform/generate", headers=other_user_auth_headers
    )

    assert response.status_code == 403


def test_editor_can_generate_waveform(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    _share_file(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.post(
        f"/api/files/{media_file.uuid}/waveform/generate", headers=other_user_auth_headers
    )

    # Real waveform extraction depends on ffmpeg/storage in the test environment;
    # permission is not the gate under test.
    assert response.status_code != 403
    assert response.status_code < 500


# ---------------------------------------------------------------------------
# Bulk action funnel: POST /files/management/bulk-action routes every one of
# nine actions through a single get_media_file_by_uuid(min_permission="editor")
# call in _process_single_file_action. A viewer must get a per-file soft
# failure (not a top-level 403 — see files/management.py's docstring) for
# every action, and — specifically for "delete" — the file must survive.
# ---------------------------------------------------------------------------

BULK_ACTIONS = [
    "delete",
    "retry",
    "cancel",
    "recover",
    "reprocess",
    "summarize",
    "redact",
    "identify_speakers",
    "add_tag",
    "remove_tag",
]


def test_viewer_cannot_bulk_act_on_shared_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    assert len(BULK_ACTIONS) == 10  # sanity: the loop below must actually run all nine+ actions

    checked_actions = []
    for action in BULK_ACTIONS:
        media_file = _make_file(db_session, normal_user, with_segment=True)
        _share_file(db_session, media_file, normal_user, other_user, permission="viewer")

        body = {"file_uuids": [str(media_file.uuid)], "action": action}
        if action in ("add_tag", "remove_tag"):
            body["tag_name"] = "some-tag"

        response = client.post(
            "/api/files/management/bulk-action",
            headers=other_user_auth_headers,
            json=body,
        )

        assert response.status_code == 200, f"action={action}"
        result = response.json()[0]
        assert result["success"] is False, f"action={action}"
        assert result["error"] == "HTTP_ERROR", f"action={action}"

        if action == "delete":
            db_session.expire_all()
            assert db_session.get(MediaFile, media_file.id) is not None

        checked_actions.append(action)

    # Every action in the table was actually exercised, not skipped by an empty loop.
    assert checked_actions == BULK_ACTIONS
