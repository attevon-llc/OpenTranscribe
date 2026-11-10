"""A failing completion/metering hook must never fail the run it reports on.

The three run-ending paths (finished, failed, cancelled) all fire the cloud-edition
completion hook. Whatever that seam or the DB read behind it raises, the run's own outcome
stands and the failure is logged. See https://docs.pytest.org/en/stable/how-to/monkeypatch.html
for the injection technique.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy.exc import SQLAlchemyError

from app.models.media import MediaFile
from app.services import transcription_retry
from app.tasks.transcription import cancellation
from app.tasks.transcription import hooks
from app.tasks.transcription import postprocess


def _exploding_hook(_ctx):
    raise RuntimeError("billing seam down")


def test_failed_run_reservation_release_survives_a_raising_hook(monkeypatch, caplog):
    monkeypatch.setattr(hooks, "fire_transcription_complete", _exploding_hook)
    media_file = MediaFile(id=1, uuid="u-1", user_id=2, organization_id=None)

    with caplog.at_level(logging.ERROR, logger=transcription_retry.logger.name):
        transcription_retry._release_reservation(media_file, "task-1")  # must not raise

    assert "Failure-path completion hook raised" in caplog.text


def test_cancelled_run_still_finalizes_when_the_hook_raises(monkeypatch, caplog):
    @contextmanager
    def _scope():
        yield MagicMock()

    cleared: list[str] = []
    monkeypatch.setattr(cancellation, "session_scope", _scope)
    monkeypatch.setattr(cancellation, "get_refreshed_object", lambda *_a, **_k: None)
    monkeypatch.setattr(cancellation, "clear_cancel", cleared.append)
    monkeypatch.setattr(hooks, "fire_transcription_complete", _exploding_hook)

    with caplog.at_level(logging.ERROR, logger=cancellation.logger.name):
        result = cancellation.finalize_cancelled_run("task-1", 7, "u-7", 3)

    assert result["status"] == "cancelled"
    assert cleared == ["task-1"]
    assert "Cancellation completion hook raised" in caplog.text


def test_metering_read_failure_does_not_fail_a_finished_run(monkeypatch, caplog):
    fired: list[object] = []
    monkeypatch.setattr(hooks, "fire_transcription_complete", fired.append)
    db = MagicMock()
    db.query.side_effect = SQLAlchemyError("connection lost")

    with caplog.at_level(logging.WARNING, logger=postprocess.logger.name):
        postprocess._fire_completion_metering(db, 5, "run-5", "local")  # must not raise

    assert fired == []
    assert "Completion metering read failed for file 5" in caplog.text
