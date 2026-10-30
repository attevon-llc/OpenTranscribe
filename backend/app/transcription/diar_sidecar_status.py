"""Sidecar readiness / liveness / status probes and the admin status resolvers.

Split out of ``diarizer_native.py``: everything that asks "can the diar-native sidecar serve
RIGHT NOW" (TTL-cached ``/healthz`` + ``/readyz`` probes, the CPU-device capability check, the
configured-vs-effective engine description, the admin diagnostics) plus the
``DiarSidecarUnavailableError`` classification and the ``engine.diarizer_require_sidecar`` policy
read. ``NativeSpeakerDiarizer`` (still in ``diarizer_native.py``) consumes these through its own
module globals; callers that only need a probe import from here and never pull in the client.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING
from typing import Any

from app.core.env import is_truthy
from app.services.asr.errors import retry_after_from_headers

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_DEFAULT_URL = os.environ.get("DIAR_NATIVE_URL", "http://diar-native:8701")


# Probe TTL cache (issue #661 probe-cost fix). A bare live GET costs the full connect timeout
# whenever the sidecar is unreachable, and the readiness gate is consulted several times per
# job: ModelManager._diarizer_current asks readiness then liveness (to tell "unreachable" from
# "up but unprovisioned"), load_model() asks liveness again, and #665 added
# engine/diarization_overlap._overlap_diarization_enabled. Unmitigated that is four serial timeouts.
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
_READY_CACHE_TTL_S_CONFIGURED = float(os.environ.get("DIAR_NATIVE_READY_CACHE_TTL_S", "8"))
_READY_CACHE_TTL_S = max(_READY_CACHE_TTL_S_CONFIGURED, _PROBE_TIMEOUT_S + 1.0)
if _READY_CACHE_TTL_S != _READY_CACHE_TTL_S_CONFIGURED:
    # Once, at import time — never per-call, this module has no per-call logging budget
    # for a value that is fixed for the process lifetime.
    logger.warning(
        "DIAR_NATIVE_READY_CACHE_TTL_S=%s is below the probe-timeout floor; using %ss instead "
        "(a shorter TTL makes the readiness cache a no-op, see the comment above)",
        _READY_CACHE_TTL_S_CONFIGURED,
        _READY_CACHE_TTL_S,
    )
#: Keyed by (endpoint, url) so readiness and liveness cache independently. Liveness is
#: cached too: _diarizer_current calls sidecar_ready() then sidecar_healthy() to tell
#: "unreachable" from "up but unprovisioned", and load_model() probes liveness again — so
#: the failure path pays the timeout three times over without this.
_ready_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_ready_cache_lock = threading.Lock()

#: /healthz BODIES, cached on the same window as the boolean probes above. Separate dict
#: because the value is a dict, not a bool, and widening _ready_cache's type would make
#: every boolean call site pay an isinstance check for no benefit.
_status_cache: dict[str, tuple[float, dict]] = {}


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
        _status_cache.clear()


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

    Used to attach a REASON to a readiness failure, to resolve the LOADED device list
    (:func:`sidecar_supports_cpu_device`), and by :func:`sidecar_diagnostics` for the admin
    stats payload. Never used to decide readiness: the status code carries that, and parsing
    a body to make a routing decision would reintroduce the coupling ``/readyz`` exists to
    remove.

    TTL-cached on the same window as the boolean probes. It was not, and
    ``sidecar_diagnostics`` is reached from ``GET /admin/stats`` — so an unreachable sidecar
    cost a full 5 s connect timeout, held in the endpoint's threadpool, on every stats
    request. The boolean probes beside it were already cached, which is precisely why this
    one being uncached was easy to miss.
    """
    import json

    url = (base_url or _DEFAULT_URL).rstrip("/")
    with _ready_cache_lock:
        hit = _status_cache.get(url)
        if hit is not None and time.monotonic() - hit[0] < _READY_CACHE_TTL_S:
            return hit[1]
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
            f"{url}/healthz", timeout=5
        ) as resp:
            body = json.loads(resp.read())
            # A 0.2.0 sidecar answers /healthz with the bare string "ok", so the body is
            # not guaranteed to be an object. A non-dict means "no reason available".
            result = body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001 — a missing reason must never fail the caller
        result = {}
    # Stamp AFTER the request, never before — same reasoning as _cached_probe: sampling the
    # clock first would bill the timeout against the entry's own lifetime and it could be
    # born already expired.
    with _ready_cache_lock:
        _status_cache[url] = (time.monotonic(), result)
    return result


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

#: ``effective`` when strict mode (``engine.diarizer_require_sidecar``) is on and the sidecar
#: cannot serve: no engine runs (issue #1184). Not a backend, so not in the table above, which
#: is keyed by ``engine.backends.VALID_DIARIZER_BACKENDS`` exactly.
_UNAVAILABLE = "unavailable"
_UNAVAILABLE_DESCRIPTION = (
    "none — diar-native cannot serve and the PyAnnote fallback is refused "
    "(engine.diarizer_require_sidecar); diarization tasks fail until it recovers"
)


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
        ``effective_description``, ``using_fallback`` (True exactly when the work is running
        on PyAnnote although native was configured — the signal issue #672 exists to
        surface), ``require_sidecar`` (strict mode is on) and ``refusing`` (strict mode is on
        and the sidecar cannot serve, so ``effective`` is ``"unavailable"`` and diarization
        tasks fail, issue #1184).
    """
    from app.transcription.config import TranscriptionConfig

    configured = TranscriptionConfig._resolve_diarizer_backend()
    effective = configured
    required = diarizer_require_sidecar()
    refusing = False
    if configured == "native" and not sidecar_ready():
        # Strict mode refuses the fallback, so nothing serves: say that, not "pyannote".
        refusing = required
        effective = _UNAVAILABLE if required else "pyannote"

    return {
        "configured": configured,
        "configured_description": _ENGINE_DESCRIPTIONS.get(
            configured, f"{configured} diarization engine"
        ),
        "effective": effective,
        "effective_description": (
            _UNAVAILABLE_DESCRIPTION
            if refusing
            else _ENGINE_DESCRIPTIONS.get(effective, f"{effective} diarization engine")
        ),
        "using_fallback": effective == "pyannote" and configured != "pyannote",
        # Issue #1184: whether strict mode is on, and whether it is currently refusing work.
        "require_sidecar": required,
        "refusing": refusing,
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
    return is_truthy(str(raw))
