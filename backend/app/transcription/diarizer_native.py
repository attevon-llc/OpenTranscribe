"""Native diarization engine client (diar-native sidecar) — drop-in for SpeakerDiarizer.

Speaks to the diar-server sidecar (Rust/speakrs engine; see /mnt/nvm/repos/diar-native).
Implements the same surface as SpeakerDiarizer.diarize()/embed_window() so callers are
untouched. Selection is driven by ``TranscriptionConfig.diarizer_backend`` (SystemSettings
``engine.diarizer_backend`` -> env ``ENGINE_DIARIZER_BACKEND`` -> default ``"native"``) via
ModelManager — this engine is now the PRIMARY diarizer (issue #58); the in-process PyAnnote
fork path is the explicit, documented failover, used automatically whenever the sidecar is
unreachable or fails mid-job.

Contract parity notes (vs diarizer.py):
- diarize() returns (DiarizeResult, overlap_info, native_embeddings) where segments come from
  the EXCLUSIVE diarization and overlap regions from the FULL diarization, and
  native_embeddings maps speaker labels to L2-normalized 256-d centroids — same as the
  pyannote path (centroids arrive un-normalized from the engine; we normalize here, matching
  build_native_embeddings semantics).
- embed_window() center-pads to >= max(model_min, 0.8 s) and NEVER raises into the caller.
- Speaker-count constraints: the sidecar currently runs auto speaker counting (community-1
  semantics). With the app defaults (min=1, max=20) the constraint path never binds; if
  config.num_speakers is set we log a warning and proceed (constraint port is tracked in
  diar-native PLAN.md M1).
- diarize() takes an EXTRA optional ``wav_path`` kwarg diarizer.py's diarize(audio) does not
  have (issue #661) — reuses an already-materialized shared-volume WAV instead of re-encoding
  ``audio`` a second time. It is additive-only (default None reproduces the old behaviour
  exactly) and native-only: callers must isinstance-check before passing it, never widen the
  shared call signature to accommodate it. See the docstring on diarize() itself.

This file is standalone/additive: no existing module is modified. Wiring lives in
ModelManager behind ``TranscriptionConfig.diarizer_backend`` (see diar-native
docs/INSTALL_NATIVE.md).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import numpy as np

from app.core.constants import DIAR_NATIVE_SHARED_DIR_DEFAULT
from app.core.constants import DIAR_SIDECAR_MIN_RTF
from app.core.constants import DIAR_SIDECAR_TIMEOUT_FLOOR_S
from app.core.constants import ENGINE_SHARED_VOLUME_DEFAULT
from app.core.constants import PIPELINE_SCRATCH_DEFAULT
from app.services.asr.errors import retry_after_from_headers
from app.transcription.diarize_result import DiarizeResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.transcription.diarizer import SpeakerDiarizer

logger = logging.getLogger(__name__)

# The model family the sidecar exports its ONNX/PLDA graphs from (issue #706) — recorded
# alongside "native" as the resolved diarization_model whenever the sidecar actually served
# the request. Not a live query of the sidecar (no endpoint reports it); this mirrors the
# same community-1 export documented in app/transcription/CLAUDE.md's provisioning section.
NATIVE_MODEL_NAME = "pyannote/speaker-diarization-community-1"

_DEFAULT_URL = os.environ.get("DIAR_NATIVE_URL", "http://diar-native:8701")
# Directory shared (bind/volume) between this worker and the sidecar container — the sidecar's
# own-copy fallback namespace inside pipeline_scratch (issue #661 E2). Still a documented,
# independently overridable env var; only the default changed.
_SHARED_DIR = os.environ.get("DIAR_NATIVE_SHARED_DIR", DIAR_NATIVE_SHARED_DIR_DEFAULT)
# CEILING on one /diarize request (issue #656 Step 4) — the real per-request budget is
# duration-scaled, see _diarize_budget_s() and core/constants.py's DIAR_SIDECAR_* comment.
# Default lowered 3600 -> 1800: at 1800 the worst case (a wedged sidecar plus the own-copy
# retry) holds a Celery slot for 3600s, comfortably under 25% of the default 21600s
# CELERY_VISIBILITY_TIMEOUT (see _warn_if_timeout_risks_redelivery below). Raise on a
# CPU-mode sidecar — CPU throughput is unmeasured in this repo.
_TIMEOUT_S = float(os.environ.get("DIAR_NATIVE_TIMEOUT_S", "1800"))


def _diarize_budget_s(audio: np.ndarray) -> float:
    """Per-request timeout for THIS audio, duration-scaled (issue #656 Step 4).

    ``urlopen``'s timeout is per-socket-operation and diar-server sends nothing until the
    job is done, so time-to-first-byte IS total processing time — a flat bound would give a
    30-second memo and a 4.7-hour recording the same hour-long budget. See the DIAR_SIDECAR_*
    comment in core/constants.py for the derivation and the CPU-mode caveat.
    """
    duration_s = len(audio) / 16000.0
    return min(_TIMEOUT_S, DIAR_SIDECAR_TIMEOUT_FLOOR_S + duration_s / DIAR_SIDECAR_MIN_RTF)


def _warn_if_timeout_risks_redelivery(timeout_s: float, visibility_timeout_s: float) -> None:
    """Loud, one-line warning when the ceiling could trigger acks_late redelivery.

    The only place these two independently-configured values are related. Two attempts (the
    own-copy retry after a reused-WAV 4xx) can each hold a Celery slot for up to `timeout_s`;
    if that total exceeds a quarter of CELERY_VISIBILITY_TIMEOUT, a wedged sidecar risks the
    task being redelivered to a second worker while the first is still running (core/celery.py's
    visibility_timeout comment). Silent at the new default (2*1800=3600 < 0.25*21600=5400); an
    operator who restores the old 3600s default gets 7200 > 5400 and this fires.
    """
    if 2 * timeout_s > 0.25 * visibility_timeout_s:
        logger.warning(
            "DIAR_NATIVE_TIMEOUT_S=%.0f: two attempts at the sidecar could hold a Celery "
            "slot for up to %.0fs, over 25%% of CELERY_VISIBILITY_TIMEOUT=%.0fs — risking "
            "acks_late redelivery of a still-running job. Lower DIAR_NATIVE_TIMEOUT_S or "
            "raise CELERY_VISIBILITY_TIMEOUT.",
            timeout_s,
            2 * timeout_s,
            visibility_timeout_s,
        )


_warn_if_timeout_risks_redelivery(
    _TIMEOUT_S, float(os.environ.get("CELERY_VISIBILITY_TIMEOUT", "21600"))
)
# Ask the sidecar for gender alongside diarization. Off leaves the app's own CPU task in charge.
_GENDER_ENABLED = os.environ.get("DIAR_NATIVE_GENDER", "1").lower() not in ("0", "false", "no")
# The engine's Stage-1 shared-volume WAV mount, as seen from THIS container (the worker, not
# the sidecar). Same env var EngineConfig reads for shared_volume_path — deliberately not a
# second knob, since issue #661 is precisely about reusing the WAV that variable locates.
#
# The default is `ENGINE_SHARED_VOLUME_DEFAULT` (app.core.constants) — the SAME constant
# `engine/config.py` and `tasks/transcription/preprocess.py` fall back to. Before issue #661's
# E0 fix this file alone defaulted to "/tmp/transcription" while the write side
# (preprocess.py) and EngineConfig both defaulted to a bare "/tmp": an install whose `.env`
# predated `ENGINE_SHARED_VOLUME_PATH` wrote the WAV into the writer's container-LOCAL /tmp,
# this reader looked in the real mount, found nothing, and silently fell back to MinIO — the
# exact per-job re-serialization cost this shared-volume path exists to avoid. Unifying all
# three sites on the mount path (not a bare /tmp) fixes that for exactly the install this
# mattered for, with no prefix-collision risk: the writer now targets the same mounted
# directory the sidecar reads, so there is no local-/tmp file for the prefix check below to
# mistake for a shared one. Since "reuse silently never fires" is itself a failure mode
# nothing else would surface, diarize() below logs which case fired on every call — see the
# "reuse-WAV optimisation" log lines.
_ENGINE_SHARED_DIR = os.environ.get("ENGINE_SHARED_VOLUME_PATH", ENGINE_SHARED_VOLUME_DEFAULT)

# Containment boundary for the reuse-WAV check, keyed on the pipeline_scratch VOLUME ROOT
# (issue #661 E2 phase 1.3) — NOT `_ENGINE_SHARED_DIR`. `str.startswith` against a subdirectory
# prefix has two independent problems: it is not real path containment (no `..` resolution, no
# symlink resolution — "/scratch/opentranscribe-evil/x.wav" passes a naive prefix test because
# the STRING happens to start with the prefix), and after E2 the engine WAV lives under
# `.../engine/<task>.wav` while rediarize's WAV lives under `.../<uuid>/audio.wav` — both on the
# same volume, but a prefix keyed on the `engine/` subdir would reject the second, silently
# forcing `_post_own_copy` on every rediarize. `PIPELINE_SCRATCH_DIR` mirrors the same env var
# `app.utils.scratch_volume` reads, so both sides of the reuse check agree on one root.
_SHARED_VOLUME_ROOT = os.path.realpath(
    os.environ.get("PIPELINE_SCRATCH_DIR", PIPELINE_SCRATCH_DEFAULT)
)


def _path_is_on_shared_volume(path: str) -> bool:
    """True when ``path`` resolves to somewhere inside the pipeline_scratch volume root.

    Uses ``os.path.realpath`` on both sides (resolves ``..`` and symlinks) plus
    ``os.path.commonpath`` — never ``str.startswith``, which a sibling directory sharing the
    same string prefix (e.g. ``/scratch/opentranscribe-evil``) would pass.
    """
    try:
        real = os.path.realpath(path)
        return os.path.commonpath([real, _SHARED_VOLUME_ROOT]) == _SHARED_VOLUME_ROOT
    except (ValueError, OSError):
        # ValueError: paths on different drives (Windows) or otherwise incomparable.
        return False


# Probe TTL cache (issue #661 probe-cost fix). A bare live GET costs the full connect timeout
# whenever the sidecar is unreachable, and the readiness gate is consulted several times per
# job: ModelManager._diarizer_current asks readiness then liveness (to tell "unreachable" from
# "up but unprovisioned"), load_model() asks liveness again, and #665 added
# engine/stages._overlap_diarization_enabled. Unmitigated that is four serial timeouts.
#
# The window is kept short because TranscriptionConfig._resolve_diarizer_backend documents a
# no-pinning contract — an admin toggling engine.diarizer_backend, and a sidecar recovering
# mid-queue, must both be picked up without a worker restart — and a long cache would defeat
# the readiness half of that guarantee. Negative results are cached too; they are the
# expensive case this exists for.
#
# ⚠️ The TTL MUST exceed the probe timeout, which is why the floor below is not advisory. An
# unreachable sidecar's probe takes the whole timeout, so an entry stamped after it is already
# that old; with a TTL shorter than the probe, every entry is born expired and the cache
# serves nothing. Measured in exactly that state: three consecutive calls each paid the full
# 5 s behind a nominally 3 s cache — the cache was a no-op in its only real use case.
_PROBE_TIMEOUT_S = 5.0
_READY_CACHE_TTL_S = max(
    float(os.environ.get("DIAR_NATIVE_READY_CACHE_TTL_S", "8")), _PROBE_TIMEOUT_S + 1.0
)
#: Keyed by (endpoint, url) so readiness and liveness cache independently. Liveness is
#: cached too: _diarizer_current calls sidecar_ready() then sidecar_healthy() to tell
#: "unreachable" from "up but unprovisioned", and load_model() probes liveness again — so
#: the failure path pays the timeout three times over without this.
_ready_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_ready_cache_lock = threading.Lock()


def _cached_probe(kind: str, url: str, probe: Callable[[str], bool]) -> bool:
    """Run *probe* for (kind, url), reusing a result younger than the TTL."""
    key = (kind, url)
    with _ready_cache_lock:
        cached = _ready_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _READY_CACHE_TTL_S:
            return cached[1]

    result = probe(url)

    # Stamp AFTER the probe, never before — see the timeout note above.
    with _ready_cache_lock:
        _ready_cache[key] = (time.monotonic(), result)
    return result


def reset_readiness_cache() -> None:
    """Clear the sidecar_ready() TTL cache.

    Test-only hook. test_diar_native_readiness_gate.py stands up a real HTTP server and flips
    it between states within a single test — without clearing the cache between phases, a
    verdict cached before the flip would mask the new state until the TTL lapsed. Production
    code has no reason to call this; the TTL is what keeps it honest there.
    """
    with _ready_cache_lock:
        _ready_cache.clear()


def post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON to the sidecar and decode the reply.

    Public because ``services/native_embedding_client`` speaks to the same sidecar
    for speaker embeddings; one HTTP client for both, not two.
    """
    import json

    req = urllib.request.Request(  # noqa: S310 — fixed internal http:// sidecar URL
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310 — internal service
        return cast(dict, json.loads(resp.read()))


def default_base_url() -> str:
    """The configured sidecar URL (``DIAR_NATIVE_URL``)."""
    return _DEFAULT_URL


def sidecar_healthy(base_url: str | None = None) -> bool:
    """True when the diar-native sidecar is LIVE — the process is up and answering.

    ⚠️ This is liveness, NOT readiness, and the difference is not academic. diar-native's
    ``/healthz`` returns 200 in *every* model state, including "no models" and "models
    known to be unusable". That is deliberate on the sidecar's side: the compose
    healthcheck gates on this endpoint, so a container that has not provisioned yet must
    not be marked unhealthy and fail ``compose up --wait`` for the whole stack.

    The consequence is that "answers /healthz" does not imply "can diarize". To decide
    whether to route work at the sidecar, use :func:`sidecar_ready`.

    TTL-cached on the same window as readiness — this is probed twice per failing job.
    """
    return _cached_probe(
        "healthz", (base_url or _DEFAULT_URL).rstrip("/"), _sidecar_healthy_uncached
    )


def _sidecar_healthy_uncached(url: str) -> bool:
    """The live ``/healthz`` probe. Always call :func:`sidecar_healthy` instead."""
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
            f"{url}/healthz", timeout=_PROBE_TIMEOUT_S
        ) as resp:
            return bool(resp.status == 200)
    except Exception:  # noqa: BLE001 — unreachable for any reason means "not healthy"
        return False


def sidecar_status(base_url: str | None = None) -> dict:
    """The sidecar's ``/healthz`` body, or ``{}`` if it cannot be read.

    Used only to attach a REASON to a readiness failure. Never used to decide readiness:
    the status code carries that, and parsing a body to make a routing decision would
    reintroduce the coupling ``/readyz`` exists to remove.
    """
    import json

    url = (base_url or _DEFAULT_URL).rstrip("/")
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
            f"{url}/healthz", timeout=5
        ) as resp:
            body = json.loads(resp.read())
            # A 0.2.0 sidecar answers /healthz with the bare string "ok", so the body is
            # not guaranteed to be an object. A non-dict means "no reason available".
            return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001 — a missing reason must never fail the caller
        return {}


def sidecar_supports_cpu_device(base_url: str | None = None) -> bool:
    """True when the sidecar has the CPU provider ``devices``-LOADED and can serve it now.

    Gates issue #679's per-request device routing. The sidecar's request structs do NOT use
    ``deny_unknown_fields``, so an OLD diar-server (pre this field) silently IGNORES an
    unrecognised ``"device"`` key and just runs on CUDA, returning 200 — indistinguishable
    from success. Sending ``device`` blind would look like it worked while burning the exact
    GPU slot it was meant to spare. This must be checked before every such request.

    Shares :func:`_cached_probe`'s TTL cache/window with :func:`sidecar_ready` /
    :func:`sidecar_healthy` — same cost profile, no second cache to keep in sync.
    """
    return _cached_probe(
        "cpu_device", (base_url or _DEFAULT_URL).rstrip("/"), _sidecar_supports_cpu_uncached
    )


def _sidecar_supports_cpu_uncached(url: str) -> bool:
    """The live check backing :func:`sidecar_supports_cpu_device`.

    Reads the same ``/healthz`` body ``sidecar_status`` already fetches for its "reason"
    string — a second endpoint would be a second round trip for information the sidecar
    already puts on the endpoint we probe for liveness anyway.

    ⚠️ Reads ``devices``, NOT ``supported_devices``. They are different questions and
    conflating them is a live outage. ``/healthz`` returns both::

        "devices":           ["cuda"]           <- LOADED, and therefore servable now
        "supported_devices": ["cuda", "cpu"]    <- what this BINARY is able to load

    ``supported_devices`` is a build-time capability advertisement; the set actually
    loaded is chosen at start-up by ``DIAR_DEVICES``. Keying on the capability made this
    return True on every ordinary GPU deployment, so ``/embed_window`` was sent
    ``device: "cpu"`` and answered::

        400  device 'cpu' is not loaded; this server is serving [cuda]
             (add it to DIAR_DEVICES to load it)

    which ``embed_waveform`` turns into ``None`` — i.e. **every speaker embedding
    silently fell back to the in-process PyAnnote model**, on a sidecar reporting
    healthy with models verified. Measured against the live sidecar, not reasoned about.

    Absence still reads as False, which keeps the original guard intact: an old
    diar-server predating these fields has no ``devices`` key either, and its request
    struct does not use ``deny_unknown_fields``, so it would silently IGNORE a ``device``
    key and run on CUDA anyway — success-shaped, while burning the GPU slot the routing
    exists to spare.
    """
    devices = sidecar_status(url).get("devices") or []
    return "cpu" in devices


def sidecar_ready(base_url: str | None = None) -> bool:
    """True when the sidecar can actually SERVE — models present and verified.

    This is the predicate to route on. ``/readyz`` is 200 only once the models are
    verified and 503 otherwise, so unlike :func:`sidecar_healthy` it distinguishes
    "still provisioning" and "models broken" from "ready to work".

    Before this existed, engine selection asked ``/healthz`` and therefore treated a
    sidecar with an unusable models directory as a working one: the native engine was
    chosen, and the failure surfaced at request time instead of at the point where the
    in-process PyAnnote fallback was still available to choose.

    **Older sidecars have no ``/readyz``** (it landed in diar-native 0.3.0) and answer
    404. That is not "not ready" — it is "cannot say", so we fall back to liveness and
    preserve the previous behaviour rather than disabling the native engine outright for
    anyone still pinned to a pre-0.3.0 image.

    Result is cached per ``base_url`` for :data:`_READY_CACHE_TTL_S` seconds — see the
    comment above that constant for why the window is short rather than absent.
    """
    return _cached_probe("readyz", (base_url or _DEFAULT_URL).rstrip("/"), _sidecar_ready_uncached)


def _sidecar_ready_uncached(url: str) -> bool:
    """The live ``/readyz`` probe. Always call :func:`sidecar_ready` instead — this exists
    only so the TTL cache above has something to wrap without duplicating the probe logic.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
            f"{url}/readyz", timeout=5
        ) as resp:
            return bool(resp.status == 200)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("diar-native sidecar has no /readyz (pre-0.3.0); falling back to /healthz")
            return sidecar_healthy(url)
        status = sidecar_status(url)
        logger.warning(
            "diar-native sidecar is up but not ready (HTTP %s, models_state=%s): %s",
            exc.code,
            status.get("models_state", "unknown"),
            status.get("models_reason", "no reason given"),
        )
        return False
    except Exception:  # noqa: BLE001 — unreachable for any reason means "not ready"
        return False


#: Human-readable description per diarization backend — the single table for admin/stats
#: displays. Previously duplicated (issue #672's second half): ``admin.py`` and
#: ``stats_helpers.py`` each hand-rolled their own copy of this table AND resolved the
#: CONFIGURED backend through two different functions (one unvalidated), so the two panels
#: could report different engines for the identical misconfiguration
#: (``ENGINE_DIARIZER_BACKEND=typo`` read as ``"typo diarization engine"`` on one panel and
#: ``"native"``'s description on the other). Keyed by ``engine.backends.VALID_DIARIZER_BACKENDS``.
_ENGINE_DESCRIPTIONS: dict[str, str] = {
    "native": "diar-native sidecar (Rust/ONNX) — primary diarization engine",
    "pyannote": "PyAnnote fork (in-process) — explicit pin or automatic native-sidecar failover",
}


def describe_diarizer_status() -> dict[str, Any]:
    """Configured vs EFFECTIVE diarization engine — the one resolver both stats panels use.

    ``configured`` is whatever ``TranscriptionConfig._resolve_diarizer_backend()`` resolves
    (SystemSettings -> env -> coded default "native", already validated against
    ``VALID_DIARIZER_BACKENDS`` and fail-safe on a bad value) — the SAME call
    ``ModelManager`` makes to pick the engine at runtime, so this display value can never
    diverge from the pipeline's own decision the way the two duplicated resolvers used to.

    ``effective`` answers the harder, and more important, question: which engine will
    actually SERVE. ``ModelManager._build_diarizer`` falls back from native to the
    in-process PyAnnote fork silently whenever the sidecar cannot serve — that is the whole
    point of the fallback — so a deployment configured for native can spend its entire life
    on PyAnnote while every setting still says "native". "pyannote" never falls back to
    anything (there is no engine below it), so it is always its own effective engine;
    "native" is effective only when :func:`sidecar_ready` agrees the sidecar can actually
    diarize — the identical predicate ``ModelManager._diarizer_current`` gates the real
    routing decision on.

    Cost: at most ONE call to :func:`sidecar_ready`, made only when ``configured ==
    "native"``. That call is itself TTL-cached (``_READY_CACHE_TTL_S``, ~8s) and shares its
    cache with the transcription pipeline's own per-task probes, so in the common case
    (cache warm) this function costs a dict lookup. Worst case — cache cold AND the sidecar
    silently unreachable — is ONE bounded HTTP call at the ``_PROBE_TIMEOUT_S`` (5s)
    ceiling, never unbounded and never chained into a second probe (a fast 404 from a
    pre-0.3.0 sidecar falls through to :func:`sidecar_healthy`, but that response is by
    definition fast — the sidecar just answered). Callers on an async request path must
    NOT await this inline: offload it (e.g. ``asyncio.to_thread``), exactly as the psutil
    calls beside it already are in ``admin.py``'s ``/admin/stats`` — a synchronous 5s probe
    run directly inside an ``async def`` handler blocks that worker's whole event loop, not
    just the one request.

    Returns:
        Dict with ``configured``, ``configured_description``, ``effective``,
        ``effective_description``, and ``using_fallback`` (True exactly when the two
        engines differ — the signal issue #672 exists to surface).
    """
    from app.transcription.config import TranscriptionConfig

    configured = TranscriptionConfig._resolve_diarizer_backend()
    effective = configured
    if configured == "native" and not sidecar_ready():
        effective = "pyannote"

    return {
        "configured": configured,
        "configured_description": _ENGINE_DESCRIPTIONS.get(
            configured, f"{configured} diarization engine"
        ),
        "effective": effective,
        "effective_description": _ENGINE_DESCRIPTIONS.get(
            effective, f"{effective} diarization engine"
        ),
        "using_fallback": effective != configured,
    }


def sidecar_diagnostics(base_url: str | None = None) -> dict[str, Any]:
    """Live sidecar diagnostics for the admin stats surface (issue #656 Step 9).

    Answers "can the sidecar serve RIGHT NOW" — a live, deployment-wide probe. This is
    deliberately a DIFFERENT question from #706's ``media_file.diarization_provider``
    ("which engine served THIS FILE", per-row, historical) — do not derive one from the
    other, and do not fold this into :func:`describe_diarizer_status`, which answers
    configured-vs-effective for the CONFIGURED backend and is consulted regardless of which
    engine is configured.

    ``models_state``/``models_reason`` come from the same ``/healthz`` body
    :func:`sidecar_status` already exposes for a readiness-failure log line; ``max_inflight``
    is read directly from the env var the sidecar's own admission gate is configured with
    (``DIAR_NATIVE_MAX_INFLIGHT``) since the sidecar exposes no endpoint for its own
    configuration. Queue depth / in-flight count are DELIBERATELY NOT surfaced here — the
    sidecar exposes no such endpoint, and inferring it client-side would be a guess presented
    as fact (see the diar-native upstream asks in issue #656).
    """
    url = (base_url or _DEFAULT_URL).rstrip("/")
    live = sidecar_healthy(url)
    ready = sidecar_ready(url)
    status = sidecar_status(url)
    return {
        "url": url,
        "live": live,
        "ready": ready,
        "models_state": status.get("models_state"),
        "models_reason": status.get("models_reason"),
        "max_inflight": int(os.environ.get("DIAR_NATIVE_MAX_INFLIGHT", "2")),
    }


def _overlap_regions(full_segments: list[dict], min_duration: float) -> list[dict]:
    """Sweep-line: regions where >=2 speakers are simultaneously active (start/end dicts)."""
    events: list[tuple[float, int]] = []
    for seg in full_segments:
        events.append((float(seg["start"]), 1))
        events.append((float(seg["end"]), -1))
    events.sort()
    regions: list[dict] = []
    depth = 0
    region_start: float | None = None
    for t, delta in events:
        depth += delta
        if depth >= 2 and region_start is None:
            region_start = t
        elif depth < 2 and region_start is not None:
            if t - region_start >= min_duration:
                regions.append({"start": region_start, "end": t})
            region_start = None
    return regions


def _log_reuse_decision(wav_path: str | None, reused: bool) -> None:
    """Observable signal for issue #661's E0 (make the fast path run, and PROVE it).

    Silence here is exactly the failure mode that let the reuse optimisation never fire on
    an install whose ``.env`` predates ``ENGINE_SHARED_VOLUME_PATH`` — every health check
    stayed green while every diarization quietly took the slower re-encode path.
    """
    if wav_path and not reused:
        if not _path_is_on_shared_volume(wav_path):
            logger.info(
                "diar-native reuse-WAV optimisation NOT used: %s is not under the shared "
                "volume root %s. If this worker and the sidecar should share a volume, set "
                "PIPELINE_SCRATCH_DIR the same way in both (see .env.example).",
                wav_path,
                _SHARED_VOLUME_ROOT,
            )
        else:
            logger.info(
                "diar-native reuse-WAV optimisation NOT used: staged path %s does not "
                "exist on disk.",
                wav_path,
            )
    elif reused:
        logger.info("diar-native reusing staged WAV at %s", wav_path)


class DiarSidecarUnavailableError(RuntimeError):
    """The sidecar could not serve this request for a reachability/capacity reason.

    Subclasses ``RuntimeError`` deliberately (issue #656 Step 3) — mirrors
    ``app.services.asr.errors.ASRProviderError``'s reasoning: every existing ``except
    Exception`` site keeps working unmodified for anything not positively classified.

    ``reason`` is one of ``"unreachable"`` (connection refused/reset — the sidecar process is
    gone), ``"timeout"`` (the sidecar answered ``/readyz`` but ``/diarize`` never returned
    inside the budget — typically a saturated admission gate), or ``"backpressure"`` (HTTP
    429/503 — the sidecar explicitly asked to be retried later). Only ``"backpressure"``
    carries a meaningful ``retry_after``.

    Deliberately NOT raised for a 4xx on a path the sidecar could not open (422 etc, still
    retried once with a fresh copy) or a genuine engine error (5xx/malformed JSON — that is
    deterministic and a retry would only reproduce it): both of those stay plain exceptions so
    they are never retried by a Celery task's ``except DiarSidecarUnavailableError`` handler.
    """

    def __init__(self, message: str, *, reason: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after = retry_after


def _classify_sidecar_failure(exc: BaseException) -> BaseException:
    """Turn a raw urllib failure into a :class:`DiarSidecarUnavailableError` when it represents a
    reachability/capacity problem, else return ``exc`` unchanged — see that class's docstring
    for exactly which failures are, and are not, classified.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (429, 503):
            return DiarSidecarUnavailableError(
                f"diar-native backpressure (HTTP {exc.code})",
                reason="backpressure",
                retry_after=retry_after_from_headers(exc.headers),
            )
        return exc
    # TimeoutError (socket.timeout is an alias since Python 3.10) means the sidecar answered
    # /readyz fast but /diarize never returned inside the budget — a saturated admission gate,
    # not a dead process. urllib.error.URLError (connection refused/reset) means the process
    # itself is unreachable. Order matters: TimeoutError does NOT subclass URLError.
    if isinstance(exc, TimeoutError):
        return DiarSidecarUnavailableError(f"diar-native timed out: {exc}", reason="timeout")
    if isinstance(exc, urllib.error.URLError):
        return DiarSidecarUnavailableError(f"diar-native unreachable: {exc}", reason="unreachable")
    return exc


def diarizer_require_sidecar() -> bool:
    """Resolve ``engine.diarizer_require_sidecar``: SystemSettings -> env -> default False.

    Issue #656 Step 6. Mirrors ``TranscriptionConfig._resolve_diarizer_backend``'s DB > env >
    default order, re-read on every call (no pinning) for the same reason — an admin flipping
    this must take effect without a worker restart. **Default false** reproduces today's
    silent-fallback behaviour exactly; when true, :meth:`NativeSpeakerDiarizer.diarize` raises
    :class:`DiarSidecarUnavailableError` instead of building the in-process PyAnnote fallback,
    making the fail-hard policy #572 will eventually make the ONLY policy exercisable in
    production before that fallback is deleted.
    """
    raw = os.getenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", "false")
    try:
        from app.db.session_utils import session_scope
        from app.models.system_settings import SystemSettings

        with session_scope() as db:
            setting = (
                db.query(SystemSettings)
                .filter(SystemSettings.key == "engine.diarizer_require_sidecar")
                .first()
            )
            if setting and setting.value is not None:
                raw = str(setting.value)
    except Exception:  # noqa: S110  # nosec B110
        # DB not available (e.g. during testing, worker startup race) — fall back to env.
        logger.debug("Could not read engine.diarizer_require_sidecar from DB, using env var")
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


class NativeSpeakerDiarizer:
    """SpeakerDiarizer-compatible client for the diar-native sidecar."""

    def __init__(self, config: Any, base_url: str | None = None):
        self.config = config
        self.base_url = (base_url or _DEFAULT_URL).rstrip("/")
        self.is_loaded = False
        self._fallback: SpeakerDiarizer | None = None
        # Which engine actually served the MOST RECENT diarize() call (issue #706) — set at
        # every return point in diarize(), including the internal fallback branch, so a caller
        # that only holds this object (typed as the duck-typed common interface) can still
        # learn what ran without re-deriving it from log lines. Defaults optimistically to
        # "native" before any call completes; callers should read these only after diarize()
        # returns.
        # `last_model` is Optional on purpose: the PyAnnote fallback branch reads its model
        # name off the fallback engine, which may not expose one. Declaring that here keeps
        # every read of the attribute checked, rather than widening the fallback assignment.
        self.last_provider: str = "native"
        self.last_model: str | None = NATIVE_MODEL_NAME

    # -- lifecycle ---------------------------------------------------------

    def load_model(self) -> None:
        """Readiness-check the sidecar (weights live server-side; nothing loads here).

        Deliberately ``sidecar_ready`` and not ``sidecar_healthy``: this method exists to
        answer "can this object serve diarization", and a sidecar whose models directory
        is empty or broken answers /healthz with a cheerful 200. Raising here routes the
        caller to the PyAnnote fallback while it is still a choice.
        """
        if not sidecar_healthy(self.base_url):
            raise RuntimeError(f"diar-native sidecar unavailable at {self.base_url}")
        if not sidecar_ready(self.base_url):
            status = sidecar_status(self.base_url)
            raise RuntimeError(
                f"diar-native sidecar at {self.base_url} is up but not ready "
                f"(models_state={status.get('models_state', 'unknown')}): "
                f"{status.get('models_reason', 'no reason given')}"
            )
        self.is_loaded = True
        logger.info("diar-native sidecar ready at %s", self.base_url)

    def unload_model(self) -> None:
        self.is_loaded = False  # server-side weights stay resident by design
        if self._fallback is not None:
            self._fallback.unload_model()
            self._fallback = None

    def _fallback_engine(self) -> SpeakerDiarizer:
        """In-process PyAnnote engine, loaded on first use after a sidecar loss."""
        fallback = self._fallback
        if fallback is None:
            from app.transcription.diarizer import SpeakerDiarizer

            fallback = SpeakerDiarizer(self.config)
            fallback.load_model()
            self._fallback = fallback
        return fallback

    # -- main entry points -------------------------------------------------

    def _post_own_copy(self, audio: np.ndarray, timeout: float) -> tuple[dict, str]:
        """Write our own WAV copy of *audio* and POST it. Returns ``(response, path)``.

        Split out of ``diarize()`` to keep that method's cyclomatic complexity in check, and
        because it is exactly the unit the write-vs-HTTP-failure comment there talks about:
        one write op followed by one POST, both degrading the same way to the caller.
        Raises on any failure (disk-full, unwritable scratch volume, HTTP failure); the
        caller decides how to degrade.

        ⚠️ This method owns the file it writes until it hands the path back. ``return
        self._post_diarize(own_wav), own_wav`` evaluates the POST *first*, so on any failure
        the tuple is never constructed and the caller's ``own_wav`` stays ``None`` — making
        its ``finally``'s ``if own_wav:`` cleanup a no-op and orphaning the WAV. Nothing
        sweeps ``_SHARED_DIR``, so at 32 KB per audio-second that leaked ~460 MB per failed
        4-hour job onto the ``diar-native-tmp`` volume. This was a regression introduced when
        the method was split out of ``diarize()``: the assignment used to precede the POST
        inside the caller's ``try``, so the ``finally`` fired. Hence the explicit unlink here.
        """
        os.makedirs(_SHARED_DIR, exist_ok=True)
        own_wav = os.path.join(_SHARED_DIR, f"diar_{uuid.uuid4().hex}.wav")
        clip = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        pcm = (clip * 32767.0).astype("<i2")
        with wave.open(own_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm.tobytes())
        try:
            return self._post_diarize(own_wav, timeout), own_wav
        except BaseException as exc:
            # BaseException, not Exception: a timeout arriving as a KeyboardInterrupt/
            # SystemExit during shutdown must not leak either, and we re-raise unchanged.
            with contextlib.suppress(OSError):
                os.unlink(own_wav)
            # Classify the same way as the reused-WAV path (issue #656 Step 3) — the
            # overlapped-diarization thread can hit this branch too (no wav_path is ever
            # passed on that path today, but nothing guarantees it stays that way), and
            # _AsyncDiarization.failed_unavailable must see a consistent classification
            # regardless of which branch actually ran.
            if isinstance(exc, Exception):
                classified = _classify_sidecar_failure(exc)
                if classified is not exc:
                    raise classified from exc
            raise

    def _try_reused_wav(self, wav_path: str, timeout: float) -> dict | None:
        """Attempt the staged-WAV POST. Returns the response on success, or ``None`` when
        the caller should retry with its own freshly-written copy (an HTTP 4xx — 400/404/422
        — the sidecar is reachable and answered, it just could not open THIS path).

        Raises for anything else (timeout, connection loss, 429/503 backpressure): a second
        attempt at the same sidecar cannot recover from any of those — it would only double
        the worst-case hang (timeout/unreachable) or waste a write against a sidecar that just
        said back off (backpressure). Those three are raised as :class:`DiarSidecarUnavailableError`
        (issue #656 Step 3) so the caller/task can classify and retry appropriately; a genuine
        engine error (5xx other than 503, malformed JSON) is re-raised unclassified — it is
        deterministic and a retry would only reproduce it. Split out of ``diarize()`` to keep
        its cyclomatic complexity in check.
        """
        try:
            return self._post_diarize(wav_path, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404, 422):
                logger.warning(
                    "diar-native could not use the staged WAV at %s (HTTP %s); re-sending "
                    "our own copy. If this repeats, the sidecar is missing the %s mount — "
                    "recreate that container rather than restarting it.",
                    wav_path,
                    exc.code,
                    _ENGINE_SHARED_DIR,
                )
                return None
            classified = _classify_sidecar_failure(exc)
            logger.warning(
                "diar-native /diarize responded HTTP %s for the staged WAV at %s; NOT "
                "retrying with a fresh copy — %s",
                exc.code,
                wav_path,
                "the sidecar asked to be retried later"
                if isinstance(classified, DiarSidecarUnavailableError)
                else "this is a deterministic engine error, a retry would only reproduce it",
            )
            if classified is exc:
                raise
            raise classified from exc
        except Exception as exc:  # noqa: BLE001 — timeout/connection loss: don't retry, re-raise
            classified = _classify_sidecar_failure(exc)
            logger.warning(
                "diar-native /diarize timed out or the connection was lost (%s); NOT "
                "retrying — falling back to PyAnnote",
                exc,
            )
            if classified is exc:
                raise
            raise classified from exc

    def _post_diarize(self, path: str, timeout: float) -> dict:
        """POST one /diarize request for a WAV the sidecar is expected to be able to open.

        Gender rides this call: the sidecar already has the decoded audio and the speaker
        turns in hand, so classifying costs no second fetch or decode. ``timeout`` is the
        duration-scaled per-request budget from :func:`_diarize_budget_s` (issue #656 Step 4),
        not the flat ``_TIMEOUT_S`` ceiling directly.
        """
        return post_json(
            f"{self.base_url}/diarize",
            {"wav_path": path, "file_id": "job", "gender": _GENDER_ENABLED},
            timeout=timeout,
        )

    def _refuse_or_fallback_impl(
        self, exc: BaseException, audio: np.ndarray, allow_local_fallback: bool
    ) -> tuple[DiarizeResult, dict, dict | None]:
        """Degrade a sidecar failure to the PyAnnote fallback, or refuse to.

        Extracted out of :meth:`diarize` (rather than kept as a nested closure) purely to
        keep that method's cyclomatic complexity under the repo's C901 gate — not for reuse.
        """
        if not allow_local_fallback:
            raise RuntimeError(
                "diar-native failed while overlapped with transcription; refusing the "
                "in-process PyAnnote fallback on this thread (unsafe to load/release GPU "
                "models while transcription may still be running) — the caller must retry "
                f"sequentially: {exc}"
            ) from exc
        if diarizer_require_sidecar():
            # Issue #656 Step 6: makes the fail-hard policy exercisable in production BEFORE
            # #572 deletes the fallback outright. Re-raise the classified exception unchanged
            # so a Celery task's `except DiarSidecarUnavailableError` handler (Step 5) sees
            # it; wrap anything else (a plain HTTPError/OSError) the same way so the policy is
            # uniform regardless of which failure path triggered it.
            if isinstance(exc, DiarSidecarUnavailableError):
                raise exc
            raise DiarSidecarUnavailableError(
                f"diar-native failed and engine.diarizer_require_sidecar is set, refusing "
                f"the PyAnnote fallback: {exc}",
                reason="unreachable",
            ) from exc
        fallback_result = self._fallback_engine().diarize(audio)
        self.last_provider = "pyannote"
        self.last_model = getattr(self._fallback, "_model_name", None)
        return fallback_result

    def diarize(
        self,
        audio: np.ndarray,
        wav_path: str | None = None,
        *,
        allow_local_fallback: bool = True,
        skip_sidecar: bool = False,
    ) -> tuple[DiarizeResult, dict, dict[str, np.ndarray] | None]:
        """Run diarization, optionally against a WAV the caller already wrote to disk.

        Args:
            audio: 16kHz mono float32 waveform — the same shape ``SpeakerDiarizer.diarize``
                takes, and still what is used for the PyAnnote fallback if this call degrades.
            wav_path: An already-materialized 16kHz mono WAV of the same audio (issue #661),
                e.g. Stage 1's shared-volume WAV in the 3-stage Celery pipeline. Only honored
                when it lives under ``_ENGINE_SHARED_DIR`` — the mount this sidecar shares
                with the engine — since that is the only case where the path this container
                sees also resolves inside the sidecar's container. Anything else (including
                the single-process ``run()`` path, which never has a shared-volume WAV at
                all) falls through to writing our own copy, exactly as before.

                NATIVE-ONLY, deliberately not on ``SpeakerDiarizer.diarize``: the two engines
                are a duck-typed drop-in for each other, and callers that only hold ``Any``
                (``ModelManager.get_diarizer``'s return) must isinstance-check before ever
                passing this — see ``engine/stages.py``'s ``_run_diarize``.
            allow_local_fallback: When False, a sidecar failure raises instead of loading the
                in-process PyAnnote fallback here. Set False by ``engine/stages.py``'s
                ``_AsyncDiarization`` — the overlapped-diarization thread that runs
                concurrently with transcription on the main thread (issue #665). Loading
                PyAnnote from THAT thread would put it on the GPU while Whisper may still be
                mid-inference on the main thread, and freeing the transcriber to make room is
                exactly as unsafe (it could tear down weights the main thread is using). The
                caller catches the raise and retries sequentially, after transcription is
                known to have finished, where both are safe.
            skip_sidecar: When True, skip the HTTP attempt(s) entirely and go straight to
                :func:`_refuse_or_fallback` (issue #656 Step 2). Set by ``_collect_diarization``
                for the inline retry that follows a failed OVERLAPPED attempt, but only when
                that attempt's failure was classified :class:`DiarSidecarUnavailableError` (sidecar
                unreachable/wedged/backpressured) — never for a 422 or a genuine engine error,
                which a fresh sidecar attempt can still recover from. Without this, a SATURATED
                sidecar (readyz green, diarize gated) pays the full budget TWICE: once on the
                overlapped thread, once again on the inline retry against the identical
                permit-starved gate — measured worst case 2 x 3600s = 2h, each holding a Celery
                slot, a GPU thread and the decoded ndarray.
        """
        if not self.is_loaded:
            raise RuntimeError("Diarizer not loaded. Call load_model() first.")
        if getattr(self.config, "num_speakers", None) is not None:
            logger.warning(
                "diarizer_backend=native: num_speakers=%s requested but the native engine "
                "runs auto speaker counting (constraint port pending); proceeding with auto.",
                self.config.num_speakers,
            )

        def _refuse_or_fallback(exc: BaseException) -> tuple[DiarizeResult, dict, dict | None]:
            return self._refuse_or_fallback_impl(exc, audio, allow_local_fallback)

        if skip_sidecar:
            return _refuse_or_fallback(
                RuntimeError(
                    "skip_sidecar=True: a previous overlapped attempt already reported the "
                    "sidecar unreachable/wedged/backpressured; not attempting it again"
                )
            )

        step_start = time.perf_counter()
        timeout = _diarize_budget_s(audio)
        # Reusing Stage 1's WAV (issue #661) saves a full write and read of the recording,
        # but it is only an OPTIMISATION and must never cost us the engine. A sidecar that
        # cannot open the path answers 422 and, before this retry existed, that degraded the
        # job to PyAnnote — silently, since the fallback works. The commonest cause is
        # mundane and affects every upgrading install: a sidecar container created before
        # the transcription-temp mount existed keeps its old mount set until it is
        # RECREATED, not merely restarted. Reproduced live, and it made every diarization on
        # the box fall back while every health check stayed green.
        reused = bool(wav_path and _path_is_on_shared_volume(wav_path) and os.path.isfile(wav_path))
        _log_reuse_decision(wav_path, reused)

        own_wav: str | None = None
        try:
            out = None
            if reused:
                try:
                    out = self._try_reused_wav(str(wav_path), timeout)
                except Exception as exc:  # noqa: BLE001 — timeout/connection loss, see above
                    return _refuse_or_fallback(exc)

            if out is None:
                # The WAV write shares the sidecar-loss fallback, not just the HTTP call:
                # a permission or disk-space failure on the shared scratch volume (e.g.
                # the volume landing root-owned on first creation) is exactly as
                # recoverable as an unreachable sidecar — both mean "this job cannot use
                # diar-native right now" — and previously a write failure propagated out
                # of diarize() uncaught and hard-failed the whole transcription.
                try:
                    out, own_wav = self._post_own_copy(audio, timeout)
                except Exception as exc:  # noqa: BLE001 — a sidecar loss must not fail the job
                    logger.warning(
                        "diar-native /diarize failed mid-job (%s); falling back to PyAnnote",
                        exc,
                    )
                    return _refuse_or_fallback(exc)
        finally:
            # Only ever unlink a file THIS call created. A reused Stage-1 WAV is still owned
            # by the pipeline stage that wrote it (cleaned up later via
            # audio_loader.cleanup_shared_volume_wav) — deleting it here would pull it out
            # from under whichever stage runs next.
            if own_wav:
                with contextlib.suppress(OSError):
                    os.unlink(own_wav)

        exclusive = out.get("exclusive_segments", [])
        full = out.get("segments", [])
        diarize_df = DiarizeResult(
            start=np.asarray([s["start"] for s in exclusive], dtype=np.float64),
            end=np.asarray([s["end"] for s in exclusive], dtype=np.float64),
            speaker=np.asarray([s["speaker"] for s in exclusive], dtype=object),
        )

        overlap_info: dict = {"count": 0, "duration": 0.0, "regions": []}
        if getattr(self.config, "enable_overlap_detection", False):
            regions = _overlap_regions(
                full, float(getattr(self.config, "overlap_min_duration", 0.25))
            )
            if regions:
                overlap_info = {
                    "count": len(regions),
                    "duration": sum(r["end"] - r["start"] for r in regions),
                    "regions": regions,
                }

        native_embeddings: dict[str, np.ndarray] = {}
        if getattr(self.config, "enable_native_embeddings", True):
            centroids = out.get("centroids") or []
            present = {s["speaker"] for s in exclusive}
            for idx, row in enumerate(centroids):
                label = f"SPEAKER_{idx:02d}"
                if label not in present:
                    continue
                vec = np.asarray(row, dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    native_embeddings[label] = vec / norm

        # Rides on the result object rather than the return tuple, so the fork's contract is
        # untouched and the verdicts stay tied to the call that produced them.
        diarize_df.speaker_gender = out.get("speaker_gender") or None

        logger.info(
            "native diarization done in %.1fs: %d segments, %d speakers",
            time.perf_counter() - step_start,
            len(diarize_df),
            out.get("num_speakers", len(native_embeddings)),
        )
        self.last_provider = "native"
        self.last_model = NATIVE_MODEL_NAME
        return diarize_df, overlap_info, native_embeddings

    def embed_window(self, audio: np.ndarray, start: float, end: float) -> np.ndarray | None:
        """Same padding + never-raise semantics as SpeakerDiarizer.embed_window.

        ⚠️ The clip is fitted to the sidecar's fixed 10 s model window before it is
        sent (``native_embedding_client.fit_to_window``). Posting the raw slice —
        which is what this did until issue #571 — let the sidecar zero-pad it to
        10 s and pool the padding with mask weight 1, so a 0.8 s re-check window
        came back as an embedding of mostly silence: measured cosine against the
        in-process model on the same clip was **+0.012 at 0.8 s** (and +1.000 only
        at exactly 10 s). Repeat-filling the window instead keeps every pooled
        frame real speech from the disputed word, and restores agreement with the
        in-process path to 0.989.
        """
        if self._fallback is not None:
            # This job already fell back mid-diarization; keep the re-check on the same
            # engine that produced the segments.
            return self._fallback.embed_window(audio, start, end)
        from app.services.native_embedding_client import embed_waveform

        sr = 16000
        min_samples = int(0.8 * sr)
        s, e = int(start * sr), int(end * sr)
        if e - s < min_samples:
            mid = (s + e) // 2
            s, e = mid - min_samples // 2, mid + min_samples // 2
        s, e = max(0, s), min(len(audio), e)
        if e - s < min_samples // 2:
            return None
        # embed_waveform never raises: it returns None on any sidecar failure.
        return embed_waveform(np.ascontiguousarray(audio[s:e]), base_url=self.base_url)
