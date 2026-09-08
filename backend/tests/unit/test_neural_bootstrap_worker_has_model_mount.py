"""The neural-search self-heal must run somewhere that can actually reach ``/ml-models``.

``app/main.py``'s startup bootstrap says, in as many words, that it is *"no longer the only
attempt: a cold or slow OpenSearch boot that outlasts this one shot self-heals on the next
beat tick instead of losing neural search permanently."*

That promise was false. ``neural_search_bootstrap`` is routed to the **utility** queue, which
only ``celery-cpu-worker`` consumes — and ``docker-compose.yml`` mounts
``${MODEL_CACHE_DIR}/opensearch-ml`` at ``/ml-models`` on exactly two services, ``opensearch``
(read-only) and ``backend``. The worker has no such path at all.

Measured 2026-09-08, lite rehearsal (Scenario C, ``1/17 assertions FAILED``)::

    INFO  neural_bootstrap: Internet available - downloading default model: all-MiniLM-L6-v2
    ERROR neural_bootstrap: Error initializing neural search:
          [Errno 13] Permission denied: '/ml-models'
    WARNING Neural search bootstrap still degraded after 2 attempt(s) (retrying in 1200s)

``docker exec opentranscribe-celery-cpu-worker ls /ml-models`` → *No such file or directory*;
the ``Permission denied`` is the non-root ``appuser`` failing to create ``/ml-models`` at ``/``.

⚠️ **This hides in the happy path.** Scenario A (fresh install) passes the very same
assertion, because there the backend's startup one-shot lands and registers the model. The
defect only surfaces when that one-shot misses — and precisely then, the mechanism designed
to rescue it cannot run. A self-heal that cannot heal is worse than none: the deployment
silently falls back to BM25 and the retry loop reports "degraded, retrying in 1200s" forever.

The invariant is derived, not transcribed: this reads the task's queue out of
``app/core/celery.py`` and the consuming services out of the compose ``command``, so routing
the task to a different queue moves the requirement with it instead of silently voiding it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yml"
CELERY = REPO_ROOT / "backend" / "app" / "core" / "celery.py"

pytestmark = pytest.mark.skipif(
    not COMPOSE.is_file() or not CELERY.is_file(),
    reason="docker-compose.yml or app/core/celery.py is not present in this checkout",
)

MOUNT_PATH = "/ml-models"
TASK_NAME = "neural_search_bootstrap"


def _bootstrap_queue() -> str:
    """The queue ``neural_search_bootstrap`` is actually scheduled onto.

    Read from the beat entry's ``options``, which spells the queue as a plain string, rather
    than from ``task_routes`` (which uses the ``CeleryQueues`` enum and would need importing
    the app just to resolve a constant).
    """
    text = CELERY.read_text(encoding="utf-8")
    idx = text.find(f'"task": "{TASK_NAME}"')
    assert idx != -1, f"the {TASK_NAME} beat entry has moved or been renamed; re-point this"
    window = text[idx : idx + 800]
    match = re.search(r'"queue":\s*"([a-z-]+)"', window)
    assert match, f"could not read the queue from the {TASK_NAME} beat entry:\n{window[:400]}"
    return match.group(1)


def _services_consuming(queue: str) -> list[str]:
    """Compose services whose celery ``command`` includes ``-Q ...<queue>...``."""
    text = COMPOSE.read_text(encoding="utf-8")
    services: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        top = re.match(r"^  ([a-z0-9][a-z0-9_-]*):\s*$", line)
        if top:
            current = top.group(1)
            continue
        if current and "celery" in line and " -Q " in line:
            queues = re.search(r"-Q\s+([a-z0-9,_-]+)", line)
            if queues and queue in queues.group(1).split(","):
                services.append(current)
    return services


def _services_mounting(path: str) -> set[str]:
    text = COMPOSE.read_text(encoding="utf-8")
    out: set[str] = set()
    current: str | None = None
    for line in text.splitlines():
        top = re.match(r"^  ([a-z0-9][a-z0-9_-]*):\s*$", line)
        if top:
            current = top.group(1)
            continue
        if current and re.search(rf":{re.escape(path)}(:ro)?\s*$", line.strip()):
            out.add(current)
    return out


def test_the_derivation_finds_something():
    """Guard the guard: an empty derivation would make every assertion below vacuous."""
    queue = _bootstrap_queue()
    assert queue, "no queue derived"
    assert _services_consuming(queue), (
        f"no compose service consumes the '{queue}' queue — either the parser broke or the "
        "task is scheduled onto a queue nothing runs, which is its own bug"
    )
    assert _services_mounting(MOUNT_PATH), (
        f"no compose service mounts {MOUNT_PATH} — the parser is not matching the volume "
        "lines, so the check below could not fail"
    )


def test_every_worker_that_runs_the_bootstrap_can_reach_the_model_dir():
    queue = _bootstrap_queue()
    consumers = _services_consuming(queue)
    mounted = _services_mounting(MOUNT_PATH)
    missing = sorted(set(consumers) - mounted)

    assert not missing, (
        f"these services consume the '{queue}' queue, so they can run {TASK_NAME}, but do "
        f"not mount {MOUNT_PATH}: {missing}.\n"
        "The bootstrap writes the embedding model there; without the mount it fails with "
        "[Errno 13] Permission denied and neural search degrades to BM25 permanently — the "
        "self-heal main.py promises 'instead of losing neural search permanently' cannot "
        "run. Measured 2026-09-08: this failed the lite rehearsal 1/17."
    )


def test_the_backend_startup_path_also_has_it():
    """The one-shot must keep working too — this fix must not be a swap."""
    assert "backend" in _services_mounting(MOUNT_PATH), (
        "the backend no longer mounts /ml-models, so the startup one-shot (the path that "
        "currently makes fresh installs pass) would break"
    )
