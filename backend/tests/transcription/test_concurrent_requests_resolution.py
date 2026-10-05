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

import pytest

from app.transcription import config as config_mod
from app.transcription.config import TranscriptionConfig


@pytest.fixture(autouse=True)
def _no_host_memory_cap(monkeypatch, tmp_path):
    """Keep the VRAM-only cases independent of the RAM of whatever host runs the suite.

    Without this the A6000 case would be capped by a 16 GiB CI runner. The host-cap tests
    below re-point these probes at files they write.
    """
    missing = str(tmp_path / "absent")
    monkeypatch.setattr(config_mod, "_PROC_MEMINFO", missing)
    monkeypatch.setattr(config_mod, "_CGROUP_V2_MEMORY_MAX", missing)
    monkeypatch.setattr(config_mod, "_CGROUP_V1_MEMORY_LIMIT", missing)


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


# ---------------------------------------------------------------------------
# Host-RAM cap (issue #1073 step 1)
# ---------------------------------------------------------------------------
#
# On common single-GPU shapes (4 vCPU / 16 GiB next to a 24 GB GPU) host memory binds before
# VRAM: every concurrent task decodes its whole file into RAM. Auto concurrency is therefore
# min(VRAM-based, host-based), with the host budget read from the cgroup limit when there is one.


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Point the host-memory probes at files this test controls."""
    for name in ("GPU_HOST_BASELINE_MB", "GPU_PER_TASK_HOST_MB"):
        monkeypatch.delenv(name, raising=False)
    meminfo = tmp_path / "meminfo"
    v2 = tmp_path / "memory.max"
    v1 = tmp_path / "memory.limit_in_bytes"
    monkeypatch.setattr(config_mod, "_PROC_MEMINFO", str(meminfo))
    monkeypatch.setattr(config_mod, "_CGROUP_V2_MEMORY_MAX", str(v2))
    monkeypatch.setattr(config_mod, "_CGROUP_V1_MEMORY_LIMIT", str(v1))

    def _set(*, mem_total_mb: int, cgroup_v2: str | None = None, cgroup_v1: str | None = None):
        meminfo.write_text(f"MemTotal:       {mem_total_mb * 1024} kB\nMemFree: 1 kB\n")
        if cgroup_v2 is not None:
            v2.write_text(cgroup_v2 + "\n")
        if cgroup_v1 is not None:
            v1.write_text(cgroup_v1 + "\n")

    return _set


def _gib(n: float) -> int:
    return int(n * 1024)


def test_host_budget_prefers_the_cgroup_limit(host):
    host(mem_total_mb=_gib(64), cgroup_v2=str(13 * 1024**3))
    assert TranscriptionConfig._host_memory_budget_mb() == 13 * 1024


def test_an_unlimited_cgroup_falls_back_to_mem_total(host):
    host(mem_total_mb=_gib(16), cgroup_v2="max")
    assert TranscriptionConfig._host_memory_budget_mb() == _gib(16)


def test_a_cgroup_v1_limit_is_read_too(host):
    host(mem_total_mb=_gib(64), cgroup_v1=str(12 * 1024**3))
    assert TranscriptionConfig._host_memory_budget_mb() == 12 * 1024


def test_a_cgroup_limit_above_physical_ram_is_ignored(host):
    """cgroup v1 reports ~8 EiB for "no limit"; physical RAM is the real ceiling."""
    host(mem_total_mb=_gib(16), cgroup_v1=str(2**63 - 4096))
    assert TranscriptionConfig._host_memory_budget_mb() == _gib(16)


def test_vram_bound_host(monkeypatch, host):
    """A 24 GB card on a roomy host: VRAM decides (4), as before this change."""
    host(mem_total_mb=_gib(64))
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=24576))
    assert TranscriptionConfig._auto_concurrent() == 4


def test_ram_bound_host(monkeypatch, host):
    """A 48 GB card in a 16 GiB pod: VRAM alone says 10, host memory must cap it."""
    host(mem_total_mb=_gib(64), cgroup_v2=str(16 * 1024**3))
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=49140))
    expected = (16 * 1024 - config_mod.DEFAULT_HOST_BASELINE_MB) // (
        config_mod.DEFAULT_PER_TASK_HOST_MB
    )
    assert 1 <= expected < 10
    assert TranscriptionConfig._auto_concurrent() == expected


def test_host_sizing_is_env_tunable(monkeypatch, host):
    host(mem_total_mb=_gib(64), cgroup_v2=str(16 * 1024**3))
    monkeypatch.setenv("GPU_HOST_BASELINE_MB", "4096")
    monkeypatch.setenv("GPU_PER_TASK_HOST_MB", "1024")
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=49140))
    assert TranscriptionConfig._auto_concurrent() == min(10, (16384 - 4096) // 1024)


def test_a_tiny_host_still_gets_one_slot(monkeypatch, host):
    host(mem_total_mb=_gib(4))
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=49140))
    assert TranscriptionConfig._auto_concurrent() == 1


def test_unreadable_host_memory_leaves_the_vram_answer(monkeypatch, host):
    # No files written at all: nothing to read, so no host cap.
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=24576))
    assert TranscriptionConfig._auto_concurrent() == 4


def test_auto_on_a_32_gib_host_may_reach_twelve(monkeypatch, host):
    """Load-test evidence (issue #1073): 32 GB hosts stayed GPU-bound at 12 concurrent tasks.
    The per-task default must not cap them at 7."""
    host(mem_total_mb=_gib(128), cgroup_v2=str(32 * 1024**3))
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=262144))
    assert TranscriptionConfig._auto_concurrent() == 12


def test_auto_on_a_16_gib_host_stays_at_or_below_six(monkeypatch, host):
    """Load-test evidence (issue #1073): 16 GB hosts were OOM-killed at 8+ concurrent tasks."""
    host(mem_total_mb=_gib(128), cgroup_v2=str(16 * 1024**3))
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, total_memory_mb=262144))
    assert 1 <= TranscriptionConfig._auto_concurrent() <= 6
