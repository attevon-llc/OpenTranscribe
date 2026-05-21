"""Tests for the _device_mode_info helper used by GET /system/stats.

Drives the CPU-only advisory banner in the frontend Settings panel. The
helper must distinguish user-forced CPU mode from auto-fallback (no usable
GPU) so the UI can show the right reason text.
"""

from __future__ import annotations

import pytest

from app.api.endpoints.system import _device_mode_info


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FORCE_CPU_MODE", raising=False)
    monkeypatch.setenv("WHISPER_MODEL", "base")
    monkeypatch.setenv("ENABLE_DIARIZATION", "false")


def test_gpu_available_no_force_returns_cuda():
    out = _device_mode_info([{"available": True, "name": "RTX A6000"}])
    assert out["device_mode"] == "cuda"
    assert out["force_cpu_mode"] is False


def test_no_gpu_no_force_returns_cpu_auto_fallback():
    out = _device_mode_info([{"available": False}])
    assert out["device_mode"] == "cpu"
    assert out["force_cpu_mode"] is False


def test_force_cpu_overrides_gpu_availability(monkeypatch):
    monkeypatch.setenv("FORCE_CPU_MODE", "true")
    out = _device_mode_info([{"available": True, "name": "RTX A6000"}])
    assert out["device_mode"] == "cpu"
    assert out["force_cpu_mode"] is True


def test_force_cpu_with_no_gpu(monkeypatch):
    monkeypatch.setenv("FORCE_CPU_MODE", "true")
    out = _device_mode_info([{"available": False}])
    assert out["device_mode"] == "cpu"
    assert out["force_cpu_mode"] is True


def test_empty_gpu_list_treated_as_no_gpu():
    out = _device_mode_info([])
    assert out["device_mode"] == "cpu"
    assert out["force_cpu_mode"] is False


def test_force_cpu_value_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("FORCE_CPU_MODE", "TRUE")
    out = _device_mode_info([{"available": True}])
    assert out["force_cpu_mode"] is True
    assert out["device_mode"] == "cpu"


def test_whisper_model_passthrough(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v3-turbo")
    out = _device_mode_info([{"available": True}])
    assert out["whisper_model"] == "large-v3-turbo"


def test_loading_state_treated_as_gpu_to_avoid_banner_flash():
    """On fresh GPU-host startup, get_gpu_usage returns
    {"available": False, "loading": True} until the worker pushes its
    first stats. We classify this as cuda to prevent the CPU banner from
    flashing on cold start."""
    out = _device_mode_info([{"available": False, "loading": True, "name": "Loading GPU stats..."}])
    assert out["device_mode"] == "cuda"
    assert out["force_cpu_mode"] is False


def test_diarization_flag_parsed(monkeypatch):
    monkeypatch.setenv("ENABLE_DIARIZATION", "true")
    out = _device_mode_info([{"available": True}])
    assert out["diarization_enabled"] is True

    monkeypatch.setenv("ENABLE_DIARIZATION", "FALSE")
    out = _device_mode_info([{"available": True}])
    assert out["diarization_enabled"] is False
