"""Transcription, diarization and storage knobs read through ``app.core.env`` keep their
variable name, default and parse rules (the former raw ``os.getenv`` sites)."""

from __future__ import annotations

import os

import pytest

from app.core import env
from app.services import minio_service
from app.services.media_source_resolution import MediaSourceResolver
from app.tasks.transcription.audio_processor import _ffmpeg_threads
from app.transcription import diar_sidecar_status
from app.transcription.config import TranscriptionConfig
from app.transcription.engine.config import EngineConfig
from app.transcription.transcriber import _max_oom_halvings
from app.transcription.vram_budget import _stage_override_raw


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"), [(None, 2), ("4", 4), ("0", 0), ("-1", 0), ("junk", 2)]
)
def test_gpu_oom_max_halvings(monkeypatch, value, expected):
    _set(monkeypatch, "GPU_OOM_MAX_HALVINGS", value)
    assert _max_oom_halvings() == expected


@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [(None, 0), ("3", 3), ("0", 0), ("weird", 0)])
def test_ffmpeg_threads(monkeypatch, value, expected):
    _set(monkeypatch, "FFMPEG_THREADS", value)
    assert _ffmpeg_threads() == expected


@pytest.mark.unit
@pytest.mark.parametrize(("value", "divisor"), [(None, 8), ("4", 4), ("junk", 8), ("0", 1)])
def test_ffmpeg_threads_auto_divides_by_cpu_worker_concurrency(monkeypatch, value, divisor):
    from app.tasks.transcription import audio_processor

    monkeypatch.setattr(audio_processor, "effective_cpu_count", lambda: 64)
    _set(monkeypatch, "FFMPEG_THREADS", "auto")
    _set(monkeypatch, "CPU_WORKER_CONCURRENCY", value)
    assert _ffmpeg_threads() == 64 // divisor


@pytest.mark.unit
def test_stage_vram_overrides_are_read_per_stage(monkeypatch):
    monkeypatch.setenv("GPU_STAGE_VRAM_MB_ASR", " 1234 ")
    monkeypatch.delenv("GPU_STAGE_VRAM_MB_DIARIZATION", raising=False)
    assert _stage_override_raw("asr") == "1234"
    assert _stage_override_raw("diarization") == ""
    assert _stage_override_raw("other") == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("true", True), ("FALSE", False), ("0", False), ("no", False), ("off", True)],
)
def test_mediacms_verify_ssl_env_fallback(monkeypatch, value, expected):
    _set(monkeypatch, "MEDIACMS_VERIFY_SSL", value)
    assert MediaSourceResolver.__new__(MediaSourceResolver).verify_ssl is expected


@pytest.mark.unit
def test_ssl_cert_file_overrides_certifi_bundle(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/custom.pem")
    assert minio_service._ca_bundle_path() == "/etc/ssl/custom.pem"
    monkeypatch.delenv("SSL_CERT_FILE")
    assert minio_service._ca_bundle_path().endswith(".pem")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"), [(None, 1), ("3", 3), ("0", 1), ("junk", 1), (" 2 ", 2)]
)
def test_gpu_concurrent_requests(monkeypatch, value, expected):
    _set(monkeypatch, "GPU_CONCURRENT_REQUESTS", value)
    assert TranscriptionConfig._resolve_concurrent_requests() == expected


@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [(None, 0.5), ("0.9", 0.9), ("", 0.5), ("x", 0.5)])
def test_engine_db_env_float(monkeypatch, value, expected):
    _set(monkeypatch, "ENGINE_TEST_FLOAT", value)
    assert EngineConfig._db_env_float(None, "", "ENGINE_TEST_FLOAT", 0.5) == expected


@pytest.mark.unit
def test_sidecar_diagnostics_reports_max_inflight_from_env(monkeypatch):
    monkeypatch.setattr(diar_sidecar_status, "sidecar_healthy", lambda url: False)
    monkeypatch.setattr(diar_sidecar_status, "sidecar_ready", lambda url: False)
    monkeypatch.setattr(diar_sidecar_status, "sidecar_status", lambda url: {})
    monkeypatch.delenv("DIAR_NATIVE_MAX_INFLIGHT", raising=False)
    assert diar_sidecar_status.sidecar_diagnostics("http://x")["max_inflight"] == 2
    monkeypatch.setenv("DIAR_NATIVE_MAX_INFLIGHT", "6")
    assert diar_sidecar_status.sidecar_diagnostics("http://x")["max_inflight"] == 6


@pytest.mark.unit
def test_env_raw_keeps_blank_values_and_env_publish_sets_the_process_env(monkeypatch):
    monkeypatch.setenv("A7_RAW", " x ")
    assert env.env_raw("A7_RAW") == " x "
    monkeypatch.delenv("A7_RAW")
    assert env.env_raw("A7_RAW") is None
    monkeypatch.setenv("A7_PUB", "old")
    env.env_publish("A7_PUB", "new")
    assert os.environ["A7_PUB"] == "new"
