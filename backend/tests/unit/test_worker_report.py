"""The loaded-model report a Whisper worker writes after pinning its model (#1246 WP4).

Real Redis for the round trip; the worker's ``preload_models`` hook is driven with every heavy
dependency stubbed, so what is asserted is the ORDER (pin first, then report) and the node name
the report is filed under.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime

import pytest

from app.core import celery as celery_module
from app.services.local_models import worker_report

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


@pytest.fixture
def client(local_model_redis):
    for key in local_model_redis.scan_iter(match=f"{worker_report.KEY_PREFIX}*"):
        local_model_redis.delete(key)
    return local_model_redis


def test_a_report_round_trips_with_its_node_name_and_a_timestamp(client):
    worker_report.report_loaded_model("gpu-transcription@abc", "large-v3-turbo", client)

    reports = worker_report.read_loaded_models(client)

    assert len(reports) == 1
    assert reports[0]["worker"] == "gpu-transcription@abc"
    assert reports[0]["model"] == "large-v3-turbo"
    assert datetime.fromisoformat(reports[0]["ready_at"]).tzinfo is not None


def test_a_restarted_worker_replaces_its_own_report(client):
    worker_report.report_loaded_model("gpu-transcription@abc", "tiny", client)
    first = worker_report.read_loaded_models(client)[0]

    worker_report.report_loaded_model("gpu-transcription@abc", "base", client)

    now = worker_report.read_loaded_models(client)
    assert [(r["worker"], r["model"]) for r in now] == [("gpu-transcription@abc", "base")]
    assert now[0]["ready_at"] >= first["ready_at"]


def test_a_report_expires_so_a_recreated_container_does_not_linger_forever(client):
    worker_report.report_loaded_model("gpu-transcription@abc", "tiny", client)

    ttl = client.ttl(f"{worker_report.KEY_PREFIX}gpu-transcription@abc")

    assert 0 < ttl <= worker_report.REPORT_TTL_SECONDS


def test_an_unreadable_record_is_skipped_not_fatal(client):
    client.set(f"{worker_report.KEY_PREFIX}broken@x", "not json")
    client.set(f"{worker_report.KEY_PREFIX}partial@x", json.dumps({"model": "tiny"}))
    worker_report.report_loaded_model("gpu-scaled@ok", "small", client)

    reports = worker_report.read_loaded_models(client)

    assert [r["worker"] for r in reports] == ["gpu-scaled@ok"]


class _DownRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    def scan_iter(self, *args, **kwargs):
        raise ConnectionError("redis is down")


def test_a_dead_redis_never_stops_a_worker_starting_or_a_page_loading():
    worker_report.report_loaded_model("gpu-transcription@abc", "tiny", _DownRedis())

    assert worker_report.read_loaded_models(_DownRedis()) == []


class TestPreloadReportsAfterPinning:
    @staticmethod
    def _stub_heavy_dependencies(monkeypatch) -> None:
        """Make the GPU branch of ``preload_models`` runnable with no GPU, torch or models."""
        torch = types.ModuleType("torch")
        torch.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
            cuda=types.SimpleNamespace(matmul=types.SimpleNamespace()),
            cudnn=types.SimpleNamespace(),
        )
        monkeypatch.setitem(sys.modules, "torch", torch)

        import app.transcription as transcription_pkg

        manager = types.ModuleType("app.transcription.model_manager")
        manager.ModelManager = types.SimpleNamespace(  # type: ignore[attr-defined]
            get_instance=lambda: types.SimpleNamespace(ensure_models_loaded=lambda cfg: None)
        )
        budget = types.ModuleType("app.transcription.vram_budget")
        budget.configure_from_device = lambda index: None  # type: ignore[attr-defined]
        for name, module in (("model_manager", manager), ("vram_budget", budget)):
            monkeypatch.setitem(sys.modules, f"app.transcription.{name}", module)
            monkeypatch.setattr(transcription_pkg, name, module, raising=False)
        monkeypatch.setattr(celery_module, "reconcile_gpu_concurrent_requests", lambda: None)
        monkeypatch.setattr(celery_module, "configure_gpu_host_admission", lambda: None)

    @pytest.fixture
    def events(self, monkeypatch):
        from app.transcription.config import TranscriptionConfig

        log: list[tuple] = []
        config = types.SimpleNamespace(
            device="cuda", device_index=0, model_name="tiny", concurrent_requests=1
        )
        monkeypatch.setenv("PRELOAD_GPU_MODELS", "true")
        monkeypatch.delenv("PRELOAD_CPU_WHISPER", raising=False)
        monkeypatch.delenv("PRELOAD_REDACTION_MODELS", raising=False)
        self._stub_heavy_dependencies(monkeypatch)
        monkeypatch.setattr(TranscriptionConfig, "from_environment", staticmethod(lambda: config))
        monkeypatch.setattr(
            TranscriptionConfig, "pin_model", staticmethod(lambda name: log.append(("pin", name)))
        )
        monkeypatch.setattr(
            worker_report,
            "report_loaded_model",
            lambda host, model: log.append(("report", host, model)),
        )
        return log

    def test_the_model_is_reported_under_the_node_name_once_it_is_pinned(self, events):
        sender = types.SimpleNamespace(hostname="gpu-transcription@host1")

        celery_module.preload_models(sender=sender)

        assert events == [("pin", "tiny"), ("report", "gpu-transcription@host1", "tiny")]

    def test_a_worker_that_never_pinned_a_model_reports_nothing(self, events, monkeypatch):
        monkeypatch.delenv("PRELOAD_GPU_MODELS")

        celery_module.preload_models(sender=types.SimpleNamespace(hostname="cpu-processor@c"))

        assert events == []
