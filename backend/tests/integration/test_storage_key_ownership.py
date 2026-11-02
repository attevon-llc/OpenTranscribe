"""Every stored object key has exactly one owning row (issue #1357).

Real MinIO (a throwaway bucket ``settings.MEDIA_BUCKET_NAME`` is pointed at) and real Postgres.

* A URL import's thumbnail is keyed by its media file, so re-importing a video that is being
  purged cannot share the purged row's object.
* The database refuses a second row naming a key (``uq_media_file_*``).
* Each avatar upload writes its own key and only deletes the key it replaced.
"""

from __future__ import annotations

import io
import os
import threading
import uuid as uuid_pkg
from collections.abc import Iterator

import pytest
import requests
from fastapi import status
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models.media import MediaFile
from app.models.media import SpeakerProfile
from tests.helpers import stub_public_dns

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="needs a reachable MinIO (export MINIO_PORT)",
    ),
]

_JPEG = b"\xff\xd8\xff\xe0thumbnail-jpeg"


@pytest.fixture
def bucket(monkeypatch) -> Iterator[str]:
    """A throwaway bucket; emptied (every version) and removed even on failure."""
    from minio.deleteobjects import DeleteObject

    from app.core.config import settings
    from app.services.minio_service import minio_client

    name = f"e2e-key-owner-{uuid_pkg.uuid4().hex[:12]}"
    monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", name)
    try:
        minio_client.make_bucket(name)
        yield name
    finally:
        if minio_client.bucket_exists(name):
            names = [
                DeleteObject(o.object_name, o.version_id)
                for o in minio_client.list_objects(name, recursive=True, include_version=True)
            ]
            if names:
                list(minio_client.remove_objects(name, names))
            minio_client.remove_bucket(name)


def _exists(bucket: str, key: str) -> bool:
    from minio.error import S3Error

    from app.services.minio_service import minio_client

    try:
        minio_client.stat_object(bucket, key)
    except S3Error:
        return False
    return True


def _keys(bucket: str, prefix: str) -> list[str]:
    from app.services.minio_service import minio_client

    return sorted(o.object_name for o in minio_client.list_objects(bucket, prefix, recursive=True))


def _row(db, owner, **fields) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"keys_{file_uuid.hex[:8]}.mp4",
        storage_path=f"user_{owner.id}/keys_{file_uuid}/original.mp4",
        content_type="video/mp4",
        file_size=1,
        status="completed",
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _import_thumbnail(monkeypatch, row: MediaFile, user_id: int) -> str:
    """Run the real URL-import thumbnail download for ``row`` against a stubbed CDN."""
    from app.services.media_download_service import MediaDownloadService

    stub_public_dns(monkeypatch, domain="thumbs.example.com")

    def fake_send(self, request, *args, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.headers = CaseInsensitiveDict({})
        resp._content = _JPEG
        resp._content_consumed = True
        resp.url = request.url or ""
        resp.request = request
        return resp

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    path = MediaDownloadService()._download_media_thumbnail_sync(
        {"thumbnail": "https://thumbs.example.com/t.jpg", "id": "abc123", "extractor": "Youtube"},
        user_id,
        media_file_id=int(row.id),
    )
    assert path
    return path


def test_reimport_between_purge_phases_keeps_the_new_thumbnail(
    db_session, normal_user, bucket, monkeypatch
):
    """Re-importing a video after a purge planned its delete must not lose the new thumbnail."""
    from app.services.file_cleanup_service import _load_purge_plan
    from app.services.file_cleanup_service import delete_file_storage_artifacts

    old = _row(db_session, normal_user)
    old_key = _import_thumbnail(monkeypatch, old, int(normal_user.id))
    old.thumbnail_path = old_key
    db_session.commit()

    plan = _load_purge_plan(db_session, old)
    db_session.commit()

    new = _row(db_session, normal_user)
    new_key = _import_thumbnail(monkeypatch, new, int(normal_user.id))
    new.thumbnail_path = new_key
    db_session.commit()

    delete_file_storage_artifacts(
        plan["file_id"],
        {
            "filename": plan["filename"],
            "storage_path": plan["storage_path"],
            "thumbnail_path": plan["thumbnail_path"],
            "playback_path": plan.get("playback_path"),
        },
    )

    assert new_key != old_key
    assert not _exists(bucket, old_key)
    assert _exists(bucket, new_key), "the new import's thumbnail was destroyed by the old purge"


@pytest.mark.parametrize("column", ["storage_path", "thumbnail_path", "playback_path"])
def test_two_rows_cannot_name_one_object_key(db_session, normal_user, column):
    key = f"user_{normal_user.id}/shared/{uuid_pkg.uuid4().hex}.bin"
    first = _row(db_session, normal_user)
    second = _row(db_session, normal_user)
    setattr(first, column, key)
    db_session.commit()

    setattr(second, column, key)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_placeholders_and_nulls_are_not_keys(db_session, normal_user):
    """Rows with no object yet ('' or NULL) may repeat; only real keys are unique."""
    a, b = _row(db_session, normal_user), _row(db_session, normal_user)
    a.storage_path = b.storage_path = ""
    a.thumbnail_path = b.thumbnail_path = None
    a.playback_path = b.playback_path = ""
    db_session.commit()

    blank = (
        db_session.query(MediaFile)
        .filter(MediaFile.id.in_([a.id, b.id]), MediaFile.storage_path == "")
        .count()
    )
    assert blank == 2


def _upload_avatar(client, headers, profile, content_type: str, name: str):
    return client.post(
        f"/api/speaker-profiles/profiles/{profile.uuid}/avatar",
        headers=headers,
        files={"file": (name, io.BytesIO(b"\x89PNG-or-jpeg-bytes"), content_type)},
    )


@pytest.fixture
def avatar_profile(db_session, normal_user) -> Iterator[SpeakerProfile]:
    """A profile whose avatar objects (every version) are removed afterwards.

    Uses the stack's real media bucket: avatar URLs are presigned by an identity scoped to it.
    """
    from minio.deleteobjects import DeleteObject

    from app.core.config import settings
    from app.services.minio_service import minio_client

    profile = SpeakerProfile(user_id=normal_user.id, name=f"Avatar {uuid_pkg.uuid4().hex[:6]}")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    try:
        yield profile
    finally:
        found = minio_client.list_objects(
            settings.MEDIA_BUCKET_NAME,
            f"avatars/{normal_user.id}/{profile.uuid}",
            recursive=True,
            include_version=True,
        )
        names = [DeleteObject(o.object_name, o.version_id) for o in found]
        if names:
            list(minio_client.remove_objects(settings.MEDIA_BUCKET_NAME, names))


def test_a_racing_avatar_upload_leaves_exactly_one_referenced_object(
    client, user_token_headers, normal_user, db_session, avatar_profile, monkeypatch
):
    """A second upload lands after the first wrote its object and before it updates the row."""
    from app.api.endpoints import speaker_profiles
    from app.core.config import settings

    profile = avatar_profile
    bucket = settings.MEDIA_BUCKET_NAME
    prefix = f"avatars/{normal_user.id}/{profile.uuid}"
    real_run = speaker_profiles.run_in_threadpool
    state = {"raced": False}

    async def racing_run(func, *args, **kwargs):
        result = await real_run(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "upload_file" and not state["raced"]:
            state["raced"] = True
            outcome: dict[str, int] = {}

            def second() -> None:
                # A fresh client: `client` holds one event loop, which this thread's join blocks.
                other = TestClient(app)
                resp = _upload_avatar(other, user_token_headers, profile, "image/png", "b.png")
                outcome["status"] = resp.status_code

            worker = threading.Thread(target=second)
            worker.start()
            worker.join()
            assert outcome["status"] == status.HTTP_200_OK
        return result

    monkeypatch.setattr(speaker_profiles, "run_in_threadpool", racing_run)

    first = _upload_avatar(client, user_token_headers, profile, "image/jpeg", "a.jpg")
    assert first.status_code == status.HTTP_200_OK
    assert state["raced"], "the race window was never opened"

    db_session.refresh(profile)
    assert profile.avatar_path
    assert _exists(bucket, str(profile.avatar_path)), "the profile points at a missing object"
    assert _keys(bucket, prefix) == [profile.avatar_path], "a superseded avatar object leaked"


def test_replacing_an_avatar_deletes_only_the_key_it_replaced(
    client, user_token_headers, normal_user, db_session, avatar_profile
):
    from app.core.config import settings

    profile = avatar_profile
    prefix = f"avatars/{normal_user.id}/{profile.uuid}"
    for ctype, name in (("image/jpeg", "a.jpg"), ("image/png", "b.png"), ("image/jpeg", "c.jpg")):
        assert _upload_avatar(client, user_token_headers, profile, ctype, name).status_code == 200
        db_session.refresh(profile)
        assert _keys(settings.MEDIA_BUCKET_NAME, prefix) == [profile.avatar_path]

    resp = client.delete(
        f"/api/speaker-profiles/profiles/{profile.uuid}/avatar", headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert _keys(settings.MEDIA_BUCKET_NAME, prefix) == []
