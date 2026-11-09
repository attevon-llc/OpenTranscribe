"""B4b broad-except lane: handlers absorb only what their try body can raise, with a traceback.

Every pair is an outage that must stay visible (the documented error is absorbed WITH its
traceback) and a programming error that must no longer be swallowed (it propagates).
"""

from __future__ import annotations

import logging
import sys
import types
import urllib.error
from typing import cast
from unittest.mock import MagicMock

import numpy as np
import pytest
from kombu.exceptions import OperationalError as KombuOperationalError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError


def _db_down(*_a, **_k):
    raise OperationalError("select", {}, Exception("db down"))


def _tb(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


# --- engine/audio_loader.py ------------------------------------------------------------------


def test_shared_wav_write_oserror_logs_traceback(tmp_path, caplog):
    from app.transcription.engine import audio_loader

    blocker = tmp_path / "file"
    blocker.write_text("x")  # makedirs under a regular file raises NotADirectoryError
    with caplog.at_level(logging.WARNING):
        out = audio_loader.write_wav_to_shared_volume(
            np.zeros(10, dtype=np.float32), str(blocker / "sub"), "t1"
        )
    assert out is None
    assert _tb(caplog, "Failed to write shared-volume WAV")


def test_shared_wav_write_programming_error_propagates(tmp_path):
    from app.transcription.engine import audio_loader

    with pytest.raises(TypeError):
        audio_loader.write_wav_to_shared_volume(cast("np.ndarray", None), str(tmp_path), "t1")


def test_shared_wav_corrupt_read_logs_traceback(tmp_path, caplog):
    from app.transcription.engine import audio_loader

    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    with caplog.at_level(logging.WARNING):
        assert audio_loader.load_from_shared_volume(str(bad)) is None
    assert _tb(caplog, "Failed to mmap-load")


# --- tasks/transcription/waveform_generator.py ----------------------------------------------


def test_waveform_numpy_resample_corrupt_wav_logs_traceback(tmp_path, caplog):
    from app.tasks.transcription.waveform_generator import WaveformGenerator

    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFFxxxxWAVE")
    with caplog.at_level(logging.WARNING):
        assert WaveformGenerator().generate_from_16khz_wav(str(bad)) is None
    assert _tb(caplog, "numpy-resample waveform failed")


def test_waveform_extract_missing_ffprobe_logs_traceback(monkeypatch, caplog):
    from app.tasks.transcription import waveform_generator as wg

    def no_binary(*a, **k):
        raise FileNotFoundError("ffprobe")

    gen = wg.WaveformGenerator()  # its constructor probes ffmpeg for real
    monkeypatch.setattr(wg.subprocess, "run", no_binary)
    with caplog.at_level(logging.ERROR):
        assert gen._extract_single_waveform("/x.wav", 100) is None
    assert _tb(caplog, "Error extracting waveform")


def test_waveform_extract_programming_error_propagates(monkeypatch):
    from app.tasks.transcription import waveform_generator as wg

    def bug(*a, **k):
        raise TypeError("bug")

    gen = wg.WaveformGenerator()
    monkeypatch.setattr(wg.subprocess, "run", bug)
    with pytest.raises(TypeError):
        gen._extract_single_waveform("/x.wav", 100)


# --- tasks/transcription/run_ownership.py ---------------------------------------------------


def test_replaced_by_newer_run_db_outage_logs_traceback(monkeypatch, caplog):
    from app.tasks.transcription import run_ownership

    monkeypatch.setattr(run_ownership, "session_scope", _db_down)
    with caplog.at_level(logging.WARNING):
        assert run_ownership.replaced_by_newer_run("t") is False
    assert _tb(caplog, "was replaced")


def test_replaced_by_newer_run_programming_error_propagates(monkeypatch):
    from app.tasks.transcription import run_ownership

    def bug():
        raise TypeError("bug")

    monkeypatch.setattr(run_ownership, "session_scope", bug)
    with pytest.raises(TypeError):
        run_ownership.replaced_by_newer_run("t")


# --- tasks/transcription/pipeline_error.py --------------------------------------------------


def test_cancel_check_db_outage_logs_traceback(monkeypatch, caplog):
    from app.tasks.transcription import pipeline_error

    monkeypatch.setattr(pipeline_error, "session_scope", _db_down)
    with caplog.at_level(logging.WARNING):
        assert pipeline_error._cancelled_run_owner("u", "t") is None
    assert _tb(caplog, "cancel check")


def test_cancel_check_programming_error_propagates(monkeypatch):
    from app.tasks.transcription import pipeline_error

    def bug():
        raise TypeError("bug")

    monkeypatch.setattr(pipeline_error, "session_scope", bug)
    with pytest.raises(TypeError):
        pipeline_error._cancelled_run_owner("u", "t")


# --- tasks/transcription/postprocess.py -----------------------------------------------------


def test_run_completed_db_outage_logs_traceback(monkeypatch, caplog):
    from app.tasks.transcription import postprocess

    monkeypatch.setattr(postprocess, "session_scope", _db_down)
    with caplog.at_level(logging.WARNING):
        assert postprocess._run_completed("t") is False
    assert _tb(caplog, "status of run")


def test_embedding_stage_broker_outage_logs_traceback(monkeypatch, caplog):
    from app.tasks import speaker_embedding_task
    from app.tasks.transcription import postprocess

    monkeypatch.setattr(postprocess, "voiceprints_allowed", lambda _f: True)
    monkeypatch.setattr(postprocess, "send_progress_notification", lambda *a, **k: None)
    task = MagicMock()
    task.apply_async.side_effect = KombuOperationalError("broker down")
    monkeypatch.setattr(speaker_embedding_task, "extract_speaker_embeddings_task", task)
    gpu_result = {"file_id": 1, "user_id": 2, "file_uuid": "u", "task_id": "t"}
    with caplog.at_level(logging.WARNING):
        assert postprocess._dispatch_embedding_stage(gpu_result) is False
    assert _tb(caplog, "speaker embedding task")


def test_embedding_stage_programming_error_propagates(monkeypatch):
    from app.tasks import speaker_embedding_task
    from app.tasks.transcription import postprocess

    monkeypatch.setattr(postprocess, "voiceprints_allowed", lambda _f: True)
    monkeypatch.setattr(postprocess, "send_progress_notification", lambda *a, **k: None)
    task = MagicMock()
    task.apply_async.side_effect = TypeError("bug")
    monkeypatch.setattr(speaker_embedding_task, "extract_speaker_embeddings_task", task)
    gpu_result = {"file_id": 1, "user_id": 2, "file_uuid": "u", "task_id": "t"}
    with pytest.raises(TypeError):
        postprocess._dispatch_embedding_stage(gpu_result)


# --- tasks/transcription/notifications.py ---------------------------------------------------


def test_file_metadata_db_outage_logs_traceback(monkeypatch, caplog):
    from app.tasks.transcription import notifications

    monkeypatch.setattr(notifications, "session_scope", _db_down)
    with caplog.at_level(logging.WARNING):
        meta = notifications.get_file_metadata(5)
    assert meta["filename"] == "File 5"
    assert _tb(caplog, "file metadata")


def test_file_metadata_programming_error_propagates(monkeypatch):
    from app.tasks.transcription import notifications

    def bug():
        raise TypeError("bug")

    monkeypatch.setattr(notifications, "session_scope", bug)
    with pytest.raises(TypeError):
        notifications.get_file_metadata(5)


def test_notification_redis_outage_logs_traceback(monkeypatch, caplog):
    from app.models.media import FileStatus
    from app.tasks.transcription import notifications

    monkeypatch.setattr(
        notifications,
        "get_file_metadata",
        lambda _f: {"file_uuid": "u", "filename": "f", "content_type": "c", "file_size": 1},
    )
    monkeypatch.setattr(
        "app.services.file_availability.is_notification_suppressed", lambda *a: False
    )

    def down(*a, **k):
        raise RedisConnectionError("down")

    monkeypatch.setattr(notifications, "send_ws_event_for_file", down)
    with caplog.at_level(logging.ERROR):
        assert notifications.send_notification_via_redis(1, 2, FileStatus.PROCESSING, "m") is False
    assert _tb(caplog, "notification via Redis")


# --- tasks/transcription/downstream.py ------------------------------------------------------


def test_owner_llm_check_db_outage_logs_traceback(monkeypatch, caplog):
    from app.tasks.transcription import downstream

    monkeypatch.setattr(downstream, "session_scope", _db_down)
    with caplog.at_level(logging.WARNING):
        assert downstream._owner_llm_configured(3) is True
    assert _tb(caplog, "LLM availability check")


def test_owner_llm_check_programming_error_propagates(monkeypatch):
    from app.tasks.transcription import downstream

    def bug():
        raise TypeError("bug")

    monkeypatch.setattr(downstream, "session_scope", bug)
    with pytest.raises(TypeError):
        downstream._owner_llm_configured(3)


# --- tasks/transcription/core.py ------------------------------------------------------------


def test_retry_priority_outside_worker_is_fresh_run_without_catching_anything():
    from app.tasks.transcription.core import _running_at_retry_priority

    assert _running_at_retry_priority() is False


# --- transcription/diar_sidecar_status.py ---------------------------------------------------


def test_sidecar_healthz_unreachable_is_unhealthy_with_traceback(monkeypatch, caplog):
    from app.transcription import diar_sidecar_status as dss

    def refused(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(dss.urllib.request, "urlopen", refused)
    with caplog.at_level(logging.DEBUG):
        assert dss._sidecar_healthy_uncached("http://x") is False
    assert _tb(caplog, "/healthz probe failed")


def test_sidecar_healthz_programming_error_propagates(monkeypatch):
    from app.transcription import diar_sidecar_status as dss

    def bug(*a, **k):
        raise TypeError("bug")

    monkeypatch.setattr(dss.urllib.request, "urlopen", bug)
    with pytest.raises(TypeError):
        dss._sidecar_healthy_uncached("http://x")


def test_require_sidecar_db_outage_falls_back_to_env_with_traceback(monkeypatch, caplog):
    from app.transcription import diar_sidecar_status as dss

    monkeypatch.setenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", "true")
    monkeypatch.setattr("app.db.session_utils.session_scope", _db_down)
    with caplog.at_level(logging.DEBUG):
        assert dss.diarizer_require_sidecar() is True
    assert _tb(caplog, "engine.diarizer_require_sidecar")


# --- transcription/vram_budget.py (torch faked; real-CUDA proof is the Phase 3 -m gpu run) ---


def test_vram_budget_cuda_runtime_error_disables_admission_with_traceback(monkeypatch, caplog):
    from app.transcription import vram_budget

    def boom(*a, **k):
        raise RuntimeError("CUDA error: device-side assert")

    cuda = types.SimpleNamespace(is_available=lambda: True, mem_get_info=boom)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))
    with caplog.at_level(logging.WARNING):
        assert vram_budget.configure_from_device(0) is None
    assert _tb(caplog, "could not read device memory")


def test_vram_budget_programming_error_propagates(monkeypatch):
    from app.transcription import vram_budget

    def bug(*a, **k):
        raise TypeError("bug")

    cuda = types.SimpleNamespace(is_available=lambda: True, mem_get_info=bug)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))
    with pytest.raises(TypeError):
        vram_budget.configure_from_device(0)
