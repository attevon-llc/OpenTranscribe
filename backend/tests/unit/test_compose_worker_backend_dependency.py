"""Which workers may start before the backend is healthy (EFFC B1).

Compose ``depends_on`` with ``condition: service_completed_successfully`` on ``migrate`` is what
guarantees the schema; ``service_healthy`` on ``backend`` additionally waits for the backend
lifespan, which exports the diar-native ONNX/PLDA set (``app/main.py``). Workers that can run
diarization or voiceprint work (every GPU worker and the cpu worker, whose queues carry the
speaker/diarization family: ``core/celery_routing.py``) and beat keep that wait. See
https://docs.docker.com/reference/compose-file/services/#depends_on and
https://docs.docker.com/compose/how-tos/startup-order/ .
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yml"

# Never read the diar-native export at boot: queues download, redaction, cloud-asr, nlp,
# embedding carry no diarization or voiceprint tasks.
NO_BACKEND_WAIT = {
    "celery-download-worker",
    "celery-redaction",
    "celery-cloud-asr-worker",
    "celery-nlp-worker",
    "celery-embedding-worker",
}
KEEP_BACKEND_WAIT = {
    "celery-worker",
    "celery-cpu-worker",
    "celery-beat",
    "celery-worker-gpu-transcribe",
    "celery-worker-gpu-diarize",
    "celery-worker-gpu-scaled",
}


def _services() -> dict:
    services: dict = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    return services


def _dep(svc: dict, name: str) -> str | None:
    dep = (svc.get("depends_on") or {}).get(name)
    return dep.get("condition") if isinstance(dep, dict) else None


@pytest.mark.parametrize("name", sorted(NO_BACKEND_WAIT | KEEP_BACKEND_WAIT))
def test_every_worker_keeps_migrate_and_redis(name):
    svc = _services()[name]
    assert _dep(svc, "migrate") == "service_completed_successfully"
    if name != "celery-beat":
        assert _dep(svc, "redis") == "service_healthy"


@pytest.mark.parametrize("name", sorted(NO_BACKEND_WAIT))
def test_non_diarizing_workers_do_not_wait_for_backend(name):
    assert _dep(_services()[name], "backend") is None


@pytest.mark.parametrize("name", sorted(KEEP_BACKEND_WAIT))
def test_diarization_capable_workers_keep_waiting(name):
    assert _dep(_services()[name], "backend") == "service_healthy"
