"""Couple the GPU worker's thread count to its engine slots (issue #1072).

Celery ``--pool=threads --concurrency=N`` decides how many transcriptions run at once;
``GPU_CONCURRENT_REQUESTS`` becomes CTranslate2's ``num_workers`` and switches the stage logic
between single-request and concurrent behaviour. Set independently, N threads share ONE
CTranslate2 worker, so Whisper is serialised while every thread believes it is alone on the
card. At ``worker_ready`` a GPU threads worker now defaults the engine slots to its pool size,
and warns once when both are set and disagree.
"""

from __future__ import annotations

import logging
import sys

import pytest

from app.core import celery as celery_module


@pytest.fixture(autouse=True)
def _gpu_worker(monkeypatch):
    monkeypatch.setenv("PRELOAD_GPU_MODELS", "true")
    monkeypatch.delenv("GPU_CONCURRENT_REQUESTS", raising=False)
    monkeypatch.delenv("GPU_WORKER_POOL", raising=False)
    monkeypatch.delenv("CELERY_WORKER_CONCURRENCY", raising=False)


def _argv(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["celery", "-A", "app.core.celery", "worker", "-Q", "gpu", *args],
    )


def test_unset_engine_slots_default_to_the_thread_count(monkeypatch):
    import os

    _argv(monkeypatch, "--pool=threads", "--concurrency=4")
    celery_module.reconcile_gpu_concurrent_requests()
    assert os.environ["GPU_CONCURRENT_REQUESTS"] == "4"


def test_the_short_flag_spelling_is_understood(monkeypatch):
    import os

    _argv(monkeypatch, "-P", "threads", "-c", "3")
    celery_module.reconcile_gpu_concurrent_requests()
    assert os.environ["GPU_CONCURRENT_REQUESTS"] == "3"


def test_the_result_feeds_the_transcription_config(monkeypatch):
    from app.transcription.config import TranscriptionConfig

    _argv(monkeypatch, "--pool=threads", "--concurrency=4")
    celery_module.reconcile_gpu_concurrent_requests()
    assert TranscriptionConfig._resolve_concurrent_requests() == 4


def test_a_mismatch_warns_once_naming_both_values(monkeypatch, caplog):
    import os

    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "1")
    _argv(monkeypatch, "--pool=threads", "--concurrency=4")
    with caplog.at_level(logging.WARNING, logger="app.core.celery"):
        celery_module.reconcile_gpu_concurrent_requests()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "GPU_CONCURRENT_REQUESTS=1" in warnings[0].getMessage()
    assert "concurrency=4" in warnings[0].getMessage()
    # An explicit operator value is respected, not overwritten.
    assert os.environ["GPU_CONCURRENT_REQUESTS"] == "1"


def test_matching_values_are_silent(monkeypatch, caplog):
    monkeypatch.setenv("GPU_CONCURRENT_REQUESTS", "4")
    _argv(monkeypatch, "--pool=threads", "--concurrency=4")
    with caplog.at_level(logging.WARNING, logger="app.core.celery"):
        celery_module.reconcile_gpu_concurrent_requests()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_prefork_pool_is_left_alone(monkeypatch):
    """Prefork children each load their own models; engine slots are per process there."""
    import os

    _argv(monkeypatch, "--pool=prefork", "--concurrency=4")
    celery_module.reconcile_gpu_concurrent_requests()
    assert "GPU_CONCURRENT_REQUESTS" not in os.environ


def test_a_non_gpu_worker_is_left_alone(monkeypatch):
    import os

    monkeypatch.delenv("PRELOAD_GPU_MODELS")
    _argv(monkeypatch, "--pool=threads", "--concurrency=4")
    celery_module.reconcile_gpu_concurrent_requests()
    assert "GPU_CONCURRENT_REQUESTS" not in os.environ


def test_preload_reconciles_before_building_the_config(monkeypatch):
    """The ordering matters: the preload builds the TranscriptionConfig (and loads
    CTranslate2 with its num_workers) from the env, so the reconcile must run first."""
    seen: list[str | None] = []

    class _Config:
        device = "cpu"
        concurrent_requests = 0

    def _from_environment(**_kwargs):
        import os

        seen.append(os.environ.get("GPU_CONCURRENT_REQUESTS"))
        return _Config()

    from app.transcription.config import TranscriptionConfig

    monkeypatch.setattr(TranscriptionConfig, "from_environment", staticmethod(_from_environment))
    monkeypatch.delenv("PRELOAD_CPU_WHISPER", raising=False)
    monkeypatch.delenv("PRELOAD_REDACTION_MODELS", raising=False)
    _argv(monkeypatch, "--pool=threads", "--concurrency=4")
    celery_module.preload_models()
    assert seen == ["4"]
