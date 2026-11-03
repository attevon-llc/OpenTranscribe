"""Each processing task refuses a file whose owner has not accepted, in its own body.

The routes refuse first; these are the backstops for every other way the task is queued
(the post-transcription fan-out, recovery sweeps, a setting switched on while the task sat in
the queue). A refused task returns ``skipped`` with the code and does no work.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings

pytestmark = pytest.mark.xdist_group("legal_processing_gate_settings")

TERMS = "opentranscribe-terms-of-use"
SKIPPED = {"status": "skipped", "reason": "legal_acceptance_required", "documents": [TERMS]}


@pytest.fixture
def refused_file(db_session, normal_user, monkeypatch):
    from app.db import session_utils

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(session_utils, "session_scope", _scope)
    db_session.add(SystemSettings(key=LEGAL_PROCESSING_REQUIRED_KEYS_KEY, value=TERMS))
    row = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="backstop.wav",
        title="backstop",
        storage_path="media/test/backstop.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _no_work(monkeypatch, module, name):
    def _fail(*a, **kw):
        raise AssertionError(f"{name} ran for a refused file")

    monkeypatch.setattr(module, name, _fail)


def test_summary_is_skipped(refused_file, monkeypatch):
    from app.tasks import summarization

    _no_work(monkeypatch, summarization, "_load_summarization_inputs")
    assert summarization.summarize_transcript_task.run(str(refused_file.uuid)) == SKIPPED


def test_speaker_identification_is_skipped(refused_file, monkeypatch):
    from app.tasks import speaker_identification_task as task

    _no_work(monkeypatch, task, "_load_identification_inputs")
    assert task.identify_speakers_llm_task.run(str(refused_file.uuid)) == SKIPPED


def test_topic_extraction_is_skipped(refused_file, monkeypatch):
    from app.tasks import topic_extraction

    _no_work(monkeypatch, topic_extraction, "_load_file_context")
    assert topic_extraction.extract_topics_task.run(str(refused_file.uuid)) == SKIPPED


def test_redaction_detection_is_skipped(refused_file, monkeypatch):
    from app.services.redaction import service as redaction_service
    from app.tasks import redaction_task

    def _fail(*a, **kw):
        raise AssertionError("redaction detection ran for a refused file")

    monkeypatch.setattr(redaction_service.RedactionService, "__init__", _fail)
    assert redaction_task.redaction_detect_task.run(file_id=refused_file.id) == SKIPPED


def test_manual_rediarize_is_skipped(refused_file, monkeypatch):
    from app.tasks import rediarize_task

    _no_work(monkeypatch, rediarize_task, "_start")
    assert rediarize_task.rediarize_task.run(str(refused_file.uuid)) == SKIPPED


def test_url_import_is_refused_before_download(refused_file, monkeypatch, db_session):
    from app.tasks import youtube_processing

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(youtube_processing, "session_scope", _scope)
    monkeypatch.setattr(youtube_processing.settings, "YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED", False)
    _no_work(monkeypatch, youtube_processing, "send_youtube_notification_via_redis")
    result = youtube_processing.process_youtube_url_task.run(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        user_id=refused_file.user_id,
        file_uuid=str(refused_file.uuid),
    )
    assert result == {
        "status": "skipped",
        "message": f"legal_acceptance_required: {TERMS}",
        "file_id": refused_file.id,
    }
    db_session.refresh(refused_file)
    assert refused_file.status == "error"
    assert refused_file.error_category == "legal_acceptance_required"


def test_playlist_import_is_refused_before_extraction(refused_file, monkeypatch, db_session):
    from app.tasks import youtube_processing

    @contextlib.contextmanager
    def _scope():
        yield db_session

    sent: list = []
    monkeypatch.setattr(youtube_processing, "session_scope", _scope)
    monkeypatch.setattr(
        youtube_processing, "_send_playlist_notification", lambda *a: sent.append(a)
    )
    _no_work(monkeypatch, youtube_processing, "_process_playlist_with_db")
    result = youtube_processing.process_youtube_playlist_task.run(
        url="https://www.youtube.com/playlist?list=PL0", user_id=refused_file.user_id
    )
    assert result["status"] == "error"
    assert result["message"] == f"legal_acceptance_required: {TERMS}"
    assert [s[1] for s in sent] == ["error"]
