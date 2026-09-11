"""Issue #872 — reprocess must not delete a transcript it cannot regenerate.

``reset_file_for_retry`` (``app/utils/task_utils.py``) deletes a file's
``TranscriptSegment``, ``Speaker`` and ``Analytics`` rows and then **commits**,
synchronously, before any Celery task is dispatched. Both reprocess call sites
(``files/reprocess.py`` — the full path and the selective path when
``"transcription"`` is among the stages) reach it.

The only gate in front of that was a *truthiness* check on ``storage_path``.
A row whose ``storage_path`` is SET but resolves to no object in storage — a
corpus-injected transcript, or any deployment whose Postgres rows outlived their
MinIO objects (``backup_database`` is ``pg_dump``-only and the Media Mirror is
default-OFF) — sailed through it and lost its only copy of the transcript with
nothing left to regenerate it from.

⚠️ **Assert on the surviving row counts, not only on the status code.** On the
unfixed code the transcript is already gone by the time the response is written,
so a test that checks the status alone proves the wrong thing.

The storage probe is stubbed rather than driven against real MinIO so the
outcome is identical with the dev stack up (``SKIP_S3=False``) and on a bare CI
runner (``SKIP_S3=True``); only the external stat call is replaced, the guard
itself runs for real. Both directions are exercised — a present object must
still let the reprocess through, or the guard would pass by refusing everything.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import Analytics
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.utils.task_utils import reset_file_for_retry


def _make_file_with_transcript(db_session, owner, **overrides) -> MediaFile:
    """A COMPLETED file carrying a segment, a speaker and an analytics row."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": f"media_guard_872_{uuid.uuid4().hex[:8]}.wav",
        "title": "media_guard_872",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "retry_count": 0,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            start_time=0.0,
            end_time=1.5,
            text="the only copy of this sentence",
        )
    )
    db_session.add(
        Speaker(
            uuid=str(uuid.uuid4()),
            user_id=owner.id,
            media_file_id=media_file.id,
            name="SPEAKER_00",
        )
    )
    db_session.add(Analytics(media_file_id=media_file.id, overall_analytics={"words": 6}))
    db_session.commit()
    return media_file


def _counts(db_session, media_file_id: int) -> tuple[int, int, int]:
    return (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media_file_id)
        .count(),
        db_session.query(Speaker).filter(Speaker.media_file_id == media_file_id).count(),
        db_session.query(Analytics).filter(Analytics.media_file_id == media_file_id).count(),
    )


@pytest.fixture
def storage_probe(monkeypatch):
    """Drive ``minio_service.object_exists_and_size`` deterministically.

    ``SKIP_S3`` is forced off so the guard performs its real lookup in both
    environments; the returned callable installs the verdict for the run.
    """
    monkeypatch.setenv("SKIP_S3", "False")

    def install(result):
        def fake_object_exists_and_size(object_name: str):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(
            "app.services.minio_service.object_exists_and_size",
            fake_object_exists_and_size,
        )

    return install


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_reprocess_refuses_when_the_media_object_cannot_be_resolved(
    client, user_token_headers, normal_user, db_session, storage_probe
):
    """A set-but-unresolvable ``storage_path`` must 4xx with the transcript intact."""
    storage_probe(None)  # a confirmed "no such key"
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=user_token_headers)

    # The data assertion comes FIRST on purpose: on the unfixed code the rows are
    # already deleted and committed by the time the response is written, so the
    # status code alone would report the wrong thing about the wrong moment.
    db_session.expire_all()
    assert _counts(db_session, file_id) == (1, 1, 1)
    assert response.status_code == status.HTTP_409_CONFLICT
    assert "storage" in response.json()["detail"].lower()


def test_selective_transcription_reprocess_refuses_when_the_media_object_is_gone(
    client, user_token_headers, normal_user, db_session, storage_probe
):
    """The selective path reaches the same deletion when ``transcription`` is staged."""
    storage_probe(None)
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["transcription"]},
    )

    db_session.expire_all()
    assert _counts(db_session, file_id) == (1, 1, 1)
    assert response.status_code == status.HTTP_409_CONFLICT


def test_a_transcript_only_stage_is_not_refused_when_the_media_is_gone(
    client, user_token_headers, normal_user, db_session, storage_probe
):
    """Control on the scope of the guard: summarization needs no media.

    Only ``transcription`` and ``rediarize`` re-run work that requires the audio
    and delete what they regenerate. Summarization, analytics, topics, search
    indexing and LLM speaker ID are all derived from the transcript, so refusing
    them would remove capability without protecting anything.
    """
    storage_probe(None)
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["summarization"]},
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    # The transcript is untouched by a summarization-only rerun.
    assert _counts(db_session, file_id)[0] == 1


def test_bulk_reprocess_refuses_the_media_less_file(
    client, user_token_headers, normal_user, db_session, storage_probe
):
    """The gallery's bulk action is the "one click destroys many" path."""
    storage_probe(None)
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "reprocess"},
    )

    db_session.expire_all()
    assert _counts(db_session, file_id) == (1, 1, 1)
    assert response.status_code == status.HTTP_200_OK
    result = response.json()[0]
    assert result["success"] is False
    assert result["error"] == "MEDIA_UNAVAILABLE"


def test_reprocess_still_succeeds_when_the_media_object_is_present(
    client, user_token_headers, normal_user, db_session, storage_probe
):
    """Control: a resolvable object still reprocesses and still clears the transcript.

    Without this the guard could pass by refusing every reprocess.
    """
    storage_probe(4096)  # the object is there, 4096 bytes
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert _counts(db_session, file_id) == (0, 0, 0)


# ---------------------------------------------------------------------------
# The deletion site itself — the guard has to hold for every caller, not just
# the endpoint (bulk retry/reprocess and the stuck-file recovery path also
# call it).
# ---------------------------------------------------------------------------


def test_reset_file_for_retry_refuses_when_the_media_object_is_gone(
    db_session, normal_user, storage_probe
):
    storage_probe(None)
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    assert reset_file_for_retry(db_session, int(file_id), reset_retry_count=False) is False

    db_session.expire_all()
    assert _counts(db_session, file_id) == (1, 1, 1)


def test_reset_file_for_retry_fails_closed_when_storage_cannot_be_reached(
    db_session, normal_user, storage_probe
):
    """A storage outage must not read as "the object is gone" and delete the transcript."""
    storage_probe(OSError("minio unreachable"))
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    assert reset_file_for_retry(db_session, int(file_id), reset_retry_count=False) is False

    db_session.expire_all()
    assert _counts(db_session, file_id) == (1, 1, 1)


def test_reset_file_for_retry_still_proceeds_when_the_media_object_is_present(
    db_session, normal_user, storage_probe
):
    """Control: the guard must not stand between a real file and its retry."""
    storage_probe(4096)
    media_file = _make_file_with_transcript(db_session, normal_user)
    file_id = media_file.id

    assert reset_file_for_retry(db_session, int(file_id), reset_retry_count=False) is True

    db_session.expire_all()
    assert _counts(db_session, file_id) == (0, 0, 0)


def test_retry_of_a_file_with_no_transcript_is_unaffected(db_session, normal_user, storage_probe):
    """A row with nothing to lose must still reset — this is the URL/upload retry path.

    ``upload.py``, ``url_processing.py``, ``media_download_service`` and the watch
    sources all create rows with ``storage_path=""`` and no segments; retrying one
    re-runs the download. Refusing here would break that, and there is no transcript
    at risk.
    """
    storage_probe(None)
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename=f"pending_872_{uuid.uuid4().hex[:8]}.wav",
        title="pending_872",
        storage_path="",
        content_type="audio/wav",
        file_size=0,
        status="error",
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    assert reset_file_for_retry(db_session, int(media_file.id), reset_retry_count=False) is True
