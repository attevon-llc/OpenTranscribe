"""Speaker-attribute toggles must change what is stored and what is dispatched (#1200, #1148).

Each test runs the real task / dispatcher / writer against a real DB session and asserts the
observable output (``Speaker.predicted_gender``, which Celery tasks were queued), not merely
that a setting row was read.
"""

from __future__ import annotations

import uuid as uuid_mod
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.prompt import UserSetting
from app.services import system_settings_service
from app.services.speaker_attribute_settings import resolve_speaker_attribute_flags
from app.tasks import speaker_attribute_task as sat
from app.tasks.transcription.speaker_processor import apply_sidecar_gender_for_user


@contextmanager
def _real_scope(db_session):
    try:
        yield db_session
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def _pref(db, user, key: str, value: str) -> None:
    db.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db.commit()


def _file_with_speaker(db, user):
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="a.mp4",
        storage_path="test/a.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db.add(media_file)
    db.flush()
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()), media_file_id=media_file.id, user_id=user.id, name="SPEAKER_00"
    )
    db.add(speaker)
    db.flush()
    for i in range(2):
        db.add(
            TranscriptSegment(
                uuid=str(uuid_mod.uuid4()),
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=i * 4.0,
                end_time=i * 4.0 + 4.0,
                text="hello there",
            )
        )
    db.commit()
    return media_file, speaker


SIDECAR = {"SPEAKER_00": {"label": "female", "confidence": 0.9}}


# ---------------------------------------------------------------- resolver


def test_flags_resolve_user_then_system_then_default(db_session, normal_user):
    assert resolve_speaker_attribute_flags(db_session, normal_user.id).gender_detection_enabled

    system_settings_service.set_setting(
        db_session, "speaker_attribute.gender_detection_enabled", "false"
    )
    db_session.commit()
    assert not resolve_speaker_attribute_flags(db_session, normal_user.id).gender_detection_enabled

    _pref(db_session, normal_user, "speaker_attribute_gender_detection_enabled", "true")
    assert resolve_speaker_attribute_flags(db_session, normal_user.id).gender_detection_enabled


def test_gender_prediction_needs_both_detection_and_gender_on(db_session, normal_user):
    _pref(db_session, normal_user, "speaker_attribute_detection_enabled", "false")
    flags = resolve_speaker_attribute_flags(db_session, normal_user.id)
    assert flags.gender_detection_enabled and not flags.gender_prediction_allowed


# ---------------------------------------------------------------- sidecar write


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (None, None, "female"),  # control: nothing opted out, the sidecar verdict is stored
        ("speaker_attribute_detection_enabled", "false", None),
        ("speaker_attribute_gender_detection_enabled", "false", None),
    ],
)
def test_sidecar_gender_is_stored_only_when_the_user_allows_it(
    db_session, normal_user, key, value, expected
):
    _, speaker = _file_with_speaker(db_session, normal_user)
    if key:
        _pref(db_session, normal_user, key, value)

    apply_sidecar_gender_for_user(db_session, speaker.media_file_id, normal_user.id, SIDECAR)
    db_session.commit()

    db_session.refresh(speaker)
    assert speaker.predicted_gender == expected
    if expected is None:
        assert speaker.attributes_predicted_at is None  # a later opt-in can still run detection


# ---------------------------------------------------------------- attribute task


@pytest.fixture
def task_env(db_session, monkeypatch):
    monkeypatch.setattr(sat, "session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr(sat, "_dispatch_llm_speaker_identification", lambda file_uuid: None)
    monkeypatch.setattr(sat, "send_ws_event_for_file", lambda *a, **kw: None)
    monkeypatch.setattr(
        "app.services.takedown_service.is_notification_suppressed_for_uuid",
        lambda *a, **kw: False,
    )
    minio = MagicMock()
    minio.presigned_get_object.return_value = "http://minio.invalid/a.mp4"
    monkeypatch.setattr("app.services.minio_service.minio_client", minio)
    monkeypatch.setattr(
        "app.services.speaker_attribute_service.get_cached_attribute_service",
        lambda: MagicMock(),
    )
    calls = {"inference": 0}

    def fake_inference(audio_source, work_items, service):
        calls["inference"] += 1
        return {work_items[0][0]: {"male": 0.9, "female": 0.1}}, {work_items[0][0]: 1}

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", fake_inference)
    return calls


def test_attribute_task_with_gender_off_predicts_nothing(db_session, normal_user, task_env):
    media_file, speaker = _file_with_speaker(db_session, normal_user)
    _pref(db_session, normal_user, "speaker_attribute_gender_detection_enabled", "false")

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result == {"status": "skipped", "reason": "gender_disabled"}
    assert task_env["inference"] == 0
    db_session.refresh(speaker)
    assert speaker.predicted_gender is None


def test_attribute_task_with_gender_on_still_predicts(db_session, normal_user, task_env):
    """Control for the test above: same fixture, toggle left on, gender is written."""
    media_file, speaker = _file_with_speaker(db_session, normal_user)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert result["status"] == "success", result
    db_session.refresh(speaker)
    assert speaker.predicted_gender == "male"
