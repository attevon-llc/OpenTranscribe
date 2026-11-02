"""A delete whose object versions could not be checked is recorded unverified, not complete.

``storage_versions.remove_completely`` flags ``unverified`` when both versioning reads are
refused. File purge and GDPR erasure must carry that into their residual errors so the
erasure audits PARTIAL.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.services import file_cleanup_service as fcs
from app.services.storage_versions import UNVERIFIED_VERSIONS_ERROR
from app.services.storage_versions import RemovalResult


@pytest.fixture
def stubbed_planes(monkeypatch):
    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", lambda plan, file_uuid: [])
    monkeypatch.setattr(
        "app.services.video_processing_service.VideoProcessingService.__init__",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.video_processing_service.VideoProcessingService.clear_derived_cache",
        lambda self, file_id, filename: RemovalResult(0),
    )


def _row(db, owner) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"unv_{file_uuid.hex[:8]}.mp4",
        storage_path=f"user_{owner.id}/file_{file_uuid.hex[:6]}/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=1,
        status="completed",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_an_unverified_media_delete_is_a_residual_error(
    db_session, normal_user, stubbed_planes, monkeypatch
):
    monkeypatch.setattr(
        "app.services.minio_service.delete_file", lambda key: RemovalResult(1, unverified=True)
    )

    result = fcs.purge_media_file(db_session, _row(db_session, normal_user))

    assert result["deleted"] is True
    stages = [e["stage"] for e in result["residual_errors"]]
    assert stages == ["storage_unverified"]
    assert result["residual_errors"][0]["error"] == UNVERIFIED_VERSIONS_ERROR


def test_a_verified_media_delete_has_no_residual(
    db_session, normal_user, stubbed_planes, monkeypatch
):
    monkeypatch.setattr("app.services.minio_service.delete_file", lambda key: RemovalResult(1))

    result = fcs.purge_media_file(db_session, _row(db_session, normal_user))

    assert result["deleted"] is True
    assert result["residual_errors"] == []


def test_an_unverified_avatar_delete_is_recorded_by_erasure(monkeypatch):
    from app.services.gdpr_erasure_service import _erase_avatar_objects

    monkeypatch.setattr(
        "app.services.minio_service.delete_file", lambda key: RemovalResult(1, unverified=True)
    )
    errors: list[dict] = []

    _erase_avatar_objects(["avatars/a.jpg"], errors)

    assert errors == [{"stage": "storage_unverified", "error": UNVERIFIED_VERSIONS_ERROR}]
