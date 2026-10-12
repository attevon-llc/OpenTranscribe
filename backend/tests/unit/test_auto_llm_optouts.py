"""Per-user opt-outs for the two automatic AI steps after a transcription (#1148).

``auto_llm_speaker_identification`` (AI speaker-name suggestions) and
``auto_topic_extraction`` (tag / collection suggestions) both send transcript text to the
configured AI provider and both used to run for every user with a provider. Default is ON,
resolved user > system default > True. Only the AUTOMATIC dispatch honours them: manual
buttons and explicit reprocess stage lists never do.
"""

from __future__ import annotations

import uuid as uuid_mod
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.prompt import UserSetting
from app.models.system_settings import SystemSettings
from app.services.auto_llm_settings import resolve_auto_llm_flags
from app.services.auto_llm_settings import resolve_auto_llm_flags_for_users
from app.services.llm_service import LLMService
from app.services.task_detection_service import TaskDetectionService
from app.tasks import speaker_attribute_task as sat
from app.tasks.transcription import downstream

SPEAKER_KEY = "auto_llm_speaker_identification"
TOPIC_KEY = "auto_topic_extraction"


@contextmanager
def _real_scope(db_session):
    try:
        yield db_session
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def _user_pref(db, user, key: str, value: str) -> None:
    db.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db.commit()


def _system_pref(db, key: str, value: str) -> None:
    db.add(SystemSettings(key=key, value=value))
    db.commit()


def _media_file(db, user, **kwargs) -> MediaFile:
    fid = str(uuid_mod.uuid4())
    mf = MediaFile(
        uuid=fid,
        user_id=user.id,
        filename="optout.wav",
        storage_path=f"media/test/{fid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=kwargs.pop("status", "completed"),
        **kwargs,
    )
    db.add(mf)
    db.commit()
    db.refresh(mf)
    return mf


@pytest.fixture
def queued(db_session, monkeypatch):
    """Everything the dispatchers put on the queue, as plain data."""
    monkeypatch.setattr(sat, "session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr(downstream, "session_scope", lambda: _real_scope(db_session))
    sent: list[tuple[str, tuple, dict]] = []

    def rec(name):
        def record(*a, **k):
            sent.append((name, a, k))
            return SimpleNamespace(id=f"fake-{name}")

        return record

    monkeypatch.setattr(sat.detect_speaker_attributes_task, "delay", rec("detect_attributes"))
    monkeypatch.setattr(
        "app.tasks.speaker_tasks.identify_speakers_llm_task.delay", rec("llm_speaker_id")
    )
    monkeypatch.setattr("app.tasks.topic_extraction.extract_topics_task.delay", rec("topics"))
    monkeypatch.setattr("app.tasks.analytics.analyze_transcript_task.delay", rec("analytics"))
    monkeypatch.setattr(downstream, "_dispatch_automatic_summary", lambda *a, **k: None)
    monkeypatch.setattr(downstream, "_get_collection_prompt_uuid", lambda *a, **k: None)
    return sent


def _names(sent):
    return [name for name, _, _ in sent]


# --- resolver --------------------------------------------------------------------------


def test_defaults_are_on(db_session, normal_user):
    flags = resolve_auto_llm_flags(db_session, normal_user.id)
    assert flags.speaker_identification is True
    assert flags.topic_extraction is True


def test_user_off_beats_system_on(db_session, normal_user):
    _system_pref(db_session, "auto_llm.topic_extraction", "true")
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    flags = resolve_auto_llm_flags(db_session, normal_user.id)
    assert flags.topic_extraction is False
    assert flags.speaker_identification is True


def test_user_on_beats_system_off(db_session, normal_user):
    _system_pref(db_session, "auto_llm.speaker_identification", "false")
    _user_pref(db_session, normal_user, SPEAKER_KEY, "true")
    assert resolve_auto_llm_flags(db_session, normal_user.id).speaker_identification is True


def test_user_without_a_choice_inherits_the_system_default(db_session, normal_user):
    _system_pref(db_session, "auto_llm.speaker_identification", "false")
    _system_pref(db_session, "auto_llm.topic_extraction", "false")
    flags = resolve_auto_llm_flags(db_session, normal_user.id)
    assert (flags.speaker_identification, flags.topic_extraction) == (False, False)


def test_batch_resolver_keeps_users_apart(db_session, normal_user, other_user):
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    flags = resolve_auto_llm_flags_for_users(db_session, [normal_user.id, other_user.id])
    assert flags[normal_user.id].topic_extraction is False
    assert flags[other_user.id].topic_extraction is True


# --- LLM speaker identification ----------------------------------------------------------


def test_postprocess_queues_llm_names_with_the_switch_on_and_detection_off(
    db_session, normal_user, queued
):
    from app.tasks.transcription.postprocess import _dispatch_speaker_attributes

    _user_pref(db_session, normal_user, "speaker_attribute_detection_enabled", "false")
    fid = str(uuid_mod.uuid4())
    _dispatch_speaker_attributes(fid, normal_user.id, None)
    assert queued == [("llm_speaker_id", (), {"file_uuid": fid})]


def test_postprocess_queues_nothing_llm_with_the_switch_off_and_detection_off(
    db_session, normal_user, queued
):
    from app.tasks.transcription.postprocess import _dispatch_speaker_attributes

    _user_pref(db_session, normal_user, "speaker_attribute_detection_enabled", "false")
    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    _dispatch_speaker_attributes(str(uuid_mod.uuid4()), normal_user.id, None)
    assert queued == []


def test_switch_off_keeps_attribute_detection_running(db_session, normal_user, queued):
    """Gender/age detection is a separate feature; only the LLM hand-off is opted out."""
    from app.tasks.transcription.postprocess import _dispatch_speaker_attributes

    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    fid = str(uuid_mod.uuid4())
    _dispatch_speaker_attributes(fid, normal_user.id, None)
    assert queued == [("detect_attributes", (fid, normal_user.id), {})]


def test_the_attribute_task_hand_off_honours_the_switch(db_session, normal_user, queued):
    """``_dispatch_llm_speaker_identification`` is what every chain exit calls."""
    mf = _media_file(db_session, normal_user)
    sat._dispatch_llm_speaker_identification(str(mf.uuid))
    assert _names(queued) == ["llm_speaker_id"]

    queued.clear()
    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    sat._dispatch_llm_speaker_identification(str(mf.uuid))
    assert queued == []


def test_an_explicit_speaker_llm_stage_ignores_the_switch(db_session, normal_user, queued):
    from app.tasks.transcription.postprocess import _dispatch_speaker_attributes

    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    fid = str(uuid_mod.uuid4())
    _dispatch_speaker_attributes(fid, normal_user.id, ["speaker_llm"])
    assert _names(queued) == ["detect_attributes", "llm_speaker_id"]

    queued.clear()
    _user_pref(db_session, normal_user, "speaker_attribute_detection_enabled", "false")
    _dispatch_speaker_attributes(fid, normal_user.id, ["speaker_llm"])
    assert _names(queued) == ["llm_speaker_id"]


def test_manual_identify_speakers_button_works_with_the_switch_off(
    client, user_token_headers, normal_user, db_session
):
    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    mf = _media_file(db_session, normal_user)
    db_session.add(Speaker(user_id=normal_user.id, media_file_id=mf.id, name="SPEAKER_00"))
    db_session.commit()
    with (
        patch(
            "app.api.endpoints.summarization.is_llm_available",
            new=AsyncMock(return_value=True),
        ),
        patch("app.api.endpoints.summarization.identify_speakers_llm_task.delay") as delay,
    ):
        delay.return_value.id = "t-1"
        response = client.post(
            f"/api/files/{mf.uuid}/identify-speakers", headers=user_token_headers
        )
    assert response.status_code == status.HTTP_200_OK
    delay.assert_called_once_with(file_uuid=str(mf.uuid))


# --- topic extraction --------------------------------------------------------------------


def test_automatic_pipeline_queues_topics_by_default(db_session, normal_user, queued):
    mf = _media_file(db_session, normal_user)
    downstream.trigger_automatic_summarization(mf.id, str(mf.uuid))
    assert _names(queued) == ["analytics", "topics"]


def test_automatic_pipeline_skips_topics_when_the_switch_is_off(db_session, normal_user, queued):
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    mf = _media_file(db_session, normal_user)
    downstream.trigger_automatic_summarization(mf.id, str(mf.uuid))
    assert _names(queued) == ["analytics"]


def test_system_default_off_skips_topics_for_a_user_without_a_choice(
    db_session, normal_user, queued
):
    _system_pref(db_session, "auto_llm.topic_extraction", "false")
    mf = _media_file(db_session, normal_user)
    downstream.trigger_automatic_summarization(mf.id, str(mf.uuid))
    assert "topics" not in _names(queued)


def test_explicit_topic_stage_runs_even_with_the_switch_off(db_session, normal_user, queued):
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    mf = _media_file(db_session, normal_user)
    downstream.trigger_automatic_summarization(
        mf.id, str(mf.uuid), tasks_to_run=["topic_extraction"]
    )
    assert _names(queued) == ["topics"]


def test_reprocess_stage_dispatch_ignores_both_switches(
    db_session, normal_user, queued, monkeypatch
):
    from app.api.endpoints.files.reprocess import dispatch_task_by_name

    monkeypatch.setenv("SKIP_CELERY", "false")
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    fid = str(uuid_mod.uuid4())
    dispatch_task_by_name("topic_extraction", fid)
    dispatch_task_by_name("speaker_llm", fid)
    assert _names(queued) == ["topics", "llm_speaker_id"]


# --- recovery sweep ----------------------------------------------------------------------


def _old_completed_file(db, user) -> MediaFile:
    return _media_file(
        db,
        user,
        status=FileStatus.COMPLETED,
        completed_at=datetime(1999, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def sweep(db_session, monkeypatch):
    monkeypatch.setattr(LLMService, "is_configured_for_user", staticmethod(lambda db, uid: True))
    service = TaskDetectionService()

    def run(mf):
        found = service.identify_incomplete_post_transcription_files(db_session, batch_size=200)
        return next((f for f in found if f.media_file_id == mf.id), None)

    return run


def test_sweep_flags_missing_ai_steps_when_the_switches_are_on(db_session, normal_user, sweep):
    mf = _old_completed_file(db_session, normal_user)
    hit = sweep(mf)
    assert hit is not None
    assert hit.missing_topics is True
    assert hit.missing_speaker_id is True


def test_sweep_does_not_requeue_a_step_the_user_switched_off(db_session, normal_user, sweep):
    """Re-queuing would override the opt-out, and re-detect the file every 10 minutes."""
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    _user_pref(db_session, normal_user, SPEAKER_KEY, "false")
    mf = _old_completed_file(db_session, normal_user)
    hit = sweep(mf)
    assert hit is not None  # analytics / search indexing are still missing
    assert hit.missing_topics is False
    assert hit.missing_speaker_id is False


def test_sweep_respects_each_switch_independently(db_session, normal_user, sweep):
    _user_pref(db_session, normal_user, TOPIC_KEY, "false")
    mf = _old_completed_file(db_session, normal_user)
    hit = sweep(mf)
    assert hit.missing_topics is False
    assert hit.missing_speaker_id is True


# --- API -----------------------------------------------------------------------------------

_BASE = "/api/user-settings"


def test_speaker_attributes_api_round_trip(client, user_token_headers):
    got = client.get(f"{_BASE}/speaker-attributes", headers=user_token_headers).json()
    assert got["auto_llm_speaker_identification"] is True

    put = client.put(
        f"{_BASE}/speaker-attributes",
        json={"auto_llm_speaker_identification": False},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK
    assert put.json()["auto_llm_speaker_identification"] is False
    # Partial update: the neighbours are untouched.
    assert put.json()["detection_enabled"] is True

    again = client.get(f"{_BASE}/speaker-attributes", headers=user_token_headers).json()
    assert again["auto_llm_speaker_identification"] is False


def test_speaker_attributes_api_rejects_a_non_boolean(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/speaker-attributes",
        json={"auto_llm_speaker_identification": "maybe"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_speaker_attributes_system_defaults_expose_the_new_default(
    client, user_token_headers, db_session
):
    _system_pref(db_session, "auto_llm.speaker_identification", "false")
    resp = client.get(f"{_BASE}/speaker-attributes/system-defaults", headers=user_token_headers)
    assert resp.json()["auto_llm_speaker_identification"] is False


def test_auto_label_api_round_trip(client, user_token_headers):
    assert (
        client.get(f"{_BASE}/auto-label", headers=user_token_headers).json()[
            "auto_topic_extraction"
        ]
        is True
    )
    put = client.put(
        f"{_BASE}/auto-label",
        json={"auto_topic_extraction": False},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK
    assert put.json()["auto_topic_extraction"] is False


def test_auto_label_update_from_an_older_client_does_not_reset_the_switch(
    client, user_token_headers
):
    client.put(
        f"{_BASE}/auto-label", json={"auto_topic_extraction": False}, headers=user_token_headers
    )
    resp = client.put(
        f"{_BASE}/auto-label", json={"confidence_threshold": 0.9}, headers=user_token_headers
    )
    assert resp.json()["confidence_threshold"] == 0.9
    assert resp.json()["auto_topic_extraction"] is False


def test_auto_label_api_rejects_a_non_boolean(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/auto-label", json={"auto_topic_extraction": "maybe"}, headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
