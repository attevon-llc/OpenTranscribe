"""Task-row visibility for detect_speaker_attributes_task (issue #622).

Before this, the task created ZERO ``Task`` rows: unlike every sibling
post-processing task (speaker_identification, summarization, topic_extraction,
speaker_clustering, analytics, search_indexing) a run had no status, no
duration, and nothing in the Tasks UI/API. A gender-detection run was
discovered live, 8+ minutes in, completely invisible — indistinguishable from
"never dispatched" until it hit its own hard ``time_limit=660``.

These tests assert on the real ``Task`` row
``detect_speaker_attributes_task.apply(...)`` leaves behind, not just on the
task's return value — mirroring the repo's audit-tests standard (issue #431):
a mocked ``assert_called_once_with`` proves the code *called* a function, not
that it left the state a human debugging the Tasks UI would actually see.

Each test calls the outer, bound Celery task via ``.apply(...)`` (not the
inner ``_detect_speaker_attributes`` helper `test_speaker_attribute_session_
lifetime.py` exercises) specifically because ``.apply()`` is what gives
``self.request.id`` a real value — the exact thing the task-tracking code
needs and the direct-call form does not provide.
"""

from __future__ import annotations

import datetime
import time
import uuid as uuid_mod
from contextlib import contextmanager

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.tasks import speaker_attribute_task as sat


@contextmanager
def _real_scope(db_session):
    """A `session_scope` stand-in bound to the test's savepoint session.

    Commits like the real thing so a later query in the same test sees the
    write, but never closes `db_session` — the fixture owns that.
    """
    try:
        yield db_session
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def _make_file_with_speech(db_session, user, *, speakers: int = 1, seg_seconds: float = 4.0):
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="attrs.mp4",
        storage_path="test/attrs.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()

    created = []
    start = 0.0
    for i in range(speakers):
        speaker = Speaker(
            uuid=str(uuid_mod.uuid4()),
            media_file_id=media_file.id,
            user_id=user.id,
            name=f"SPEAKER_0{i}",
        )
        db_session.add(speaker)
        db_session.flush()
        created.append(speaker)

        for _ in range(2):
            db_session.add(
                TranscriptSegment(
                    uuid=str(uuid_mod.uuid4()),
                    media_file_id=media_file.id,
                    speaker_id=speaker.id,
                    start_time=start,
                    end_time=start + seg_seconds,
                    text="hello there",
                )
            )
            start += seg_seconds
    db_session.flush()
    db_session.commit()
    return media_file, created


class _FakeMinio:
    def presigned_get_object(self, **kwargs):
        return "http://minio.invalid/attrs.mp4"


class _FakeAttributeService:
    """Stands in for the real wav2vec2 service — no model, no ffmpeg."""

    def load_models(self):
        pass


def _fake_inference_success(audio_source, work_items, service):
    if not work_items:
        return {}, {}
    speaker_id = work_items[0][0]
    return ({speaker_id: {"male": 0.9, "female": 0.1}}, {speaker_id: 1})


@pytest.fixture
def tracking_env(db_session, monkeypatch):
    """Real DB session wired in; everything slow/external is faked."""
    monkeypatch.setattr(sat, "session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr(sat, "_is_speaker_attribute_detection_enabled", lambda user_id: True)
    monkeypatch.setattr(sat, "_dispatch_llm_speaker_identification", lambda file_uuid: None)
    monkeypatch.setattr(sat, "send_ws_event", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.minio_service.minio_client", _FakeMinio())
    monkeypatch.setattr(
        "app.services.speaker_attribute_service.get_cached_attribute_service",
        lambda: _FakeAttributeService(),
    )
    monkeypatch.setattr(sat, "_run_gender_inference_parallel", _fake_inference_success)
    return None


def _task_row_for(db_session, media_file_id: int) -> Task | None:
    db_session.expire_all()
    return (  # type: ignore[no-any-return]
        db_session.query(Task).filter(Task.media_file_id == media_file_id).first()
    )


def test_normal_completion_creates_and_completes_task_row(db_session, normal_user, tracking_env):
    """(a) The happy path must leave a real, terminal, matched Task row."""
    media_file, _ = _make_file_with_speech(db_session, normal_user)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result["status"] == "success", result

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "no Task row was created — issue #622 regression"
    assert task.task_type == "speaker_attribute_detection"
    assert task.status == "completed"
    assert task.progress == 1.0
    assert task.completed_at is not None
    assert task.user_id == normal_user.id

    refreshed_file = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed_file.active_task_id is None, (
        "active_task_id must clear on a terminal status, or is_file_safe_to_delete "
        "and similar liveness checks will keep reporting this file as busy"
    )


def test_already_predicted_skip_path_creates_skipped_task_row(
    db_session, normal_user, tracking_env, monkeypatch
):
    """(b) The idempotency guard must still leave an honest, visible Task row."""
    media_file, speakers = _make_file_with_speech(db_session, normal_user)
    for speaker in speakers:
        speaker.attributes_predicted_at = datetime.datetime.now(datetime.UTC)
        speaker.predicted_gender = "male"
    db_session.commit()

    called = {"inference": False}

    def fake_inference(*args, **kwargs):
        called["inference"] = True
        return {}, {}

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", fake_inference)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result == {"status": "skipped", "reason": "already_predicted"}
    assert called["inference"] is False, "idempotency guard did not actually skip the slow work"

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "the skip path left no Task row — invisible in the Tasks UI"
    assert task.status == "skipped"
    assert task.completed_at is not None


def test_duplicate_in_progress_skip_path_creates_skipped_task_row(
    db_session, normal_user, tracking_env, monkeypatch
):
    """(c) The Redis dedup guard must also leave an honest, visible Task row."""
    media_file, _ = _make_file_with_speech(db_session, normal_user)

    class _FakeRedis:
        def set(self, *args, **kwargs):
            return False  # simulate: another dispatch already holds the lock

        def delete(self, *args, **kwargs):
            pass

    monkeypatch.setattr("app.core.redis.get_redis", lambda: _FakeRedis())

    called = {"inference": False}

    def fake_inference(*args, **kwargs):
        called["inference"] = True
        return {}, {}

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", fake_inference)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result == {"status": "skipped", "reason": "duplicate_in_progress"}
    assert called["inference"] is False

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "the dedup skip path left no Task row"
    assert task.status == "skipped"
    assert task.completed_at is not None


def test_failure_path_creates_failed_task_row(db_session, normal_user, tracking_env, monkeypatch):
    """(d) A real failure must be recorded as failed, with the error captured."""
    media_file, _ = _make_file_with_speech(db_session, normal_user)

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg segment fetch failed")

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", boom)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result["status"] == "error"

    task = _task_row_for(db_session, media_file.id)
    assert task is not None, "a failed run left no Task row"
    assert task.status == "failed"
    assert task.error_message and "ffmpeg segment fetch failed" in task.error_message
    assert task.completed_at is not None


def test_stalled_model_load_fails_fast_instead_of_hanging(
    db_session, normal_user, tracking_env, monkeypatch
):
    """A wedged HuggingFace Hub call must surface as a bounded, clear error.

    ``Wav2Vec2*.from_pretrained`` has no wall-clock bound of its own (see
    ``_load_models_with_timeout``'s docstring) — this is the defensive fix for
    the live stall observed in issue #622: 0% CPU for 8+ minutes before the
    task's hard `time_limit` killed it with no useful signal. The fake service
    below stands in for that stall with a `time.sleep`, and the test proves
    the task gives up in roughly the (patched, short) timeout window rather
    than anywhere near the sleep duration or the real 60s default.
    """
    media_file, _ = _make_file_with_speech(db_session, normal_user)
    monkeypatch.setattr(sat, "_MODEL_LOAD_TIMEOUT_SECONDS", 1)

    class _HangingService:
        def load_models(self):
            time.sleep(5)  # stands in for a stalled network call

    monkeypatch.setattr(
        "app.services.speaker_attribute_service.get_cached_attribute_service",
        lambda: _HangingService(),
    )

    started = time.monotonic()
    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()
    elapsed = time.monotonic() - started

    assert result["status"] == "error"
    assert "did not complete within" in result["message"]
    assert elapsed < 4, f"the timeout did not bound the wait: took {elapsed:.1f}s"

    task = _task_row_for(db_session, media_file.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error_message and "did not complete within" in task.error_message
