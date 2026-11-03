"""The manual speaker re-detection publish is behind the legal processing gate.

``files/reprocess._dispatch_manual_rediarize`` calls ``rediarize_task.apply_async`` itself;
it never goes through ``dispatch_transcription_pipeline`` and so never reached
``_run_pre_dispatch_gate``, the shared backstop every other processing start goes through.
These tests drive the helper directly (not the route, whose own check would hide the bypass)
with the gate switched on through its real ``SystemSettings`` row.

The settings key is spelled as a literal on purpose: the test must run, and fail, against a
tree that predates the gate.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.models.media import MediaFile
from app.models.system_settings import SystemSettings

REQUIRED_KEYS_SETTING = "legal.processing_required_document_keys"
TERMS = "opentranscribe-terms-of-use"


@pytest.fixture
def owner_file(db_session, normal_user):
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="rediarize_gate.wav",
        title="rediarize_gate",
        storage_path="media/test/rediarize_gate.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    return media_file


@pytest.fixture
def published(monkeypatch, db_session):
    """Route the helper's own sessions to the test session; record every publish."""
    from app.db import session_utils
    from app.tasks.rediarize_task import rediarize_task

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(session_utils, "session_scope", _scope)
    calls: list[dict] = []
    monkeypatch.setattr(rediarize_task, "apply_async", lambda *a, **kw: calls.append(kw))
    return calls


def _require(db_session, keys: str) -> None:
    db_session.add(SystemSettings(key=REQUIRED_KEYS_SETTING, value=keys))
    db_session.commit()


def _dispatch(media_file, owner_id):
    from app.api.endpoints.files.reprocess import _dispatch_manual_rediarize

    _dispatch_manual_rediarize(
        str(media_file.uuid),
        owner_id,
        min_speakers=None,
        max_speakers=None,
        num_speakers=None,
        downstream_tasks=None,
    )


def test_rediarize_publish_is_refused_when_the_owner_has_not_accepted(
    db_session, normal_user, owner_file, published
):
    from fastapi import HTTPException

    _require(db_session, TERMS)
    with pytest.raises(HTTPException) as caught:
        _dispatch(owner_file, normal_user.id)
    assert caught.value.status_code == 403
    # HTTPException types ``detail`` as str; the gate's refusal carries a dict.
    detail: object = caught.value.detail
    assert detail == {"code": "legal_acceptance_required", "documents": [TERMS]}
    assert published == []


def test_rediarize_publishes_once_the_owner_has_accepted(
    db_session, normal_user, owner_file, published
):
    from app.legal import registry
    from app.legal.acceptance import record_acceptance

    _require(db_session, TERMS)
    doc = registry.get_document(TERMS)
    assert doc is not None
    record_acceptance(db_session, normal_user.id, doc, None)
    _dispatch(owner_file, normal_user.id)
    assert len(published) == 1
    assert published[0]["args"] == [str(owner_file.uuid)]


def test_rediarize_publishes_with_the_gate_off(db_session, normal_user, owner_file, published):
    _dispatch(owner_file, normal_user.id)
    assert len(published) == 1
