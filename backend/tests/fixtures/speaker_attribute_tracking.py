"""Shared setup for ``detect_speaker_attributes_task`` tests: a file with speech, a faked env.

``tracking_env`` is registered as a pytest plugin in ``tests/conftest.py`` so any test module
can request it by name; the helpers are imported explicitly.
"""

from __future__ import annotations

import uuid as uuid_mod
from contextlib import contextmanager

import pytest

from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.tasks import speaker_attribute_task as sat


@contextmanager
def real_scope(db_session):
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


def make_file_with_speech(db_session, user, *, speakers: int = 1, seg_seconds: float = 4.0):
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


def _fake_inference_success(audio_source, work_items, service, audio_stream_index=None):
    if not work_items:
        return {}, {}
    speaker_id = work_items[0][0]
    return ({speaker_id: {"male": 0.9, "female": 0.1}}, {speaker_id: 1})


@pytest.fixture
def tracking_env(db_session, monkeypatch):
    """Real DB session wired in; everything slow/external is faked."""
    monkeypatch.setattr(sat, "session_scope", lambda: real_scope(db_session))
    monkeypatch.setattr(sat, "_is_speaker_attribute_detection_enabled", lambda user_id: True)
    monkeypatch.setattr(sat, "_dispatch_llm_speaker_identification", lambda file_uuid: None)
    monkeypatch.setattr(sat, "send_ws_event_for_file", lambda *a, **kw: None)
    # send_ws_event_for_file (issue #908) resolves quarantine via its OWN
    # session_scope() on a real connection, which cannot see this test's
    # savepoint-isolated MediaFile even after real_scope's commit (a nested
    # savepoint release is not visible across connections) — stub the check
    # itself rather than bridge a second session.
    monkeypatch.setattr(
        "app.services.file_availability.is_notification_suppressed_for_uuid",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr("app.services.minio_service.minio_client", _FakeMinio())
    monkeypatch.setattr(
        "app.services.speaker_attribute_service.get_cached_attribute_service",
        lambda: _FakeAttributeService(),
    )
    monkeypatch.setattr(sat, "_run_gender_inference_parallel", _fake_inference_success)
    return None


def task_row_for(db_session, media_file_id: int) -> Task | None:
    db_session.expire_all()
    row: Task | None = db_session.query(Task).filter(Task.media_file_id == media_file_id).first()
    return row
