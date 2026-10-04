"""effective_cpu_count(): container CPU quota, not host core count (issue #1069)."""

import os

import pytest

from app.utils import cpu_budget
from app.utils.cpu_budget import effective_cpu_count


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    def _setup(cpu_max: str | None, affinity: int = 16, host: int = 16):
        path = tmp_path / "cpu.max"
        if cpu_max is not None:
            path.write_text(cpu_max)
        monkeypatch.setattr(cpu_budget, "CGROUP_CPU_MAX", str(path))
        monkeypatch.setattr(
            os, "sched_getaffinity", lambda _pid: set(range(affinity)), raising=False
        )
        monkeypatch.setattr(os, "cpu_count", lambda: host)

    return _setup


@pytest.mark.parametrize(
    ("cpu_max", "expected"),
    [
        ("max 100000\n", 16),
        ("400000 100000\n", 4),
        ("150000 100000\n", 2),  # rounds up
        ("50000 100000\n", 1),  # minimum 1
        (None, 16),  # missing file
        ("garbage\n", 16),
        ("100000 0\n", 16),
    ],
)
def test_cgroup_quota(fake_env, cpu_max, expected):
    fake_env(cpu_max)
    assert effective_cpu_count() == expected


def test_affinity_smaller_than_quota(fake_env):
    fake_env("800000 100000\n", affinity=3)
    assert effective_cpu_count() == 3


def test_quota_smaller_than_affinity(fake_env):
    fake_env("200000 100000\n", affinity=8)
    assert effective_cpu_count() == 2


def test_no_affinity_falls_back_to_cpu_count(fake_env, monkeypatch):
    fake_env("max 100000\n", host=6)
    monkeypatch.delattr(os, "sched_getaffinity")
    assert effective_cpu_count() == 6


def test_ffmpeg_threads_auto_uses_quota(fake_env, monkeypatch):
    from app.tasks.transcription.audio_processor import _ffmpeg_threads

    fake_env("400000 100000\n")
    monkeypatch.setenv("FFMPEG_THREADS", "auto")
    monkeypatch.setenv("CPU_WORKER_CONCURRENCY", "2")
    assert _ffmpeg_threads() == 2


def test_pii_worker_count_uses_quota(fake_env, monkeypatch):
    from app.services.redaction import pii_pool

    fake_env("300000 100000\n")
    monkeypatch.delenv("REDACTION_PII_WORKERS", raising=False)
    assert pii_pool._worker_count() == 2


def test_hardware_detection_omp_threads_use_quota(fake_env):
    from app.utils.hardware_detection import HardwareConfig

    fake_env("300000 100000\n")
    cfg = HardwareConfig.__new__(HardwareConfig)
    cfg.device = "cpu"
    assert cfg.get_environment_variables()["OMP_NUM_THREADS"] == "3"


def test_torch_threads_divided_across_children(fake_env, monkeypatch):
    import sys
    import types

    from app.core import celery as celery_mod

    fake_env("800000 100000\n")
    calls: list[int] = []
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(set_num_threads=calls.append))
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.delenv("TORCH_NUM_THREADS", raising=False)
    monkeypatch.setattr(sys, "argv", ["celery", "worker", "--concurrency=4"])
    celery_mod._cap_torch_threads_to_cpu_quota()
    assert calls == [2]

    calls.clear()
    monkeypatch.setenv("OMP_NUM_THREADS", "7")
    celery_mod._cap_torch_threads_to_cpu_quota()
    assert calls == []
