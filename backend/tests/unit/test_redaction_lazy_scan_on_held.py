"""Redaction on a held file (plan §3.4): the first scan runs, a rescan does not.

Masking is a read-time view over cached spans; the stored transcript never changes. A RESCAN
replaces the spans, which changes what a held file displays and exports, so it is skipped. The
FIRST scan of a file that was never scanned (the lazy scan a transcript read queues when masking
is mandated) is allowed: refusing it would withhold the held file's transcript for good, which
defeats "a held file stays readable".
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.models.media import MediaFile


@pytest.fixture
def scoped(db_session, monkeypatch):
    from app.db import session_utils

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(session_utils, "session_scope", _scope)
    monkeypatch.setattr(
        "app.legal.processing_gate.check_processing_allowed_for_file", lambda *a: None
    )
    return db_session


@pytest.fixture
def detections(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _detect(db, file_id):
        calls.append(file_id)
        return {"status": "done"}

    monkeypatch.setattr(
        "app.services.redaction.service.RedactionService.detect_and_store", staticmethod(_detect)
    )
    monkeypatch.setattr("app.tasks.redaction_task._notify", lambda *a: None)
    monkeypatch.setattr("app.tasks.redaction_task._mark_redaction_task", lambda *a, **k: None)
    # Task-row bookkeeping needs a Celery request id; it is not what is under test.
    monkeypatch.setattr("app.utils.task_utils.create_task_record", lambda *a, **k: None)
    monkeypatch.setattr("app.utils.task_utils.update_task_status", lambda *a, **k: None)
    return calls


def _held(db, owner, *, version: str | None) -> MediaFile:
    row = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="held.wav",
        storage_path=f"media/test/{uuid.uuid4()}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        user_id=owner.id,
        legal_hold=True,
        redaction_model_version=version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_the_first_scan_of_a_held_file_runs(scoped, normal_user, detections):
    from app.tasks import redaction_task

    never_scanned = _held(scoped, normal_user, version=None)

    result = redaction_task.redaction_detect_task.run(file_id=never_scanned.id)

    assert detections == [never_scanned.id]
    assert result["status"] == "done"


def test_a_rescan_of_a_held_file_is_skipped(scoped, normal_user, detections):
    from app.tasks import redaction_task

    scanned = _held(scoped, normal_user, version="v1")

    result = redaction_task.redaction_detect_task.run(file_id=scanned.id)

    assert detections == []
    assert result == {"status": "skipped", "reason": "legal_hold"}
