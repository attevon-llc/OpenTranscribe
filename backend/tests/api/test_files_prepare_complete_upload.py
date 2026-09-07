"""Characterization tests for the two-phase presigned upload endpoints.

Covers:
- ``POST /api/files/prepare``  (``files/prepare_upload.py``)
- ``POST /api/files/complete`` (``files/complete_upload.py``)

``/prepare`` creates the MediaFile row and (optionally) mints a presigned PUT
URL; ``/complete`` verifies the object landed in MinIO, fingerprints it, and
dispatches the pipeline. The pure-DB branches (duplicate-by-hash short-circuit,
validation, non-existent / missing-storage_path) run ungated. The genuine
round-trip (presigned PUT → /complete) only runs when MinIO is reachable
(``SKIP_S3=False``, auto-detected by conftest); the prepared row + any uploaded
object are cleaned up so dev data/storage stay untouched.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"


def _prepare_payload(**overrides) -> dict:
    payload = {
        "filename": "prep.wav",
        "file_size": 4096,
        "content_type": "audio/wav",
    }
    payload.update(overrides)
    return payload


def _seed_existing_file(db_session, owner, file_hash: str | None) -> MediaFile:
    """Persist a completed, fully-uploaded file with ``file_hash`` so the
    duplicate-by-hash branch in /prepare can find it (it requires a real
    storage_path and a non-failed status)."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="original.wav",
        title="original",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        file_hash=file_hash,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# POST /api/files/prepare
# ---------------------------------------------------------------------------


def test_prepare_unauthorized(client):
    response = client.post("/api/files/prepare", json=_prepare_payload())
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_prepare_happy_creates_record(client, user_token_headers):
    """Plain prepare returns the new file UUID and is_duplicate=0."""
    response = client.post(
        "/api/files/prepare", headers=user_token_headers, json=_prepare_payload()
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 0
    assert "file_id" in body
    # Valid UUID string.
    uuid.UUID(body["file_id"])
    # No presigned fields when use_presigned was not requested.
    assert "upload_url" not in body
    # No manual cleanup needed: /prepare commits onto the nested savepoint, which
    # the db_session fixture rolls back at teardown — the row never reaches dev data.


def test_prepare_missing_filename_422(client, user_token_headers):
    payload = _prepare_payload()
    del payload["filename"]
    response = client.post("/api/files/prepare", headers=user_token_headers, json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_prepare_missing_file_size_422(client, user_token_headers):
    payload = _prepare_payload()
    del payload["file_size"]
    response = client.post("/api/files/prepare", headers=user_token_headers, json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_prepare_missing_content_type_422(client, user_token_headers):
    payload = _prepare_payload()
    del payload["content_type"]
    response = client.post("/api/files/prepare", headers=user_token_headers, json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_prepare_bad_collection_uuid_422(client, user_token_headers):
    """collection_ids is typed list[UUID]; a non-UUID member is a validation error."""
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(collection_ids=["not-a-uuid"]),
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_prepare_duplicate_by_hash_returns_existing(
    client, user_token_headers, normal_user, db_session
):
    """When a prior file shares the hash, /prepare short-circuits to it.

    Returns ``{"file_id": <existing_uuid>, "is_duplicate": 1}`` and does NOT
    create a new row. The seed file is created on the savepoint session so it
    rolls back; because /prepare uses the SAME overridden session it sees it.
    """
    digest = uuid.uuid4().hex
    existing = _seed_existing_file(db_session, normal_user, digest)

    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(filename="copy.wav", file_hash=digest),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 1
    assert body["file_id"] == str(existing.uuid)


def test_prepare_unknown_hash_not_duplicate(client, user_token_headers):
    """A hash with no prior match creates a fresh record (is_duplicate=0)."""
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(file_hash=uuid.uuid4().hex),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 0


def test_prepare_duplicate_matches_server_computed_imohash(
    client, user_token_headers, normal_user, db_session
):
    """The gate also matches ``MediaFile.imohash``, not just ``file_hash``.

    Since issue #342 the browser sends the *same* constant-time imohash the server
    computes from the stored object, so the whole existing library — every row
    populated by ``/complete``, URL import, watch sources or the recompute backfill —
    is reachable by the pre-upload check even though those rows carry a legacy
    SHA-256 (or nothing) in ``file_hash``. Without this branch the algorithm switch
    would silently stop deduplicating everything uploaded before it.
    """
    fingerprint = uuid.uuid4().hex[:32]
    existing = _seed_existing_file(db_session, normal_user, file_hash=None)
    existing.imohash = fingerprint
    db_session.commit()

    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(filename="copy.wav", file_hash=fingerprint),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 1
    assert body["file_id"] == str(existing.uuid)


def test_prepare_duplicate_does_not_leak_another_users_file(
    client, user_token_headers, other_user, db_session
):
    """A fingerprint owned by a different user is not a duplicate for this one.

    The gate hands the caller the matching file's UUID, so an unscoped lookup would
    both disclose that another tenant holds the content and point the caller at a
    row they cannot read.
    """
    fingerprint = uuid.uuid4().hex[:32]
    foreign = _seed_existing_file(db_session, other_user, file_hash=fingerprint)
    foreign.imohash = fingerprint
    db_session.commit()

    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(filename="copy.wav", file_hash=fingerprint),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 0
    assert body["file_id"] != str(foreign.uuid)


@pytest.mark.skipif(not S3_LIVE, reason="presigned PUT URL requires MinIO (SKIP_S3=False)")
def test_prepare_presigned_returns_upload_url(client, user_token_headers):
    """use_presigned=true adds the presigned PUT URL + storage_path + task_id.

    The URL is signed (AWS Signature V4 query params). In the dev stack
    MINIO_PUBLIC_URL is unset, so the URL is the relative ``/s3`` proxy path
    rather than an absolute host — characterize that exactly.
    """
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(use_presigned=True),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 0
    assert body["upload_method"] == "PUT"
    assert body["http_flow"] == "presigned"
    assert body["upload_url"]
    assert "X-Amz-Signature" in body["upload_url"]
    assert body["storage_path"]
    assert body["task_id"]


def _abort(body: dict) -> None:
    """Release the multipart upload /prepare just created.

    Storage bills for the parts of an incomplete upload, and these tests run
    against the live dev MinIO — leaving one behind would be leaving litter in
    real storage.
    """
    from app.services.multipart_upload import abort_upload

    abort_upload(body["storage_path"], body["multipart"]["upload_id"])


@pytest.mark.skipif(not S3_LIVE, reason="creating a multipart upload requires MinIO")
def test_prepare_uses_multipart_above_the_s3_single_put_limit(
    client, user_token_headers, monkeypatch
):
    """On native S3 a >5 GiB object cannot be uploaded with one PUT (issue #284 A1.11).

    S3 answers ``EntityTooLarge`` only after the browser has streamed the whole body.
    /prepare used to withhold the URL and let the client fall back to ``POST /files``,
    pushing the whole body through the API container; since #327 it hands out a
    presigned *multipart* plan instead.
    """
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "STORAGE_BACKEND", "s3")

    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(
            filename="huge.mp4",
            file_size=6 * 1024**3,
            content_type="video/mp4",
            use_presigned=True,
        ),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_duplicate"] == 0
    assert "upload_url" not in body
    assert body["upload_method"] == "MULTIPART"
    assert body["multipart"]["part_count"] == 96  # 6 GiB / 64 MiB
    assert body["multipart"]["upload_id"]
    _abort(body)


@pytest.mark.skipif(not S3_LIVE, reason="creating a multipart upload requires MinIO")
def test_prepare_uses_multipart_for_large_files_on_minio(client, user_token_headers):
    """MinIO's 5 TiB single-PUT ceiling means multipart here is about resume, not size.

    A 6 GiB single PUT that dies at 90% restarts at zero; the same upload in 64 MiB
    parts loses one part.
    """
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(
            filename="huge-minio.mp4",
            file_size=6 * 1024**3,
            content_type="video/mp4",
            use_presigned=True,
        ),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["upload_method"] == "MULTIPART"
    assert body["http_flow"] == "presigned-multipart"
    _abort(body)


@pytest.mark.skipif(not S3_LIVE, reason="presigned PUT URL requires MinIO (SKIP_S3=False)")
def test_prepare_keeps_the_single_put_path_below_the_threshold(client, user_token_headers):
    """Small uploads must not get more complicated: one PUT, no plan to execute."""
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(
            filename="small.mp4",
            file_size=32 * 1024**2,
            content_type="video/mp4",
            use_presigned=True,
        ),
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["upload_method"] == "PUT"
    assert "multipart" not in body


# ---------------------------------------------------------------------------
# POST /api/files/complete
# ---------------------------------------------------------------------------


def test_complete_unauthorized(client):
    response = client.post("/api/files/complete", json={"file_id": str(uuid.uuid4())})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_complete_missing_file_id_422(client, user_token_headers):
    """file_id is required on CompleteUploadRequest."""
    response = client.post("/api/files/complete", headers=user_token_headers, json={})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_complete_nonexistent_file_404(client, user_token_headers):
    """A file_id with no matching row for the caller is a 404 with the
    'not found for user' detail (note: this is NOT the 'File not found' string —
    complete_upload uses its own message)."""
    missing = str(uuid.uuid4())
    response = client.post(
        "/api/files/complete", headers=user_token_headers, json={"file_id": missing}
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == f"MediaFile {missing} not found for user"


def test_complete_other_users_file_404(client, other_user_auth_headers, normal_user, db_session):
    """A file owned by someone else is invisible to /complete (filtered by
    user_id) → the same 404 as a nonexistent file (NOT a 403)."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="theirs.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="pending",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()

    response = client.post(
        "/api/files/complete", headers=other_user_auth_headers, json={"file_id": file_uuid}
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == f"MediaFile {file_uuid} not found for user"


def test_complete_no_storage_path_400(client, user_token_headers, normal_user, db_session):
    """A row created without a storage_path (prepare not run with use_presigned)
    is a 400 explaining the missing path."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="nostorage.wav",
        storage_path="",  # empty → falsy → the no-storage_path guard fires
        content_type="audio/wav",
        file_size=4096,
        status="pending",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()

    response = client.post(
        "/api/files/complete", headers=user_token_headers, json={"file_id": file_uuid}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "no storage_path" in response.json()["detail"]


def test_complete_object_never_uploaded_400(client, user_token_headers, normal_user, db_session):
    """A prepared row whose presigned PUT never completed (no MinIO object) is a
    400 and the orphan row is dropped. Requires MinIO to be reachable so
    ``object_exists_and_size`` returns None for a real (absent) key.

    Skips when MinIO is down because the no-object check is then unverifiable.
    """
    if not S3_LIVE:
        pytest.skip("object existence check requires MinIO (SKIP_S3=False)")

    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="orphan.wav",
        storage_path=f"media/test/never-uploaded-{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="pending",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()

    response = client.post(
        "/api/files/complete", headers=user_token_headers, json={"file_id": file_uuid}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "did not complete successfully" in response.json()["detail"]


def test_a_stat_object_outage_does_not_delete_the_file_row(
    client, user_token_headers, normal_user, db_session
):
    """B1: a transient storage error from stat_object (MinIO restart, network
    hiccup) must NOT be treated as proof the object is missing. It used to be
    swallowed into ``None`` — indistinguishable from a genuine NoSuchKey — and
    ``/complete`` deleted the user's already-uploaded ``media_file`` row and
    told them the upload failed, while the bytes sat safely in the bucket."""
    from minio.error import S3Error

    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="outage.wav",
        storage_path=f"media/test/outage-{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="pending",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()

    outage = S3Error(
        response=None,
        code="InternalError",
        message="simulated storage outage",
        resource=f"/{file_uuid}",
        request_id="test",
        host_id="test",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.minio_service.minio_client.stat_object",
            lambda *a, **k: (_ for _ in ()).throw(outage),
        )
        response = client.post(
            "/api/files/complete", headers=user_token_headers, json={"file_id": file_uuid}
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_session.expire_all()
    still_there = db_session.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
    assert still_there is not None
    assert still_there.status == "pending"


def test_a_minio_unreachable_outage_does_not_delete_the_file_row(
    client, user_token_headers, normal_user, db_session
):
    """B1 (adversarial-review follow-up): the exception a REAL MinIO-down
    scenario raises is not ``S3Error`` — minio-py's ``stat_object`` calls
    urllib3's ``PoolManager.urlopen`` directly, and when the connection is
    refused/unreachable urllib3 retries internally and then raises
    ``urllib3.exceptions.MaxRetryError``, which is not a subclass of
    ``MinioException``/``S3Error`` at all (confirmed empirically: pointing a
    real ``Minio`` client at a closed port raises exactly this type).
    Catching only ``S3Error`` let this specific outage shape propagate as an
    unhandled 500 instead of the intended 503-please-retry, and — worse —
    left it indistinguishable from the codepath that deletes an orphan row,
    since nothing caught it before request teardown."""
    from urllib3.connection import HTTPConnection
    from urllib3.connectionpool import HTTPConnectionPool
    from urllib3.exceptions import MaxRetryError
    from urllib3.exceptions import NewConnectionError

    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="unreachable.wav",
        storage_path=f"media/test/unreachable-{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="pending",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()

    pool_error = NewConnectionError(
        HTTPConnection("localhost"), "Failed to establish a new connection: refused"
    )
    outage = MaxRetryError(HTTPConnectionPool("localhost"), "/bucket/object", reason=pool_error)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.minio_service.minio_client.stat_object",
            lambda *a, **k: (_ for _ in ()).throw(outage),
        )
        response = client.post(
            "/api/files/complete", headers=user_token_headers, json={"file_id": file_uuid}
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_session.expire_all()
    still_there = db_session.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
    assert still_there is not None
    assert still_there.status == "pending"


@pytest.mark.skipif(not S3_LIVE, reason="full presigned round-trip requires MinIO (SKIP_S3=False)")
def test_prepare_then_complete_round_trip(client, user_token_headers, sample_wav_bytes):
    """End-to-end presigned flow: prepare → land bytes in MinIO → complete.

    The dev stack's presigned URL is the relative ``/s3`` proxy path (no host),
    so we stage the object directly via the internal MinIO client at the
    server-chosen ``storage_path`` — this still genuinely exercises /complete's
    object-existence + magic-byte + fingerprint verification. Celery dispatch is
    stubbed (SKIP_CELERY=True), so we assert the response contract: status flips
    to 'pending', the server-observed size + imohash come back. The MinIO object
    is removed afterward (the DB row rolls back with the savepoint).
    """
    import io

    from app.core.config import settings
    from app.services.minio_service import minio_client

    prep = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json=_prepare_payload(
            filename="roundtrip.wav", file_size=len(sample_wav_bytes), use_presigned=True
        ),
    )
    assert prep.status_code == status.HTTP_200_OK, prep.json()
    prep_body = prep.json()
    file_id = prep_body["file_id"]
    task_id = prep_body["task_id"]
    storage_path = prep_body["storage_path"]

    # Stage the bytes at the prepared storage_path (stands in for the browser's
    # presigned PUT — /complete only checks that an object exists there).
    minio_client.put_object(
        settings.MEDIA_BUCKET_NAME,
        storage_path,
        io.BytesIO(sample_wav_bytes),
        length=len(sample_wav_bytes),
        content_type="audio/wav",
    )

    try:
        complete = client.post(
            "/api/files/complete",
            headers=user_token_headers,
            json={
                "file_id": file_id,
                "task_id": task_id,
                "file_size": len(sample_wav_bytes),
            },
        )
        assert complete.status_code == status.HTTP_200_OK, complete.json()
        body = complete.json()
        assert body["file_id"] == file_id
        assert body["status"] == "pending"
        assert body["file_size"] == len(sample_wav_bytes)
    finally:
        try:
            minio_client.remove_object(settings.MEDIA_BUCKET_NAME, storage_path)
        except Exception:
            pass  # best-effort cleanup of the staged test object
