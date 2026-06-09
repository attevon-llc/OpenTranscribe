"""Unit tests for the in-place MinIO re-ingestion service (Feature B).

GPU-free, savepoint-isolated. The minio client is mocked at the service
boundary (``iter_media_objects`` / ``list_objects``) so the live 484 GB bucket
is NEVER listed. Rows are created on the savepoint ``db_session`` and roll back
at teardown.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services import storage_recovery_service as recovery


class _FakeObj:
    """Minimal stand-in for a minio list_objects result item."""

    def __init__(self, object_name: str, size: int) -> None:
        self.object_name = object_name
        self.size = size


class _FakeMinio:
    """Fake minio client whose list_objects yields a fixed object set.

    Honors the ``prefix`` filter the way the real client does so the
    user-scoping path is exercised too.
    """

    def __init__(self, objects: list[_FakeObj]) -> None:
        self._objects = objects

    def list_objects(self, bucket, prefix=None, recursive=False):  # noqa: ARG002
        for obj in self._objects:
            if prefix is None or obj.object_name.startswith(prefix):
                yield obj


# ---------------------------------------------------------------------------
# content_type_for / extension handling (pure functions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("media/1/abc.mp4", "video/mp4"),
        ("media/1/abc.mkv", "video/x-matroska"),
        ("media/1/abc.webm", "video/webm"),
        ("media/1/abc.mp3", "audio/mpeg"),
        ("media/1/abc.wav", "audio/wav"),
        ("media/1/abc.m4a", "audio/mp4"),
    ],
)
def test_content_type_for(key, expected):
    assert recovery.content_type_for(key) == expected


def test_content_type_for_unknown_falls_back():
    assert recovery.content_type_for("media/1/abc.zzqq") == "application/octet-stream"


def test_iter_media_objects_filters_non_media():
    client = _FakeMinio(
        [
            _FakeObj("media/1/a.mp4", 100),
            _FakeObj("media/1/b.txt", 50),  # not a media ext -> skipped
            _FakeObj("media/1/c.wav", 200),
        ]
    )
    found = list(recovery.iter_media_objects(client))
    assert found == [("media/1/a.mp4", 100), ("media/1/c.wav", 200)]


def test_iter_media_objects_user_scope():
    client = _FakeMinio(
        [
            _FakeObj("media/1/a.mp4", 100),
            _FakeObj("media/2/b.mp4", 200),
        ]
    )
    found = list(recovery.iter_media_objects(client, user_id=2))
    assert found == [("media/2/b.mp4", 200)]


# ---------------------------------------------------------------------------
# Discovery idempotency + row shape
# ---------------------------------------------------------------------------


def test_register_object_row_shape(db_session, normal_user):
    """A registered row points at the EXISTING key and is PENDING with placeholder name."""
    obj = "media/1/deadbeef-1234.mp4"
    mf = recovery.register_object(db_session, object_name=obj, size=4096, user=normal_user)

    assert mf.storage_path == obj  # no copy/move — points in place
    assert mf.filename == "deadbeef-1234.mp4"
    assert mf.title == "deadbeef-1234.mp4"
    assert mf.content_type == "video/mp4"
    assert mf.file_size == 4096
    assert mf.status == FileStatus.PENDING
    assert mf.user_id == normal_user.id
    assert mf.is_public is False


def test_reingest_idempotent_skips_existing(db_session, normal_user):
    """An object already referenced by a row is skipped on re-run (no duplicate)."""
    existing_key = "media/1/already.mp4"
    db_session.add(
        MediaFile(
            user_id=normal_user.id,
            filename="already.mp4",
            title="already",
            storage_path=existing_key,
            file_size=10,
            content_type="video/mp4",
            status=FileStatus.COMPLETED,
        )
    )
    db_session.commit()

    client = _FakeMinio(
        [
            _FakeObj(existing_key, 10),  # already registered -> skip
            _FakeObj("media/1/fresh.mp4", 20),  # new -> register
        ]
    )

    with patch.object(recovery, "fingerprint_object", lambda mf: None):
        summary = recovery.reingest_objects(
            db_session,
            minio_client=client,
            user=normal_user,
            dispatch=False,
        )

    assert summary.discovered == 2
    assert summary.skipped_existing == 1
    assert summary.registered == 1

    # Exactly one new row was created for the fresh key.
    rows = db_session.query(MediaFile).filter(MediaFile.storage_path == "media/1/fresh.mp4").all()
    assert len(rows) == 1


def test_dry_run_creates_no_rows(db_session, normal_user):
    client = _FakeMinio([_FakeObj("media/1/x.mp4", 1), _FakeObj("media/1/y.mp4", 2)])

    before = db_session.query(MediaFile).count()
    summary = recovery.reingest_objects(
        db_session,
        minio_client=client,
        user=normal_user,
        dry_run=True,
        dispatch=False,
    )
    after = db_session.query(MediaFile).count()

    assert summary.discovered == 2
    assert summary.registered == 2  # would-register count
    assert before == after  # but no rows actually created


def test_reingest_respects_limit(db_session, normal_user):
    client = _FakeMinio([_FakeObj(f"media/1/f{i}.mp4", i) for i in range(5)])
    with patch.object(recovery, "fingerprint_object", lambda mf: None):
        summary = recovery.reingest_objects(
            db_session,
            minio_client=client,
            user=normal_user,
            limit=2,
            dispatch=False,
        )
    assert summary.registered == 2


def test_reingest_dispatches_per_file(db_session, normal_user):
    client = _FakeMinio([_FakeObj("media/1/d.mp4", 5)])
    with (
        patch.object(recovery, "fingerprint_object", lambda mf: None),
        patch(
            "app.api.endpoints.files.upload.dispatch_upload_pipeline",
            return_value="task-1",
        ) as mock_dispatch,
    ):
        summary = recovery.reingest_objects(
            db_session,
            minio_client=client,
            user=normal_user,
            dispatch=True,
        )
    assert summary.dispatched == 1
    assert mock_dispatch.call_count == 1


# ---------------------------------------------------------------------------
# YouTube id discovery + duration matcher (pure logic)
# ---------------------------------------------------------------------------


def test_discover_youtube_ids():
    client = _FakeMinio(
        [
            _FakeObj("user_1/youtube_ABC123/thumbnail.jpg", 100),
            _FakeObj("user_1/youtube_XYZ789/thumbnail.jpg", 100),
            _FakeObj("user_1/youtube_ABC123/thumbnail.jpg", 100),  # dup id
            _FakeObj("user_1/vimeo_999/thumbnail.jpg", 100),  # not youtube
        ]
    )
    ids = recovery.discover_youtube_ids(client, user_id=1)
    assert ids == ["ABC123", "XYZ789"]


def test_match_metadata_by_duration_unique():
    rows = [
        SimpleNamespace(id=1, duration=120.0),
        SimpleNamespace(id=2, duration=300.0),
    ]
    metadata = {
        "AAA": {"youtube_id": "AAA", "title": "Vid A", "duration": 121.0},
        "BBB": {"youtube_id": "BBB", "title": "Vid B", "duration": 600.0},
    }
    matches = recovery.match_metadata_by_duration(rows, metadata)
    # row 1 (120s) uniquely matches AAA (121s, within 2s). row 2 (300s) matches none.
    assert matches == {1: metadata["AAA"]}


def test_match_metadata_by_duration_ambiguous_skipped():
    rows = [
        SimpleNamespace(id=1, duration=120.0),
        SimpleNamespace(id=2, duration=120.5),  # also near AAA -> ambiguous
    ]
    metadata = {"AAA": {"youtube_id": "AAA", "title": "Vid A", "duration": 121.0}}
    matches = recovery.match_metadata_by_duration(rows, metadata)
    # Two rows within tolerance of the same record -> reverse-uniqueness fails.
    assert matches == {}


def test_match_metadata_two_records_one_row_ambiguous():
    rows = [SimpleNamespace(id=1, duration=120.0)]
    metadata = {
        "AAA": {"youtube_id": "AAA", "title": "A", "duration": 120.5},
        "BBB": {"youtube_id": "BBB", "title": "B", "duration": 121.0},
    }
    # Two records within tolerance of the single row -> not unique, skip.
    assert recovery.match_metadata_by_duration(rows, metadata) == {}


def test_match_metadata_ignores_rows_without_duration():
    rows = [SimpleNamespace(id=1, duration=None)]
    metadata = {"AAA": {"youtube_id": "AAA", "title": "A", "duration": 120.0}}
    assert recovery.match_metadata_by_duration(rows, metadata) == {}


def test_apply_metadata_matches_updates_title(db_session, normal_user):
    mf = recovery.register_object(
        db_session, object_name="media/1/uuid.mp4", size=10, user=normal_user
    )
    db_session.commit()

    matches = {
        mf.id: {
            "title": "Real Title",
            "source_url": "https://www.youtube.com/watch?v=ABC",
            "uploader": "Some Channel",
        }
    }
    updated = recovery.apply_metadata_matches(db_session, matches)
    db_session.refresh(mf)

    assert updated == 1
    assert mf.title == "Real Title"
    assert mf.filename == "Real Title"
    assert mf.source_url == "https://www.youtube.com/watch?v=ABC"
    assert mf.author == "Some Channel"


def test_resolve_user_missing_raises(db_session):
    with pytest.raises(ValueError, match="No user found"):
        recovery.resolve_user(db_session, "nobody@nowhere.invalid")
