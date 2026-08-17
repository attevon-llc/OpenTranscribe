"""Tests for ``app/models/upload_batch.py`` (issue #474).

``UploadBatch`` tracks files uploaded together for batch topic grouping
(``app/api/endpoints/files/prepare_upload.py::get_or_create_upload_batch``). Real rows
against the live dev Postgres via the savepoint-rolled-back ``db_session`` fixture
(``backend/tests/user_owned_rows.py::make_user`` for the owner), covering: column
defaults, the UUID uniqueness constraint, the ``user`` FK requirement, and the
bidirectional relationship to ``MediaFile`` including the ``ON DELETE SET NULL``
behavior on ``media_file.upload_batch_id``.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.media import MediaFile
from app.models.upload_batch import UploadBatch
from tests.user_owned_rows import make_user


def _make_media_file(db_session, user_id: int, **overrides) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    kwargs = {
        "uuid": fuuid,
        "filename": f"upload-batch-fixture-{fuuid.hex[:8]}.mp4",
        "storage_path": f"media/upload-batch-fixture/{fuuid}.mp4",
        "content_type": "video/mp4",
        "file_size": 1234,
        "user_id": user_id,
        "status": "pending",
    }
    kwargs.update(overrides)
    media_file = MediaFile(**kwargs)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


class TestDefaults:
    def test_minimal_row_gets_expected_defaults(self, db_session):
        user = make_user(db_session, "batch-owner")

        batch = UploadBatch(user_id=user.id, source="multi_upload")
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.id is not None
        assert isinstance(batch.uuid, uuid_pkg.UUID)
        assert batch.file_count == 0
        assert batch.grouping_status == "pending"
        assert batch.created_at is not None
        assert batch.user_id == user.id
        assert batch.source == "multi_upload"

    def test_explicit_uuid_is_respected_not_overwritten(self, db_session):
        user = make_user(db_session, "batch-owner-explicit-uuid")
        explicit_uuid = uuid_pkg.uuid4()

        batch = UploadBatch(user_id=user.id, source="playlist", uuid=explicit_uuid)
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.uuid == explicit_uuid

    def test_file_count_and_grouping_status_can_be_overridden(self, db_session):
        user = make_user(db_session, "batch-owner-overrides")

        batch = UploadBatch(
            user_id=user.id,
            source="url_batch",
            file_count=5,
            grouping_status="completed",
        )
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.file_count == 5
        assert batch.grouping_status == "completed"


class TestConstraints:
    def test_duplicate_uuid_is_rejected(self, db_session):
        user = make_user(db_session, "batch-owner-dup-uuid")
        shared_uuid = uuid_pkg.uuid4()

        db_session.add(UploadBatch(user_id=user.id, source="multi_upload", uuid=shared_uuid))
        db_session.commit()

        db_session.add(UploadBatch(user_id=user.id, source="multi_upload", uuid=shared_uuid))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_missing_user_id_is_rejected(self, db_session):
        batch = UploadBatch(source="multi_upload")
        db_session.add(batch)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_missing_source_is_rejected(self, db_session):
        user = make_user(db_session, "batch-owner-no-source")
        batch = UploadBatch(user_id=user.id)
        db_session.add(batch)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_nonexistent_user_id_is_rejected(self, db_session):
        batch = UploadBatch(user_id=-1, source="multi_upload")
        db_session.add(batch)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestRelationships:
    def test_user_relationship_resolves_to_the_owning_user(self, db_session):
        user = make_user(db_session, "batch-owner-relationship")

        batch = UploadBatch(user_id=user.id, source="multi_upload")
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.user.id == user.id
        assert batch.user.email == user.email

    def test_media_files_back_populates_from_upload_batch_id(self, db_session):
        user = make_user(db_session, "batch-owner-media-files")
        batch = UploadBatch(user_id=user.id, source="multi_upload")
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        media_file = _make_media_file(db_session, user.id, upload_batch_id=batch.id)

        db_session.refresh(batch)
        assert [mf.id for mf in batch.media_files] == [media_file.id]
        assert media_file.upload_batch is not None
        assert media_file.upload_batch.id == batch.id

    def test_batch_with_no_media_files_has_empty_collection(self, db_session):
        user = make_user(db_session, "batch-owner-empty")
        batch = UploadBatch(user_id=user.id, source="multi_upload")
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.media_files == []

    def test_deleting_the_batch_nulls_out_the_media_files_fk(self, db_session):
        # upload_batch_id is ondelete="SET NULL" and the relationship is not
        # delete-orphan, so removing the batch must detach its files, not remove them.
        user = make_user(db_session, "batch-owner-delete")
        batch = UploadBatch(user_id=user.id, source="multi_upload")
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        media_file = _make_media_file(db_session, user.id, upload_batch_id=batch.id)

        db_session.delete(batch)
        db_session.commit()

        db_session.refresh(media_file)
        assert media_file.upload_batch_id is None
        # The file itself must survive the batch's deletion.
        assert db_session.query(MediaFile).filter(MediaFile.id == media_file.id).count() == 1
