"""Parity tests for post-storage upload dispatch failures (issue #905).

Before this fix the two upload routes disagreed on what a dispatch failure (the
Celery pipeline kickoff that runs right after the bytes land) should do to the
``MediaFile`` row:

- The legacy multipart route (``files/upload.py::process_file_upload``) wrapped
  the ENTIRE request body — including the post-storage dispatch call — in one
  broad ``except Exception``, so a dispatch failure (including
  ``ASRConfigurationError``, issue #865) deleted the row and answered a bare 500,
  even though the bytes were already safely in MinIO.
- The presigned route (``files/complete_upload.py::complete_upload``) had NO
  exception handling around dispatch at all. ``ASRConfigurationError`` propagated
  to the global handler (503) but left the row's status as a side effect of
  nothing — it stayed at PENDING. Any OTHER exception was an unhandled 500 with
  the row stuck at PENDING **forever**: ``orphan_upload_sweeper`` deliberately
  skips a PENDING row whose object exists, so nothing else in the app could ever
  reach it again.

Both now funnel through ``dispatch_upload_pipeline_or_mark_error``: the row is
persisted at ``ERROR`` with ``last_error_message`` and the real exception is
re-raised unchanged, so ``ASRConfigurationError`` still reaches ``main.py``'s
503 handler.

``SKIP_CELERY=True`` is forced globally in this suite (see ``tests/CLAUDE.md``),
so the real Celery pipeline never runs regardless — these tests patch
``dispatch_upload_pipeline`` directly to simulate a failure at the dispatch
step itself, never letting the real pipeline touch the live dev broker/DB.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.core.exceptions import ASRConfigurationError
from app.models.media import FileStatus
from app.models.media import MediaFile

ASR_REFUSAL_MESSAGE = "No local ASR provider is configured on this deployment."


def _raise_asr_configuration_error(*_args, **_kwargs):
    raise ASRConfigurationError(ASR_REFUSAL_MESSAGE)


def _raise_generic_failure(*_args, **_kwargs):
    raise RuntimeError("broker unreachable")


def _seed_pending_file_with_storage_path(db_session, owner, filename: str) -> MediaFile:
    """A PENDING row with a real (but not-necessarily-uploaded) storage_path.

    ``content_type=""`` deliberately skips /complete's magic-byte re-validation
    branch (it is gated on a truthy ``declared_content_type``), so these tests
    don't need a live MinIO object to exercise the dispatch failure itself —
    only ``object_exists_and_size`` is patched to simulate the object landing.
    """
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename=filename,
        title=filename,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="",
        file_size=4096,
        status="pending",
        is_public=False,
        user_id=owner.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _post_legacy_upload(client, headers, sample_wav_bytes, filename: str):
    return client.post(
        "/api/files",
        headers=headers,
        files={"file": (filename, io.BytesIO(sample_wav_bytes), "audio/wav")},
    )


# ---------------------------------------------------------------------------
# 1 + 2: ASRConfigurationError must surface as 503 with the row kept at ERROR
# ---------------------------------------------------------------------------


def test_legacy_upload_keeps_the_row_at_error_when_asr_is_refused(
    client, user_token_headers, normal_user, sample_wav_bytes, db_session, monkeypatch
):
    from app.api.endpoints.files import upload as upload_mod

    filename = f"legacy-asr-refused-{uuid.uuid4().hex[:8]}.wav"
    monkeypatch.setattr(upload_mod, "upload_file_to_storage", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod, "dispatch_upload_pipeline", _raise_asr_configuration_error)

    response = _post_legacy_upload(client, user_token_headers, sample_wav_bytes, filename)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert ASR_REFUSAL_MESSAGE in response.json()["detail"]

    db_session.expire_all()
    row = (
        db_session.query(MediaFile)
        .filter(MediaFile.filename == filename, MediaFile.user_id == normal_user.id)
        .first()
    )
    assert row is not None, "a post-storage dispatch failure must NOT delete the row"
    assert row.status == FileStatus.ERROR
    assert row.last_error_message and ASR_REFUSAL_MESSAGE in row.last_error_message


def test_presigned_complete_keeps_the_row_at_error_when_asr_is_refused(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    from app.api.endpoints.files import upload as upload_mod

    seeded = _seed_pending_file_with_storage_path(
        db_session, normal_user, f"complete-asr-refused-{uuid.uuid4().hex[:8]}.wav"
    )
    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", lambda *a, **k: 4096)
    monkeypatch.setattr(upload_mod, "dispatch_upload_pipeline", _raise_asr_configuration_error)

    response = client.post(
        "/api/files/complete",
        headers=user_token_headers,
        json={"file_id": str(seeded.uuid)},
    )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert ASR_REFUSAL_MESSAGE in response.json()["detail"]

    db_session.expire_all()
    row = db_session.query(MediaFile).filter(MediaFile.uuid == seeded.uuid).first()
    assert row is not None, "a post-storage dispatch failure must NOT delete the row"
    assert row.status == FileStatus.ERROR
    assert row.last_error_message and ASR_REFUSAL_MESSAGE in row.last_error_message


# ---------------------------------------------------------------------------
# 3 + 4: a generic dispatch failure must ALSO keep the row at ERROR — this is
# the previously-unfiled bug: /complete had no handling at all, so the row
# was stuck at PENDING forever (invisible to orphan_upload_sweeper).
# ---------------------------------------------------------------------------


def test_legacy_upload_keeps_the_row_at_error_on_a_generic_dispatch_failure(
    app_for_raising_client,
    user_token_headers,
    normal_user,
    sample_wav_bytes,
    db_session,
    monkeypatch,
):
    from app.api.endpoints.files import upload as upload_mod

    filename = f"legacy-generic-failure-{uuid.uuid4().hex[:8]}.wav"
    monkeypatch.setattr(upload_mod, "upload_file_to_storage", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod, "dispatch_upload_pipeline", _raise_generic_failure)

    response = _post_legacy_upload(
        app_for_raising_client, user_token_headers, sample_wav_bytes, filename
    )

    # Accepted behavior change: a generic (non-ASRConfigurationError) dispatch
    # failure now answers a plain FastAPI 500 with NO custom detail string —
    # the exception text is deliberately NOT asserted in the response body,
    # only preserved server-side on the row.
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    db_session.expire_all()
    row = (
        db_session.query(MediaFile)
        .filter(MediaFile.filename == filename, MediaFile.user_id == normal_user.id)
        .first()
    )
    assert row is not None, "a post-storage dispatch failure must NOT delete the row"
    assert row.status == FileStatus.ERROR
    assert row.last_error_message and "broker unreachable" in row.last_error_message


def test_presigned_complete_keeps_the_row_at_error_on_a_generic_dispatch_failure(
    app_for_raising_client, user_token_headers, normal_user, db_session, monkeypatch
):
    """This is the test that catches the previously-unfiled PENDING-forever bug."""
    from app.api.endpoints.files import upload as upload_mod

    seeded = _seed_pending_file_with_storage_path(
        db_session, normal_user, f"complete-generic-failure-{uuid.uuid4().hex[:8]}.wav"
    )
    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", lambda *a, **k: 4096)
    monkeypatch.setattr(upload_mod, "dispatch_upload_pipeline", _raise_generic_failure)

    response = app_for_raising_client.post(
        "/api/files/complete",
        headers=user_token_headers,
        json={"file_id": str(seeded.uuid)},
    )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    db_session.expire_all()
    row = db_session.query(MediaFile).filter(MediaFile.uuid == seeded.uuid).first()
    assert row is not None, "the row must not be stuck at PENDING forever"
    assert row.status == FileStatus.ERROR, "must not still be PENDING (the unfiled bug)"
    assert row.last_error_message and "broker unreachable" in row.last_error_message


# ---------------------------------------------------------------------------
# 5: both routes must agree on the outcome for the same kind of refusal
# ---------------------------------------------------------------------------


def test_both_upload_paths_report_the_same_outcome_for_one_refusal(
    client, user_token_headers, normal_user, sample_wav_bytes, db_session, monkeypatch
):
    from app.api.endpoints.files import upload as upload_mod

    monkeypatch.setattr(upload_mod, "upload_file_to_storage", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod, "dispatch_upload_pipeline", _raise_asr_configuration_error)

    legacy_filename = f"parity-legacy-{uuid.uuid4().hex[:8]}.wav"
    legacy = _post_legacy_upload(client, user_token_headers, sample_wav_bytes, legacy_filename)

    seeded = _seed_pending_file_with_storage_path(
        db_session, normal_user, f"parity-presigned-{uuid.uuid4().hex[:8]}.wav"
    )
    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", lambda *a, **k: 4096)
    presigned = client.post(
        "/api/files/complete",
        headers=user_token_headers,
        json={"file_id": str(seeded.uuid)},
    )

    assert legacy.status_code == presigned.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    db_session.expire_all()
    legacy_row = (
        db_session.query(MediaFile)
        .filter(MediaFile.filename == legacy_filename, MediaFile.user_id == normal_user.id)
        .first()
    )
    presigned_row = db_session.query(MediaFile).filter(MediaFile.uuid == seeded.uuid).first()
    assert legacy_row is not None and presigned_row is not None
    assert legacy_row.status == FileStatus.ERROR
    assert presigned_row.status == FileStatus.ERROR


# ---------------------------------------------------------------------------
# 6 + 7: boundary guard — a PRE-storage rejection is unchanged: the row is
# still dropped. No dispatch patching here at all.
# ---------------------------------------------------------------------------


def test_legacy_upload_still_drops_the_row_when_the_bytes_are_rejected(
    client, user_token_headers, normal_user, db_session
):
    filename = f"legacy-rejected-{uuid.uuid4().hex[:8]}.wav"
    garbage = b"NOTRIFFDATA" + b"\x01\x02\x03\x04" * 8  # not a valid RIFF/WAV header
    response = _post_legacy_upload(client, user_token_headers, garbage, filename)

    assert response.status_code == status.HTTP_400_BAD_REQUEST

    db_session.expire_all()
    row = (
        db_session.query(MediaFile)
        .filter(MediaFile.filename == filename, MediaFile.user_id == normal_user.id)
        .first()
    )
    assert row is None, "a pre-storage rejection must still drop the row"


def test_presigned_complete_still_drops_the_row_when_the_object_is_oversized(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    from app.core.config import settings as app_settings

    seeded = _seed_pending_file_with_storage_path(
        db_session, normal_user, f"complete-oversized-{uuid.uuid4().hex[:8]}.wav"
    )
    assert app_settings.MAX_UPLOAD_BYTES is not None, "the ceiling must be enabled for this test"
    oversized = app_settings.MAX_UPLOAD_BYTES + 1
    monkeypatch.setattr(
        "app.services.minio_service.object_exists_and_size", lambda *a, **k: oversized
    )
    monkeypatch.setattr("app.services.minio_service.delete_file", lambda *a, **k: None)

    response = client.post(
        "/api/files/complete",
        headers=user_token_headers,
        json={"file_id": str(seeded.uuid)},
    )

    assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE

    db_session.expire_all()
    row = db_session.query(MediaFile).filter(MediaFile.uuid == seeded.uuid).first()
    assert row is None, "an oversized object must still drop the row"


# ---------------------------------------------------------------------------
# Fixture: a TestClient that does NOT re-raise unhandled server exceptions,
# needed for the generic-failure tests (3/4) since the FastAPI default for an
# uncaught exception is a 500 response — but httpx's default TestClient
# transport re-raises the original exception into the TEST process instead of
# returning it as a response (see tests/api/test_observability_middleware.py's
# identical pattern).
# ---------------------------------------------------------------------------


@pytest.fixture
def app_for_raising_client(client):
    """A second TestClient over the SAME app/dependency-overrides as ``client``,
    but with ``raise_server_exceptions=False`` so an unhandled exception in the
    endpoint comes back as a real 500 response instead of propagating into the
    test process. Depends on ``client`` purely to ensure its ``get_db``
    override is already installed on the shared ``app`` object.
    """
    from app.main import app as fastapi_app

    with TestClient(fastapi_app, raise_server_exceptions=False) as tc:
        yield tc
