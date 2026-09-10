"""No task may be routed to a queue that lite mode leaves with zero consumers (#865).

``docker-compose.lite.yml`` scales ``celery-worker`` and
``celery-worker-gpu-scaled`` — the only two services that bind ``-Q gpu`` — to
``replicas: 0``. Eight speaker/diarization tasks were nonetheless pinned to
``gpu`` in ``task_routes``, so in lite every one of them was published into a
queue nothing drains. Celery raises nothing for that: the message is accepted by
the broker and simply waits. The API has already returned 200, so the UI shows a
speaker reassignment as successful while the cross-file voiceprint update never
happens.

This test derives the answer from the compose files rather than restating it,
because the failure mode is a **disagreement between two files** — the routing
table and the deployment overlay — and a hand-maintained list of "queues lite
runs" would be exactly the thing that goes stale and stops noticing.

It is deliberately compose-parsing rather than a live probe: it must fail in CI
and in the fast unit suite, where no stack is up.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.celery import GPU_PREFERRED_TASKS
from app.core.constants import CeleryQueues
from app.core.constants import gpu_preferred_queue

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_LITE_COMPOSE = _REPO_ROOT / "docker-compose.lite.yml"

# A route may legitimately name a queue lite does not staff ONLY when the task
# cannot run in lite at all AND lite refuses it somewhere the caller can see.
# Each entry needs that second half written down.
_EXEMPT_FROM_LITE_CONSUMER_CHECK = {
    "transcription.process_file": (
        "Local WhisperX transcription. The lite image ships no local ASR and "
        "services/asr/factory.py raises a named error for DEPLOYMENT_MODE=lite "
        "before anything is dispatched, so this route is unreachable there. "
        "Rerouting it to 'cpu' would replace that clear refusal with a different "
        "failure on a worker that also cannot run it."
    ),
}


def _services(path: Path) -> dict[str, dict[str, Any]]:
    parsed = yaml.safe_load(path.read_text()) or {}
    return {name: (svc or {}) for name, svc in (parsed.get("services") or {}).items()}


def _queues_bound_by(command: Any) -> list[str]:
    """Queue names from a worker's ``-Q a,b,c`` argument; [] for a non-worker."""
    if not isinstance(command, str):
        return []
    match = re.search(r"-Q\s+([\w,\-]+)", command)
    return match.group(1).split(",") if match else []


def _queues_with_a_consumer_in_lite() -> set[str]:
    """Every queue a DEFAULT lite deployment actually runs a worker for.

    Excludes services the lite overlay scales to ``replicas: 0`` and services
    behind a Compose ``profiles:`` gate, which ``docker compose up`` does not
    start unless the profile is requested.
    """
    base = _services(_BASE_COMPOSE)
    lite = _services(_LITE_COMPOSE)

    zeroed = {name for name, svc in lite.items() if (svc.get("deploy") or {}).get("replicas") == 0}

    consumed: set[str] = set()
    for name, svc in base.items():
        if name in zeroed or svc.get("profiles"):
            continue
        consumed.update(_queues_bound_by(svc.get("command")))
    return consumed


def _routed_queues_in_lite() -> dict[str, str]:
    """``task name -> queue`` as a LITE deployment would resolve it.

    ``task_routes`` is a static dict built at import time from the ambient
    ``DEPLOYMENT_MODE`` (``full`` under pytest), so the lite resolution is
    reconstructed by substituting the gpu-preferred family's queue rather than
    re-importing ``app.core.celery`` under a patched environment — which would
    have to survive xdist and module caching to mean anything.
    """
    from app.core.celery import celery_app

    lite_gpu_preferred = gpu_preferred_queue("lite")
    routed: dict[str, str] = {}
    for name, route in celery_app.conf.task_routes.items():
        queue = route["queue"]
        routed[name] = lite_gpu_preferred if name in GPU_PREFERRED_TASKS else queue
    return routed


def test_the_compose_parsing_actually_found_workers():
    """Guard the guard: an empty parse would make every assertion below vacuous."""
    consumed = _queues_with_a_consumer_in_lite()

    assert CeleryQueues.CPU in consumed
    assert CeleryQueues.NLP in consumed
    assert CeleryQueues.EMBEDDING in consumed
    assert len(consumed) >= 6, consumed


def test_lite_mode_leaves_the_gpu_queue_unstaffed():
    """The premise. If lite ever staffs 'gpu', this whole file needs rethinking."""
    assert CeleryQueues.GPU not in _queues_with_a_consumer_in_lite(), (
        "docker-compose.lite.yml now runs a 'gpu' consumer — the reroute in "
        "gpu_preferred_queue() may no longer be needed"
    )


def test_no_task_is_routed_to_a_queue_lite_cannot_drain():
    """The regression this file exists for.

    Watched red against ``git archive HEAD``: eight tasks — rediarize,
    update_speaker_embedding_on_reassignment, extract_v4_embeddings(_batch),
    speaker.recluster_all, detect_speaker_attributes_batch,
    analyze_speakers_combined_batch and
    speaker_embedding_consistency_repair_batch — resolved to 'gpu' in lite.
    """
    consumed = _queues_with_a_consumer_in_lite()

    black_holed = {
        name: queue
        for name, queue in _routed_queues_in_lite().items()
        if queue not in consumed and name not in _EXEMPT_FROM_LITE_CONSUMER_CHECK
    }

    assert not black_holed, (
        "these tasks are published into a queue no lite worker consumes, so they "
        f"wait forever with no error: {black_holed}"
    )


@pytest.mark.parametrize("task_name", GPU_PREFERRED_TASKS)
def test_every_gpu_preferred_task_is_actually_routed(task_name):
    """The family list must name real routes, or the reroute silently covers nothing."""
    from app.core.celery import celery_app

    assert task_name in celery_app.conf.task_routes, (
        f"{task_name} is in GPU_PREFERRED_TASKS but has no route — it would fall "
        "through to the default 'celery' queue"
    )


def test_a_full_deployment_still_sends_the_family_to_the_gpu():
    """Control: the reroute is mode-dependent, not a blanket move off the GPU.

    Without this, the test above would also pass if every speaker task had simply
    been pinned to 'cpu' everywhere — which would quietly cost full deployments
    their GPU acceleration.
    """
    from app.core.celery import celery_app

    assert gpu_preferred_queue("full") == CeleryQueues.GPU
    # Anything that is not "lite" keeps the GPU, including an unknown mode.
    assert gpu_preferred_queue("FULL") == CeleryQueues.GPU
    assert gpu_preferred_queue("something-else") == CeleryQueues.GPU
    for name in GPU_PREFERRED_TASKS:
        assert celery_app.conf.task_routes[name]["queue"] == CeleryQueues.GPU, (
            f"{name} left the GPU queue in a full deployment"
        )


def test_the_lite_resolution_is_the_cpu_queue():
    """...and in lite the same family lands on a queue lite genuinely staffs."""
    assert gpu_preferred_queue("lite") == CeleryQueues.CPU
    assert gpu_preferred_queue("  Lite  ") == CeleryQueues.CPU
    assert CeleryQueues.CPU in _queues_with_a_consumer_in_lite()


def test_the_real_import_time_routing_under_lite_matches_the_reconstruction(
    run_in_clean_process,
):
    """Prove the substitution above models what a lite process ACTUALLY builds.

    ``_routed_queues_in_lite`` reconstructs the lite table rather than re-importing
    under a patched environment; that is only sound while the reconstruction and
    the real import-time resolution agree. A child process with
    ``DEPLOYMENT_MODE=lite`` builds the genuine article, so the two are compared
    instead of trusted — the one thing a same-process test cannot do, because
    ``task_routes`` is frozen at first import and pytest has already imported it.
    """
    observed = run_in_clean_process(
        "from app.core.celery import GPU_PREFERRED_TASKS, celery_app\n"
        "routes = celery_app.conf.task_routes\n"
        "print(sorted({routes[n]['queue'] for n in GPU_PREFERRED_TASKS}))\n",
        DEPLOYMENT_MODE="lite",
    )

    assert observed == f"['{CeleryQueues.CPU}']", observed


def test_an_exemption_must_carry_a_written_reason():
    """An exemption is a claim about why a dead route is acceptable, not a mute."""
    for task_name, reason in _EXEMPT_FROM_LITE_CONSUMER_CHECK.items():
        assert len(reason) > 60, f"{task_name}'s exemption needs a real reason"

    from app.core.celery import celery_app

    stale = [t for t in _EXEMPT_FROM_LITE_CONSUMER_CHECK if t not in celery_app.conf.task_routes]
    assert not stale, f"exemption(s) for task(s) that no longer have a route: {stale}"
