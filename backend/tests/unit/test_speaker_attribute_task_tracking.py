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

from app.models.media import MediaFile
from app.tasks import speaker_attribute_task as sat
from tests.fixtures.redis_fakes import RedisDownExceptFor
from tests.fixtures.speaker_attribute_tracking import make_file_with_speech
from tests.fixtures.speaker_attribute_tracking import task_row_for


def test_normal_completion_creates_and_completes_task_row(db_session, normal_user, tracking_env):
    """(a) The happy path must leave a real, terminal, matched Task row."""
    media_file, _ = make_file_with_speech(db_session, normal_user)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result["status"] == "success", result

    task = task_row_for(db_session, media_file.id)
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
    media_file, speakers = make_file_with_speech(db_session, normal_user)
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

    task = task_row_for(db_session, media_file.id)
    assert task is not None, "the skip path left no Task row — invisible in the Tasks UI"
    assert task.status == "skipped"
    assert task.completed_at is not None


def test_duplicate_in_progress_skip_path_creates_skipped_task_row(
    db_session, normal_user, tracking_env, monkeypatch
):
    """(c) The Redis dedup guard must also leave an honest, visible Task row."""
    media_file, _ = make_file_with_speech(db_session, normal_user)

    class _FakeRedis(RedisDownExceptFor):
        def set(self, *args, **kwargs):
            return False  # simulate: another dispatch already holds the lock

        def get(self, key):
            return b"another-live-task-id"

        def exists(self, key):
            return 1  # ...and that holder is still heartbeating (issue #1067)

        def delete(self, *args, **kwargs):
            pass

    fake = _FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    # task_replay.is_alive() (the take-over guard) reads the heartbeat through this client.
    monkeypatch.setattr("app.core.redis.get_request_redis", lambda: fake)

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

    task = task_row_for(db_session, media_file.id)
    assert task is not None, "the dedup skip path left no Task row"
    assert task.status == "skipped"
    assert task.completed_at is not None


def test_failure_path_creates_failed_task_row(db_session, normal_user, tracking_env, monkeypatch):
    """(d) A real failure must be recorded as failed, with the error captured."""
    media_file, _ = make_file_with_speech(db_session, normal_user)

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg segment fetch failed")

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", boom)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result["status"] == "error"

    task = task_row_for(db_session, media_file.id)
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
    media_file, _ = make_file_with_speech(db_session, normal_user)
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

    task = task_row_for(db_session, media_file.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error_message and "did not complete within" in task.error_message
