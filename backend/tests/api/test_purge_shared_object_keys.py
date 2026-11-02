"""A file purge never deletes an object key another media file still references.

Deleting a key now removes every version of it, so a shared key would lose its whole history,
not just its current bytes. Keys are per file by construction with one exception: a URL
import's thumbnail is keyed by the SOURCE video (``user_<uid>/<extractor>_<video_id>/
thumbnail.jpg``), so importing the same video twice gives two rows one thumbnail object.
``file_cleanup_service._load_purge_plan`` drops such a key from the plan; the last row to go
deletes it.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.models.media import MediaFile
from app.services.file_cleanup_service import _load_purge_plan

SHARED_THUMB = "user_1/youtube_dQw4w9WgXcQ/thumbnail.jpg"


def _row(db, owner, **fields) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"shared_{file_uuid.hex[:8]}.mp4",
        storage_path=f"user_{owner.id}/file_x/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=1,
        status="completed",
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_thumbnail_another_import_still_uses_is_kept(db_session, normal_user):
    first = _row(db_session, normal_user, thumbnail_path=SHARED_THUMB)
    second = _row(db_session, normal_user, thumbnail_path=SHARED_THUMB)

    plan = _load_purge_plan(db_session, first)

    assert plan["thumbnail_path"] is None
    assert plan["shared_paths"] == [SHARED_THUMB]
    assert plan["storage_path"] == first.storage_path, "the file's own original still goes"
    assert second.thumbnail_path == SHARED_THUMB


def test_the_last_reference_deletes_the_shared_key(db_session, normal_user, monkeypatch):
    """Purge row A then row B on the SAME key: it survives A and is gone after B."""
    from app.services import file_cleanup_service as fcs

    first = _row(db_session, normal_user, thumbnail_path=SHARED_THUMB)
    second = _row(db_session, normal_user, thumbnail_path=SHARED_THUMB)
    deleted: list[str] = []

    def _record(key: str) -> int:
        deleted.append(key)
        return 0

    monkeypatch.setattr("app.services.minio_service.delete_file", _record)
    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", lambda plan, file_uuid: [])
    monkeypatch.setattr(
        "app.services.video_processing_service.VideoProcessingService.__init__",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.video_processing_service.VideoProcessingService.clear_derived_cache",
        lambda self, file_id, filename: 0,
    )
    first_original, second_original = first.storage_path, second.storage_path

    assert fcs.purge_media_file(db_session, first)["deleted"] is True
    assert SHARED_THUMB not in deleted, "row B still references it"
    assert first_original in deleted

    assert fcs.purge_media_file(db_session, second)["deleted"] is True
    assert deleted.count(SHARED_THUMB) == 1, "the last row to go deletes it"
    assert second_original in deleted
