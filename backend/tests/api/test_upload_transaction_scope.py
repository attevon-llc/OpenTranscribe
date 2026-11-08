"""``POST /api/files`` holds no transaction while the bytes move (audit H5).

The route used to flush the new ``MediaFile`` row and keep that transaction open across
the whole object-store upload, committing only afterwards. A large upload therefore held
a pooled connection idle in a transaction for minutes (past the server's idle timeout,
which then failed the upload and left an orphan object), and on the prepared-upload path
kept the existing row locked the whole time.

Now the row is committed as ``pending`` and the session closed before the upload; a
second short transaction records the storage path, and a failure deletes the row (and
the object, when it was already stored).
"""

from __future__ import annotations

import io
import uuid

from fastapi import status
from sqlalchemy.exc import OperationalError

from app.models.media import MediaFile


def _post(client, headers, data: bytes, filename: str):
    return client.post(
        "/api/files",
        headers=headers,
        files={"file": (filename, io.BytesIO(data), "audio/wav")},
    )


def _rows(db_session, owner_id: int, filename: str) -> list[MediaFile]:
    db_session.expire_all()
    rows: list[MediaFile] = (
        db_session.query(MediaFile)
        .filter(MediaFile.filename == filename, MediaFile.user_id == owner_id)
        .all()
    )
    return rows


def test_no_transaction_is_open_while_the_object_uploads(
    client, user_token_headers, normal_user, sample_wav_bytes, db_session, monkeypatch
):
    # The upload closes the shared session, detaching fixture rows; read the id first.
    owner_id = normal_user.id
    from app.api.endpoints.files import upload as upload_mod
    from app.api.endpoints.files import upload_dispatch as dispatch_mod

    observed: list[bool] = []

    def _upload(*_args, **_kwargs):
        observed.append(db_session.in_transaction())

    monkeypatch.setattr(upload_mod, "upload_file_to_storage", _upload)
    monkeypatch.setattr(dispatch_mod, "dispatch_upload_pipeline", lambda *a, **k: None)
    filename = f"tx-scope-{uuid.uuid4().hex[:8]}.wav"

    response = _post(client, user_token_headers, sample_wav_bytes, filename)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert observed == [False], "the request session had a transaction open during the upload"
    (row,) = _rows(db_session, owner_id, filename)
    assert row.storage_path, "the second transaction must record where the bytes landed"
    assert row.file_size == len(sample_wav_bytes)


def test_a_failed_upload_leaves_no_row(
    client, user_token_headers, normal_user, sample_wav_bytes, db_session, monkeypatch
):
    # The upload closes the shared session, detaching fixture rows; read the id first.
    owner_id = normal_user.id
    from app.api.endpoints.files import upload as upload_mod

    def _storage_down(*_args, **_kwargs):
        raise ConnectionError("object store unreachable")

    monkeypatch.setattr(upload_mod, "upload_file_to_storage", _storage_down)
    filename = f"tx-fail-{uuid.uuid4().hex[:8]}.wav"

    response = _post(client, user_token_headers, sample_wav_bytes, filename)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert _rows(db_session, owner_id, filename) == []


def test_a_rejected_upload_leaves_no_row(client, user_token_headers, normal_user, db_session):
    # The upload closes the shared session, detaching fixture rows; read the id first.
    owner_id = normal_user.id
    filename = f"tx-reject-{uuid.uuid4().hex[:8]}.wav"

    response = _post(client, user_token_headers, b"definitely not a wav file" * 10, filename)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert _rows(db_session, owner_id, filename) == []


def test_a_failure_after_storage_removes_the_object_and_the_row(
    client, user_token_headers, normal_user, sample_wav_bytes, db_session, monkeypatch
):
    # The upload closes the shared session, detaching fixture rows; read the id first.
    owner_id = normal_user.id
    from app.api.endpoints.files import upload as upload_mod
    from app.services import minio_service
    from app.services.minio_service import QuarantineTagWrite

    removed: list[str] = []

    def _remove(_bucket, object_name, **_kwargs):
        removed.append(object_name)
        return QuarantineTagWrite("removed")

    def _commit_fails(*_args, **_kwargs):
        raise OperationalError("UPDATE media_file", {}, Exception("connection reset"))

    monkeypatch.setenv("SKIP_S3", "false")
    monkeypatch.setattr(upload_mod, "upload_file_to_storage", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod, "_finish_upload_record", _commit_fails)
    monkeypatch.setattr(minio_service, "remove_object_reported", _remove)
    filename = f"tx-orphan-{uuid.uuid4().hex[:8]}.wav"

    response = _post(client, user_token_headers, sample_wav_bytes, filename)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert _rows(db_session, owner_id, filename) == []
    assert len(removed) == 1 and removed[0].endswith(".wav"), removed
