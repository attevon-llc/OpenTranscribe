"""``POST /asr-settings/local-model/restart`` and the loaded-model report (#1246 WP4).

The restart used to broadcast ``shutdown`` to EVERY worker in the deployment whenever no worker
name contained "gpu" - which would have killed a model download in flight, yt-dlp jobs,
redaction and embeddings. It must name only the workers that load the Whisper model, and say so
when none answered. Celery's control is replaced with a recording double: this is the boundary,
and the assertions are on what was SENT.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from app.core.constants import WHISPER_WORKER_PREFIXES
from app.services.local_models import worker_report

RESTART = "/api/asr-settings/local-model/restart"
ACTIVE = "/api/asr-settings/local-model/active"

pytestmark = [pytest.mark.usefixtures("pipeline_env")]


class RecordingControl:
    """``celery_app.control`` as far as the restart endpoint uses it."""

    def __init__(self, nodes: list[str], running: dict[str, list] | None = None):
        self._nodes = nodes
        self._running = running or {}
        self.sent: list[tuple[str, list[str] | None]] = []

    def inspect(self, timeout=None):
        return self

    def ping(self):
        return {name: {"ok": "pong"} for name in self._nodes}

    def active(self):
        return {name: self._running.get(name, []) for name in self._nodes}

    def broadcast(self, command, destination=None, **kwargs):
        self.sent.append((command, destination))


@pytest.fixture
def control(monkeypatch):
    from app.core.celery import celery_app

    def install(nodes, running=None) -> RecordingControl:
        double = RecordingControl(nodes, running)
        monkeypatch.setattr(celery_app, "control", double)
        return double

    return install


class TestRestartTargetsOnlyWhisperWorkers:
    def test_no_worker_answering_is_a_409_and_nothing_is_signalled(
        self, client, super_admin_token_headers, control
    ):
        double = control([])

        r = client.post(RESTART, headers=super_admin_token_headers)

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "gpu_worker_not_found"
        assert double.sent == []

    def test_only_other_workers_answering_is_the_same_409(
        self, client, super_admin_token_headers, control
    ):
        # `gpu-diarize@` contains "gpu" but loads no Whisper model: the old name test took it.
        double = control(
            ["gpu-diarize@d1", "media-downloader@m1", "cpu-processor@c1", "redaction@r1"]
        )

        r = client.post(RESTART, headers=super_admin_token_headers)

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "gpu_worker_not_found"
        assert double.sent == []

    def test_the_shutdown_names_exactly_the_whisper_workers(
        self, client, super_admin_token_headers, control
    ):
        double = control(
            [
                "gpu-transcription@a",
                "gpu-transcribe@c",
                "gpu-scaled@b",
                "gpu-diarize@a",
                "media-downloader@a",
                "cpu-processor@a",
            ]
        )

        r = client.post(RESTART, headers=super_admin_token_headers)

        assert r.status_code == 200
        expected = ["gpu-transcription@a", "gpu-transcribe@c", "gpu-scaled@b"]
        assert double.sent == [("shutdown", expected)]
        assert r.json()["workers"] == expected

    def test_the_active_task_count_covers_only_the_restarted_workers(
        self, client, super_admin_token_headers, control
    ):
        control(
            ["gpu-transcription@a", "media-downloader@a"],
            running={"gpu-transcription@a": [{"id": "x"}, {"id": "y"}], "media-downloader@a": [{}]},
        )

        r = client.post(RESTART, headers=super_admin_token_headers)

        assert r.json()["active_tasks"] == 2

    def test_the_response_says_when_the_restart_was_requested(
        self, client, super_admin_token_headers, control
    ):
        control(["gpu-transcription@a"])
        before = datetime.now(UTC)

        body = client.post(RESTART, headers=super_admin_token_headers).json()

        stamp = datetime.fromisoformat(body["restart_requested_at"])
        assert before <= stamp <= datetime.now(UTC)
        assert body["status"] == "restart_signaled"
        assert "model" in body

    def test_a_plain_admin_and_a_user_signal_nothing(
        self, client, admin_token_headers, user_token_headers, control
    ):
        double = control(["gpu-transcription@a"])

        assert client.post(RESTART, headers=admin_token_headers).status_code == 403
        assert client.post(RESTART, headers=user_token_headers).status_code == 403
        assert double.sent == []

    def test_every_prefix_is_a_real_compose_hostname_and_diarize_is_not_one(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        compose = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("docker-compose*.yml"))

        for prefix in WHISPER_WORKER_PREFIXES:
            assert f"--hostname {prefix}%h" in compose or f"-n {prefix}%h" in compose, prefix
        assert not any(p.startswith("gpu-diarize") for p in WHISPER_WORKER_PREFIXES)


class TestActiveReportsWhatWorkersLoaded:
    @pytest.fixture
    def redis_for_reports(self, local_model_redis, monkeypatch):
        for key in local_model_redis.scan_iter(match=f"{worker_report.KEY_PREFIX}*"):
            local_model_redis.delete(key)
        monkeypatch.setattr("app.core.redis.get_probe_redis", lambda: local_model_redis)
        return local_model_redis

    def test_loaded_models_lists_each_reporting_worker(
        self, client, user_token_headers, redis_for_reports
    ):
        worker_report.report_loaded_model("gpu-transcription@a", "tiny", redis_for_reports)
        worker_report.report_loaded_model("gpu-scaled@b", "large-v3", redis_for_reports)

        r = client.get(ACTIVE, headers=user_token_headers)

        assert r.status_code == 200
        loaded = {m["worker"]: m["model"] for m in r.json()["loaded_models"]}
        assert loaded == {"gpu-transcription@a": "tiny", "gpu-scaled@b": "large-v3"}
        assert all(m["ready_at"] for m in r.json()["loaded_models"])
        assert "active_model" in r.json()  # the existing fields stay

    def test_an_unreachable_redis_is_an_empty_list_not_a_500(
        self, client, user_token_headers, monkeypatch
    ):
        def boom():
            raise ConnectionError("redis is down")

        monkeypatch.setattr("app.core.redis.get_probe_redis", boom)

        r = client.get(ACTIVE, headers=user_token_headers)

        assert r.status_code == 200
        assert r.json()["loaded_models"] == []
