"""Tests for GPU_CONCURRENT_REQUESTS resolution in TranscriptionConfig.

Covers `_resolve_concurrent_requests()` (env parsing, "auto" routing, invalid-value
fallback) and `_auto_concurrent()` (VRAM-based concurrency calculation, capped at 12,
floored at 1). `_auto_concurrent` imports torch inside the function, so tests stub
`sys.modules["torch"]` rather than requiring a real GPU.

Note (#369): the expected concurrency values asserted here follow directly from the
VRAM-budget constants in `TranscriptionConfig._auto_concurrent`
(`backend/app/transcription/config.py`, formula `(total_vram_mb - 7000) // 4000` at
line ~402) and the `16_000` MB co-residency threshold in
`_make_room_for_local_diarizer` (`backend/app/transcription/engine/stages.py`, line
~67). Issue #369's whole-stack measurements contradict those constants: real
co-resident VRAM usage was measured at ~4.0 GB, well under what the `7000`/`4000`
budget assumes. If those constants are corrected to match measured behavior, the
concurrency values this file asserts will legitimately change — that would NOT be a
regression, and these tests should be updated to match the corrected constants rather
than treated as a contradiction to preserve.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

from app.transcription.config import TranscriptionConfig


def _fake_torch(
    *, available: bool, total_memory_mb: float | None = None, raises: Exception | None = None
) -> Any:
    """Build a stub `torch` module exposing only what `_auto_concurrent` touches."""

    class _Props:
        def __init__(self, total_memory_bytes: float) -> None:
            self.total_memory = total_memory_bytes

    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return available

        @staticmethod
        def get_device_properties(_index: int) -> _Props:
            if raises is not None:
                raise raises
            assert total_memory_mb is not None
            return _Props(total_memory_mb * 1024**2)

    module = types.ModuleType("torch")
    module.cuda = _Cuda()  # type: ignore[attr-defined]
    return module


# ---------------------------------------------------------------------------
# _resolve_concurrent_requests
# ---------------------------------------------------------------------------


def test_resolve_numeric_value(monkeypatch):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "4")
    assert TranscriptionConfig._resolve_concurrent_requests() == 4


def test_resolve_auto_routes_to_auto_concurrent(monkeypatch):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", " AUTO ")
    monkeypatch.setattr(TranscriptionConfig, "_auto_concurrent", staticmethod(lambda: 7))
    assert TranscriptionConfig._resolve_concurrent_requests() == 7


def test_resolve_zero_floors_to_one(monkeypatch):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "0")
    assert TranscriptionConfig._resolve_concurrent_requests() == 1


def test_resolve_negative_floors_to_one(monkeypatch):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "-3")
    assert TranscriptionConfig._resolve_concurrent_requests() == 1


def test_resolve_non_numeric_defaults_to_one_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "lots")
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        result = TranscriptionConfig._resolve_concurrent_requests()
    assert result == 1
    assert any("Invalid GPU_CONCURRENT_REQUESTS" in rec.message for rec in caplog.records)


def test_resolve_float_string_defaults_to_one_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "2.5")
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        result = TranscriptionConfig._resolve_concurrent_requests()
    assert result == 1
    assert any("Invalid GPU_CONCURRENT_REQUESTS" in rec.message for rec in caplog.records)


def test_resolve_empty_string_defaults_to_one_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "")
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        result = TranscriptionConfig._resolve_concurrent_requests()
    assert result == 1
    assert any("Invalid GPU_CONCURRENT_REQUESTS" in rec.message for rec in caplog.records)


def test_resolve_unset_defaults_to_one_silently(monkeypatch, caplog):
    monkeypatch.delenv("GPU_CONCURRENT_REQUESTS", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.transcription.config"):
        result = TranscriptionConfig._resolve_concurrent_requests()
    assert result == 1
    assert caplog.records == []


# ---------------------------------------------------------------------------
# _auto_concurrent
# ---------------------------------------------------------------------------


def test_auto_concurrent_3080ti_12gb(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=12288))
    assert TranscriptionConfig._auto_concurrent() == 1


def test_auto_concurrent_3090_24gb(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=24576))
    assert TranscriptionConfig._auto_concurrent() == 4


def test_auto_concurrent_a6000_49gb(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=49140))
    assert TranscriptionConfig._auto_concurrent() == 10


def test_auto_concurrent_below_baseline_floors_to_one(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=8192))
    assert TranscriptionConfig._auto_concurrent() == 1


def test_auto_concurrent_caps_at_twelve(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=262144))
    assert TranscriptionConfig._auto_concurrent() == 12


def test_auto_concurrent_no_cuda_returns_one(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=False))
    assert TranscriptionConfig._auto_concurrent() == 1


def test_auto_concurrent_detection_failure_returns_one_with_debug_log(monkeypatch, caplog):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        _fake_torch(available=True, raises=RuntimeError("no device")),
    )
    with caplog.at_level(logging.DEBUG, logger="app.transcription.config"):
        result = TranscriptionConfig._auto_concurrent()
    assert result == 1
    assert any("Auto-concurrent VRAM detection failed" in rec.message for rec in caplog.records)
