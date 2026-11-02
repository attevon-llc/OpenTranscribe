"""A re-upload's failed-duplicate cleanup goes through the guarded purge path.

``cleanup_failed_duplicates`` used to delete the media object and the row directly: no legal
hold check, and the thumbnail, playback rendition and derived cache were
left behind. It now calls ``purge_media_file``, so that guard applies.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.utils.file_hash import cleanup_failed_duplicates


@pytest.fixture
def storage(monkeypatch):
    """Record every object-storage delete; stub the OpenSearch and derived-cache planes."""
    deleted: list[str] = []
    cleared: list[int] = []

    def _record_delete(key: str) -> int:
        deleted.append(key)
        return 0

    def _record_clear(self, file_id: int, filename: str) -> int:
        cleared.append(file_id)
        return 0

    monkeypatch.setattr("app.services.minio_service.delete_file", _record_delete)
    monkeypatch.setattr(
        "app.services.video_processing_service.VideoProcessingService.__init__",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.video_processing_service.VideoProcessingService.clear_derived_cache",
        _record_clear,
    )
    monkeypatch.setattr(
        "app.services.file_cleanup_service._cleanup_opensearch_for_file",
        lambda plan, file_uuid: [],
    )
    return type("Storage", (), {"deleted": deleted, "cleared": cleared})


def _failed(db, owner, digest, **fields) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"dup_{file_uuid.hex[:8]}.mp4",
        storage_path=f"user_{owner.id}/file_{file_uuid.hex[:6]}/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=1,
        status="error",
        file_hash=digest,
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_held_failed_duplicate_survives_a_reupload(db_session, normal_user, storage):
    digest = uuid_pkg.uuid4().hex
    held = _failed(db_session, normal_user, digest, legal_hold=True)
    row_id, original = held.id, held.storage_path

    assert cleanup_failed_duplicates(db_session, digest, normal_user.id) == 0

    assert db_session.get(MediaFile, row_id) is not None
    assert original not in storage.deleted


def test_thumbnail_playback_and_derived_residue_are_removed(db_session, normal_user, storage):
    digest = uuid_pkg.uuid4().hex
    failed = _failed(
        db_session,
        normal_user,
        digest,
        thumbnail_path=f"user_{normal_user.id}/t/{digest}.jpg",
        playback_path=f"user_{normal_user.id}/p/{digest}.mp4",
    )
    file_id, original = failed.id, failed.storage_path
    thumb, playback = failed.thumbnail_path, failed.playback_path

    assert cleanup_failed_duplicates(db_session, digest, normal_user.id) == 1

    assert {original, thumb, playback} <= set(storage.deleted)
    assert storage.cleared == [file_id]
    assert db_session.get(MediaFile, file_id) is None
