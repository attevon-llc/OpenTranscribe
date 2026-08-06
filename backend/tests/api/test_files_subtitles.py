"""Characterization tests for ``files/subtitles.py``.

Covers subtitle export (SRT/VTT/TXT), validation, supported-formats, the async
bulk-export prepare endpoint, and the bulk-export SSE stream wired under
``/api/files``.

Rows are created on the savepoint-isolated ``db_session`` (rolled back at
teardown) with fabricated transcript segments where a real transcript is needed,
so dev data is never touched. Celery dispatch is no-op'd by the autouse conftest
fixture, so the bulk-export prepare path asserts the queued-job envelope, not
worker side effects. The SSE test drives the generator against a stub async Redis
client — no broker, no worker, and nothing written to the dev stack's Redis.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import status

from app.api.endpoints.files import subtitles
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.user import User


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "subs_test.wav",
        "title": "subs_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _add_segments(db_session, media_file) -> None:
    """Attach two fabricated transcript segments so subtitle output is non-empty."""
    for start, end, text in [(0.0, 1.5, "Hello world."), (1.5, 3.0, "This is a test.")]:
        db_session.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                start_time=start,
                end_time=end,
                text=text,
            )
        )
    db_session.commit()


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/subtitles
# ---------------------------------------------------------------------------


def test_subtitles_srt_default(client, user_token_headers, normal_user, db_session):
    """The default format is SRT (application/x-subrip) and contains arrow timing."""
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file)
    response = client.get(f"/api/files/{media_file.uuid}/subtitles", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("application/x-subrip")
    assert "-->" in response.text
    assert "Hello world." in response.text
    assert 'attachment; filename="subs_test.srt"' in response.headers["content-disposition"]


def test_subtitles_webvtt_format(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": "webvtt"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("text/vtt")
    assert response.text.startswith("WEBVTT")


def test_subtitles_txt_format(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": "txt"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("text/plain")


def test_subtitles_unknown_format_falls_back_to_srt(
    client, user_token_headers, normal_user, db_session
):
    """An unrecognized format string is NOT a 422 — it falls through to SRT."""
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": "bogusfmt"},
    )
    assert response.status_code == status.HTTP_200_OK
    # Unknown format → content_type map default is text/plain.
    assert response.headers["content-type"].startswith("text/plain")


def test_subtitles_not_completed_400(client, user_token_headers, normal_user, db_session):
    """Subtitles for a non-completed file are a 400."""
    media_file = _make_file(db_session, normal_user, file_status="processing")
    response = client.get(f"/api/files/{media_file.uuid}/subtitles", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Transcription not completed yet"


def test_subtitles_no_segments_400(client, user_token_headers, normal_user, db_session):
    """A completed file with no transcript segments → 400.

    Contract pin: the SubtitleService raises ValueError("No transcript segments
    found for this media file"), which the endpoint maps to 400 (NOT the 404
    "No transcript available" empty-content branch, which is unreachable because
    the service errors first).
    """
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/subtitles", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "No transcript segments found for this media file"


def test_subtitles_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/subtitles")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_subtitles_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_subtitles_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/subtitles", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_subtitles_malformed_uuid_400(client, user_token_headers):
    """``file_uuid: str`` → bad UUID rejected by get_by_uuid with 400."""
    response = client.get("/api/files/not-a-uuid/subtitles", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/subtitles/validate
# ---------------------------------------------------------------------------


def test_subtitles_validate_owner(client, user_token_headers, normal_user, db_session):
    """Validation returns the SubtitleValidationResult envelope for the owner."""
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles/validate", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"is_valid", "issues", "total_segments", "total_duration"}
    assert body["total_segments"] == 2
    assert body["total_duration"] == 3.0


def test_subtitles_validate_not_completed_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, file_status="pending")
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles/validate", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Transcription not completed yet"


def test_subtitles_validate_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles/validate", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


# ---------------------------------------------------------------------------
# GET /api/files/supported-formats
# ---------------------------------------------------------------------------


def test_supported_formats(client, user_token_headers):
    response = client.get("/api/files/supported-formats", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"subtitle_formats": ["srt", "webvtt", "txt"]}


# ---------------------------------------------------------------------------
# POST /api/files/bulk-export/prepare
# ---------------------------------------------------------------------------


def test_bulk_export_empty_list_400(client, user_token_headers):
    response = client.post(
        "/api/files/bulk-export/prepare",
        headers=user_token_headers,
        json={"file_uuids": [], "subtitle_format": "srt"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "No file UUIDs provided"


def test_bulk_export_too_many_400(client, user_token_headers):
    response = client.post(
        "/api/files/bulk-export/prepare",
        headers=user_token_headers,
        json={"file_uuids": [str(uuid.uuid4()) for _ in range(101)], "subtitle_format": "srt"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Maximum 100 files per export"


def test_bulk_export_bad_format_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        "/api/files/bulk-export/prepare",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "subtitle_format": "pdf"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Unsupported format: pdf"


def test_bulk_export_no_accessible_files_404(client, user_token_headers):
    """All-unknown UUIDs are silently skipped → 404 'No accessible completed files'."""
    response = client.post(
        "/api/files/bulk-export/prepare",
        headers=user_token_headers,
        json={"file_uuids": [str(uuid.uuid4())], "subtitle_format": "srt"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "No accessible completed files to export."


def test_bulk_export_queues_job(client, user_token_headers, normal_user, db_session):
    """A completed owned file produces a processing/job_id envelope (Celery no-op'd)."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        "/api/files/bulk-export/prepare",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "subtitle_format": "srt"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "processing"
    assert isinstance(body["job_id"], str) and body["job_id"]


def test_bulk_export_unauthorized(client):
    response = client.post(
        "/api/files/bulk-export/prepare",
        json={"file_uuids": [str(uuid.uuid4())], "subtitle_format": "srt"},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /api/files/bulk-export-stream  (SSE)
# ---------------------------------------------------------------------------


class _RaceyPubSub:
    """Pub/sub stub that lets the worker finish exactly during ``subscribe``.

    ``get_message`` never returns a message: the worker's ``completed`` publish
    landed while nobody was subscribed, so it is gone for good. That is the lost
    wakeup of issue #334 — the only remaining signal is the result cache.
    """

    def __init__(self, on_subscribe):
        self._on_subscribe = on_subscribe
        self.channel: str | None = None

    async def subscribe(self, channel):
        self.channel = channel
        self._on_subscribe()

    async def get_message(self, **_kwargs):
        return None

    async def unsubscribe(self, _channel):
        return None

    async def close(self):
        return None


class _RaceyRedis:
    """Async Redis stub whose result key appears only once ``subscribe`` was called."""

    def __init__(self, result):
        self._result = result
        self._finished = False
        self.gets: list[str] = []
        self._pubsub = _RaceyPubSub(self._worker_finishes)

    def _worker_finishes(self):
        self._finished = True

    async def get(self, key):
        self.gets.append(key)
        return json.dumps(self._result) if self._finished else None

    def pubsub(self):
        return self._pubsub

    async def close(self):
        return None


async def _first_frame(response):
    """Return the first SSE frame emitted by a StreamingResponse."""
    async for frame in response.body_iterator:
        return frame
    raise AssertionError("the stream closed without emitting a frame")


def test_bulk_export_stream_recovers_a_completion_during_subscribe(monkeypatch):
    """A job finishing in the check→subscribe gap must still deliver ``ready`` (#334).

    The stub completes the job at the moment ``pubsub.subscribe`` is called and
    publishes to nobody, exactly like a fast worker. With only the pre-subscribe
    cache read the stream emits ``progress`` and then waits on ``get_message``
    forever; the post-subscribe re-check is what turns it back into ``ready``.
    """
    result = {
        "url": "https://storage.test/bulk/job-334.zip",
        "filename": "transcripts_srt.zip",
        "exported": 2,
        "skipped": 0,
    }
    fake_redis = _RaceyRedis(result)
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: fake_redis)

    response = subtitles.bulk_export_stream(job="job-334", current_user=User())
    frame = asyncio.run(_first_frame(response))

    assert frame.startswith("event: ready"), (
        f"expected the cached result, got {frame!r} — the completion published during "
        "the subscribe window was lost, so the stream would hang until the client gave up"
    )
    assert json.loads(frame.split("data: ", 1)[1]) == result
    assert fake_redis.gets == ["bulk_export_result:job-334"] * 2, (
        "the result cache must be read before AND after subscribing"
    )
    assert fake_redis.pubsub().channel == "download_events:bulk:job-334"
