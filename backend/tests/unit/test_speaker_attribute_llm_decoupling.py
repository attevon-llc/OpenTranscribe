"""Automatic LLM speaker-name suggestions do not depend on attribute detection (#1148).

They used to be dispatched only from the end of ``detect_speaker_attributes_task``, so
turning attribute detection off in Settings -> Speaker Attributes silently turned off AI name
suggestions too. The dispatchers now queue the LLM step directly when detection is off.
"""

from __future__ import annotations

import uuid as uuid_mod
from contextlib import contextmanager

import pytest

from app.models.prompt import UserSetting
from app.tasks import speaker_attribute_task as sat


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


@pytest.fixture
def queued(db_session, monkeypatch):
    """Record what the dispatcher puts on the queue, in order, as plain data."""
    monkeypatch.setattr(sat, "session_scope", lambda: _real_scope(db_session))
    sent: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        sat.detect_speaker_attributes_task,
        "delay",
        lambda *a, **k: sent.append(("detect_attributes", a, k)),
    )
    monkeypatch.setattr(
        "app.tasks.speaker_tasks.identify_speakers_llm_task.delay",
        lambda *a, **k: sent.append(("llm_speaker_id", a, k)),
    )
    return sent


def test_postprocess_queues_llm_name_suggestions_when_attribute_detection_is_off(
    db_session, normal_user, queued
):
    from app.tasks.transcription.postprocess import _dispatch_speaker_attributes

    _pref(db_session, normal_user, "speaker_attribute_detection_enabled", "false")
    file_uuid = str(uuid_mod.uuid4())

    _dispatch_speaker_attributes(file_uuid, normal_user.id, None)

    assert queued == [("llm_speaker_id", (), {"file_uuid": file_uuid})]


def test_postprocess_leaves_the_llm_hand_off_to_the_attribute_task_when_detection_is_on(
    db_session, normal_user, queued
):
    """The attribute task chains the LLM step itself; queuing it here too would double it."""
    from app.tasks.transcription.postprocess import _dispatch_speaker_attributes

    file_uuid = str(uuid_mod.uuid4())

    _dispatch_speaker_attributes(file_uuid, normal_user.id, None)

    assert queued == [("detect_attributes", (file_uuid, normal_user.id), {})]
