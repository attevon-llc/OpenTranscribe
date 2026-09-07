"""Every celery worker that can serve the diarization queues must be wired to
the diar-native sidecar overlay, or explicitly exempt (issue #655).

``docker-compose.diar-native.yml`` patches specific services with
``DIAR_NATIVE_URL``/``DIAR_NATIVE_SHARED_DIR`` (the ``diar/`` namespace of the
``pipeline_scratch`` volume as of issue #661 E2 — previously a dedicated
``diar-native-tmp`` handoff volume). That list is a hand-maintained dispatch in a YAML
overlay, and
issue #655 found it silently missing three of the four workers that can run a
GPU diarization task: under ``--gpu-scale`` the only patched worker
(``celery-worker``) is scaled to 0 by default, so all GPU work actually runs
on the unpatched ``celery-worker-gpu-scaled``; under ``--gpu-split`` the
diarizing service is ``celery-worker-gpu-diarize`` (the overlay's own comment
named a service, ``celery-worker-diarize``, that does not exist in any compose
file). Because ``diarizer_native.py`` degrades to in-process PyAnnote on a
failed handoff with only a warning log line, none of this raised an error —
PyAnnote silently became the de-facto engine on those workers.

Nothing enumerated the services that can run the diarizing queues against the
services the overlay actually patches. This does, in the same shape as
``test_opentr_fresh_aux_isolation.py``.

Static, over the compose YAML: starting every GPU topology to find out is not
a test anyone would run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DIAR_NATIVE_OVERLAY = REPO_ROOT / "docker-compose.diar-native.yml"

#: Compose files whose services are scanned for a celery worker command that
#: consumes a GPU/diarization queue. Overlays that only patch an *existing*
#: service's image/build (e.g. docker-compose.blackwell.yml) do not introduce
#: a new service name and are deliberately not in this list — see EXEMPT below
#: for why blackwell specifically needs no sidecar entry of its own.
SCANNED_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.gpu-scale.yml",
    "docker-compose.gpu-split.yml",
]

#: A celery `-Q` queue name counts as diarization-capable if a diarize task can
#: land on it. `gpu` and `gpu-transcribe` both run the full (transcribe+diarize)
#: pipeline when gpu-split is not engaged; `gpu-diarize` is the dedicated split
#: queue.
DIARIZE_CAPABLE_QUEUES = {"gpu", "gpu-transcribe", "gpu-diarize"}

QUEUE_FLAG_RE = re.compile(r"-Q\s+(\S+)")

#: Services deliberately NOT wired to the diar-native overlay, each with the
#: reason. An entry here is a decision, not a backlog item.
EXEMPT: dict[str, str] = {
    "celery-worker-blackwell": (
        "docker-compose.blackwell.yml does not define a new service named "
        "celery-worker-blackwell — it overrides celery-worker's image/build "
        "in place, so celery-worker's own diar-native patch already covers it. "
        "opentr.sh separately propagates DIAR_NATIVE_IMAGE so the sidecar "
        "matches the blackwell tag instead of :latest."
    ),
}


def _load_compose(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        # yaml.safe_load is untyped and returns Any; naming the type here keeps every
        # downstream key access checked instead of widening the whole caller to Any.
        loaded: dict = yaml.safe_load(f)
    return loaded


def _diarize_capable_services() -> set[str]:
    """Every service across the scanned compose files whose `command` consumes
    a diarize-capable queue."""
    services: set[str] = set()
    for rel in SCANNED_COMPOSE_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"expected compose file not found: {path}"
        doc = _load_compose(path)
        for name, spec in (doc.get("services") or {}).items():
            command = spec.get("command")
            if not isinstance(command, str):
                continue
            match = QUEUE_FLAG_RE.search(command)
            if not match:
                continue
            queues = set(match.group(1).split(","))
            if queues & DIARIZE_CAPABLE_QUEUES:
                services.add(name)
    return services


def _sidecar_patched_services() -> set[str]:
    """Services docker-compose.diar-native.yml actually patches with the
    DIAR_NATIVE_URL handoff (excluding the diar-native service itself)."""
    assert DIAR_NATIVE_OVERLAY.is_file(), f"overlay not found: {DIAR_NATIVE_OVERLAY}"
    doc = _load_compose(DIAR_NATIVE_OVERLAY)
    patched = set()
    for name, spec in (doc.get("services") or {}).items():
        if name == "diar-native":
            continue
        env = spec.get("environment") or []
        env_text = "\n".join(env) if isinstance(env, list) else str(env)
        if "DIAR_NATIVE_URL" in env_text:
            patched.add(name)
    return patched


def test_every_diarize_capable_worker_is_wired_or_exempt():
    diarize_capable = _diarize_capable_services()
    patched = _sidecar_patched_services()

    assert diarize_capable, (
        "no diarize-capable celery worker was found at all — the queue-name "
        "regex or SCANNED_COMPOSE_FILES list is stale, this must not silently "
        "pass with zero matches"
    )

    unwired = diarize_capable - patched - set(EXEMPT)
    assert not unwired, (
        f"these celery workers can run a diarize-capable queue "
        f"({sorted(unwired)}) but docker-compose.diar-native.yml does not "
        f"patch them with DIAR_NATIVE_URL and they are not in EXEMPT — a "
        f"native-configured stack would silently fall back to in-process "
        f"PyAnnote on them (issue #655)"
    )


def test_exempt_entries_still_correspond_to_real_or_absent_services():
    """An EXEMPT entry for a service that now exists for real (e.g. a future
    blackwell-specific worker) must be re-justified, not just carried forward."""
    diarize_capable = _diarize_capable_services()
    for exempt_name in EXEMPT:
        assert exempt_name not in diarize_capable, (
            f"'{exempt_name}' is listed in EXEMPT as not a real service, but "
            f"it now appears as a diarize-capable worker in the compose "
            f"files — remove the exemption and wire it, or update the reason"
        )


@pytest.mark.parametrize("name", sorted(EXEMPT))
def test_exempt_reason_is_non_trivial(name):
    reason = EXEMPT[name]
    assert len(reason) > 20, f"EXEMPT['{name}'] needs a real written reason"
