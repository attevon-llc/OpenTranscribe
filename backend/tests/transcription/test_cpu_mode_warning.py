"""Tests for the CPU-mode misconfiguration warning in TranscriptionConfig.

The warning is intended to fire once per process when a CPU-only worker
boots with a configuration intended for GPU (heavy Whisper model and/or
PyAnnote diarization enabled). It must stay silent for safe CPU configs
and for any GPU config.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.transcription import config as cfgmod
from app.transcription.config import TranscriptionConfig


@pytest.fixture(autouse=True)
def _reset_warning_guard():
    """Reset the module-level emit guard between tests."""
    cfgmod._CPU_MODE_WARNING_EMITTED = False
    yield
    cfgmod._CPU_MODE_WARNING_EMITTED = False


def _config(**overrides: Any) -> TranscriptionConfig:
    base: dict[str, Any] = {
        "model_name": "base",
        "device": "cpu",
        "enable_diarization": False,
    }
    base.update(overrides)
    return TranscriptionConfig(**base)


def test_warns_on_cpu_with_heavy_model(caplog):
    cfg = _config(model_name="large-v3-turbo")
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)

    assert any("large-v3-turbo" in rec.message for rec in caplog.records)
    assert cfgmod._CPU_MODE_WARNING_EMITTED is True


def test_warns_on_cpu_with_diarization_enabled(caplog):
    cfg = _config(enable_diarization=True)
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)

    assert any("ENABLE_DIARIZATION" in rec.message for rec in caplog.records)


def test_warns_only_once_per_process(caplog):
    cfg = _config(model_name="large-v3-turbo", enable_diarization=True)
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)

    cpu_warnings = [r for r in caplog.records if "CPU-only mode detected" in r.message]
    assert len(cpu_warnings) == 1


def test_silent_for_safe_cpu_config(caplog):
    cfg = _config(model_name="base", enable_diarization=False)
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)
    assert caplog.records == []
    assert cfgmod._CPU_MODE_WARNING_EMITTED is False


def test_silent_for_tiny_cpu_config(caplog):
    cfg = _config(model_name="tiny.en", enable_diarization=False)
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)
    assert caplog.records == []


def test_silent_for_gpu_config_even_when_misconfigured(caplog):
    cfg = _config(device="cuda", model_name="large-v3-turbo", enable_diarization=True)
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        TranscriptionConfig._maybe_warn_cpu_mode_misconfigured(cfg)
    assert caplog.records == []
    assert cfgmod._CPU_MODE_WARNING_EMITTED is False
