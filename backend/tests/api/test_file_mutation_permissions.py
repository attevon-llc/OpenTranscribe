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

import uuid

from app.core.enums import FileStatus
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.sharing import CollectionShare


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_file(db_session, owner, *, with_segment: bool = False) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"mutation_perm_{_suffix()}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
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
