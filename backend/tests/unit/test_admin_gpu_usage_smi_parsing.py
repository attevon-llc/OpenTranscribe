"""The admin dashboard's direct nvidia-smi read tolerates the same readings as the task.

``get_gpu_usage`` falls back to querying nvidia-smi itself when Redis holds no stats. A
fractional utilization ("37.5") or an absent field ("[N/A]") used to make that read return
nothing, so the dashboard sat on "Loading GPU stats..." for a healthy board. Only fixture
strings are parsed here; nvidia-smi is never run.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.api.endpoints import admin_system_metrics


class _FakeRedis:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    def get(self, _key: str) -> None:
        return None

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.stored[key] = value


@pytest.fixture
def smi_stdout(monkeypatch):
    """Return a setter that makes the fake nvidia-smi print the given row."""
    from app.core import celery as celery_module

    fake = _FakeRedis()
    monkeypatch.setattr(
        celery_module, "celery_app", SimpleNamespace(backend=SimpleNamespace(client=fake))
    )

    def _set(stdout: str) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_a, **_kw: SimpleNamespace(stdout=stdout, returncode=0),
        )

    return _set


def test_fractional_utilization_and_temperature_keep_the_gpu(smi_stdout):
    smi_stdout("NVIDIA RTX A6000, 4096, 49140, 45044, 37.5, 61.4\n")

    stats = admin_system_metrics.get_gpu_usage()

    assert len(stats) == 1
    assert stats[0]["available"] is True
    assert stats[0]["name"] == "NVIDIA RTX A6000"
    assert stats[0]["utilization_percent"] == "37%"
    assert stats[0]["temperature_celsius"] == 61


def test_not_available_memory_free_is_derived_not_fatal(smi_stdout):
    smi_stdout("NVIDIA RTX A6000, 4096, 49140, [N/A], 37, 61\n")

    stats = admin_system_metrics.get_gpu_usage()

    assert stats[0]["available"] is True
    assert stats[0]["memory_free"] == "43.99 GB"
