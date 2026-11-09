"""``rediarize_task`` helpers: an outage degrades loudly, a bug is not mistaken for one."""

import logging
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.tasks import rediarize_task


def _traceback_logged(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def test_asr_provider_lookup_outage_falls_back_with_a_traceback(caplog):
    db = MagicMock()
    db.query.side_effect = OperationalError("select", {}, Exception("db down"))
    with caplog.at_level(logging.WARNING, logger=rediarize_task.logger.name):
        assert rediarize_task._resolve_asr_provider(db, 5) == "local"
    assert _traceback_logged(caplog, "Could not resolve asr_provider")


def test_asr_provider_lookup_bug_propagates():
    db = MagicMock()
    db.query.side_effect = AttributeError("bug")
    with pytest.raises(AttributeError):
        rediarize_task._resolve_asr_provider(db, 5)


class _ReachedOriginalFallbackError(Exception):
    pass


def test_temp_wav_fetch_outage_falls_back_to_the_original_with_a_traceback(monkeypatch, caplog):
    from app.utils import scratch_volume

    monkeypatch.setattr(scratch_volume, "is_scratch_available", lambda: False)
    monkeypatch.setattr(rediarize_task, "temp_audio_available", lambda *a, **k: True)

    def _down(file_uuid, path):
        raise OSError("minio unreachable")

    def _original(path):
        raise _ReachedOriginalFallbackError

    monkeypatch.setattr(rediarize_task, "download_temp_audio", _down)
    monkeypatch.setattr(rediarize_task, "download_file", _original)
    with caplog.at_level(logging.WARNING, logger=rediarize_task.logger.name):
        with pytest.raises(_ReachedOriginalFallbackError):
            rediarize_task._prepare_audio("a/b.mp4", "video/mp4", "b.mp4", file_uuid="u-1")
    assert _traceback_logged(caplog, "temp WAV fetch failed")


def test_downstream_dispatch_failure_is_traced_and_the_next_stage_still_runs(monkeypatch, caplog):
    from app.api.endpoints.files import reprocess_dispatch

    calls: list[str] = []

    def _dispatch(stage, file_uuid, file_id=None, user_id=None):
        calls.append(stage)
        if stage == "summarize":
            raise ValueError("unknown stage")

    monkeypatch.setattr(reprocess_dispatch, "dispatch_task_by_name", _dispatch)
    with caplog.at_level(logging.ERROR, logger=rediarize_task.logger.name):
        rediarize_task._dispatch_downstream(["summarize", "topics"], "u-1", file_id=1, user_id=2)

    assert calls == ["summarize", "topics"]
    assert _traceback_logged(caplog, "Failed to dispatch downstream task 'summarize'")
