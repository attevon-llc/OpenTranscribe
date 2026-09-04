"""Issue #660: `celery-cpu-worker` must be an accounted-for consumer of the
diar-native sidecar, not silently absent from any wiring inventory.

`test_diar_native_overlay_wiring.py` (#655) enumerates workers that can run a
*diarization* queue against the services `docker-compose.diar-native.yml`
patches with ``DIAR_NATIVE_URL``. That inventory is deliberately not extended
here: speaker-EMBEDDING extraction (``extract_speaker_embeddings``, issue #571)
routes to ``CeleryQueues.CPU`` — NOT the `gpu`/`gpu-transcribe`/`gpu-diarize`
queues #655 tracks — and reaches the sidecar by a completely different
mechanism: the coded default URL in ``diarizer_native.py``
(``http://diar-native:8701``) plus ``env_file: .env``, resolved by Docker DNS.
No overlay ``environment:`` patch is involved, so a scan for `DIAR_NATIVE_URL`
in the overlay's YAml would (correctly) find nothing and (incorrectly) read as
"unwired".

This is the gist's misnamed service, corrected: `celery-embedding-worker`
(``-Q embedding``) is search-indexing (sentence-transformers), and has nothing
to do with speaker embeddings or the sidecar.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

#: A celery `-Q` queue name counts as speaker-embedding-capable if
#: `extract_speaker_embeddings` (`app/core/celery.py`) can land on it.
EMBEDDING_CAPABLE_QUEUES = {"cpu"}

QUEUE_FLAG_RE = re.compile(r"-Q\s+(\S+)")

#: Services reachable from the sidecar by DNS + coded default + env_file,
#: rather than an overlay environment patch — each with the reason it needs
#: no `docker-compose.diar-native.yml` entry of its own.
DNS_REACHABLE: dict[str, str] = {
    "celery-cpu-worker": (
        "extract_speaker_embeddings routes to CeleryQueues.CPU, served by "
        "celery-cpu-worker (`-Q cpu,utility,cpu-transcribe`). It reaches the "
        "sidecar via diarizer_native.py's coded default "
        "(http://diar-native:8701) plus `env_file: .env` — no overlay "
        "environment patch is needed or present. Under --lite this is the "
        "ONLY speaker-embedding-capable worker (issue #660); the gpu queue "
        "has zero consumers there (both GPU workers are replicas: 0)."
    ),
}


def _load_compose(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        loaded: dict = yaml.safe_load(f)
    return loaded


def _embedding_capable_services(compose_files: list[str]) -> set[str]:
    services: set[str] = set()
    for rel in compose_files:
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
            if queues & EMBEDDING_CAPABLE_QUEUES:
                services.add(name)
    return services


def test_every_speaker_embedding_capable_worker_is_listed_or_exempt():
    capable = _embedding_capable_services(["docker-compose.yml"])
    assert capable, (
        "no speaker-embedding-capable celery worker was found — the queue-name "
        "regex or the scanned compose file is stale; this must not silently "
        "pass with zero matches"
    )
    unlisted = capable - set(DNS_REACHABLE)
    assert not unlisted, (
        f"these celery workers can run extract_speaker_embeddings "
        f"({sorted(unlisted)}) but are not accounted for in DNS_REACHABLE — "
        f"under --lite this task has nowhere documented to reach the sidecar"
    )


def test_celery_cpu_worker_is_present_under_the_lite_overlay_too():
    """The service must still exist (re-imaged, not removed) once docker-compose.lite.yml
    is layered on — the DNS_REACHABLE claim is worthless if lite drops the service."""
    lite_doc = _load_compose(REPO_ROOT / "docker-compose.lite.yml")
    assert "celery-cpu-worker" in (lite_doc.get("services") or {}), (
        "docker-compose.lite.yml no longer re-images celery-cpu-worker — the "
        "worker that serves extract_speaker_embeddings under lite would be "
        "missing or running the full (non-lite) image"
    )
    cpu_worker_lite = lite_doc["services"]["celery-cpu-worker"]
    assert "-lite" in str(cpu_worker_lite.get("image", "")), (
        "celery-cpu-worker under docker-compose.lite.yml must resolve to the "
        "lite image, not the full backend image"
    )


def test_this_inventory_would_catch_a_removed_entry():
    """Guard the guard: an unlisted embedding-capable worker must fail the assertion."""
    capable = _embedding_capable_services(["docker-compose.yml"])
    reachable_minus_cpu_worker = set(DNS_REACHABLE) - {"celery-cpu-worker"}
    unlisted = capable - reachable_minus_cpu_worker
    assert "celery-cpu-worker" in unlisted, (
        "removing celery-cpu-worker from DNS_REACHABLE did not surface it as "
        "unlisted — the detector cannot see this class of regression"
    )


def test_the_embedding_path_needs_no_shared_volume_on_the_lite_worker():
    """Load-bearing (#660): unlike /diarize, /embed_window carries its audio base64
    in the request body (native_embedding_client.py). Do NOT mount diar-native-tmp
    or transcription-temp onto celery-cpu-worker under lite — that would imply lite
    diarizes, which asr/factory.py's cloud-ASR-only guard guarantees it does not."""
    overlay_doc = _load_compose(REPO_ROOT / "docker-compose.diar-native.yml")
    cpu_worker_spec = (overlay_doc.get("services") or {}).get("celery-cpu-worker")
    assert cpu_worker_spec is None, (
        "docker-compose.diar-native.yml now patches celery-cpu-worker directly — "
        "if that patch adds a shared volume mount, remove it: /embed_window needs "
        "no shared path, and mounting one would incorrectly imply lite diarizes"
    )

    base_doc = _load_compose(REPO_ROOT / "docker-compose.yml")
    lite_doc = _load_compose(REPO_ROOT / "docker-compose.lite.yml")
    lite_cpu_worker = (lite_doc.get("services") or {}).get("celery-cpu-worker") or {}

    # docker-compose.lite.yml does not declare a `volumes:` key for celery-cpu-worker
    # at all, so under compose's file-merge semantics the BASE service's `volumes:`
    # list stays in effect once `--lite` is layered on. Checking lite's own (absent)
    # `volumes:` key — as this test used to — compares against "", which makes the
    # forbidden-mount assertion below pass vacuously regardless of what's mounted.
    # Guard that assumption explicitly, then check the list that's ACTUALLY effective.
    assert "volumes" not in lite_cpu_worker, (
        "docker-compose.lite.yml now declares its own `volumes:` for celery-cpu-worker "
        "— under compose merge semantics that list REPLACES (not appends to) the base "
        "service's, so this test must check lite's list directly, not base's"
    )
    effective_volumes = (base_doc.get("services") or {}).get("celery-cpu-worker", {}).get(
        "volumes"
    ) or []
    assert effective_volumes, (
        "celery-cpu-worker has no volumes at all in docker-compose.yml — the "
        "forbidden-mount check below would vacuously pass against an empty list"
    )
    volume_text = "\n".join(effective_volumes)
    for forbidden in ("diar-native-tmp", "transcription-temp"):
        assert forbidden not in volume_text, (
            f"celery-cpu-worker mounts '{forbidden}' (effective under --lite, since "
            f"lite adds no volumes of its own) — the embedding path needs no shared "
            f"volume; this implies (incorrectly) that lite diarizes"
        )
