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

from app.core.constants import DIAR_SIDECAR_MIN_RTF
from app.core.constants import DIAR_SIDECAR_TIMEOUT_FLOOR_S
from app.core.scratch_paths import DIAR_NATIVE_SHARED_DIR_DEFAULT
from app.transcription.diar_shared_volume import _ENGINE_SHARED_DIR
from app.transcription.diar_shared_volume import _log_reuse_decision
from app.transcription.diar_shared_volume import _path_is_on_shared_volume
from app.transcription.diar_sidecar_status import DiarSidecarUnavailableError
from app.transcription.diar_sidecar_status import _classify_sidecar_failure
from app.transcription.diar_sidecar_status import default_base_url
from app.transcription.diar_sidecar_status import diarizer_require_sidecar
from app.transcription.diar_sidecar_status import sidecar_healthy
from app.transcription.diar_sidecar_status import sidecar_ready
from app.transcription.diar_sidecar_status import sidecar_status
from app.transcription.diarize_result import DiarizeResult

if TYPE_CHECKING:
    from app.transcription.diarizer import SpeakerDiarizer

logger = logging.getLogger(__name__)

# The model family the sidecar exports its ONNX/PLDA graphs from (issue #706) — recorded
# alongside "native" as the resolved diarization_model whenever the sidecar actually served
# the request. Not a live query of the sidecar (no endpoint reports it); this mirrors the
# same community-1 export documented in app/transcription/CLAUDE.md's provisioning section.
NATIVE_MODEL_NAME = "pyannote/speaker-diarization-community-1"

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


class NativeSpeakerDiarizer:
    """SpeakerDiarizer-compatible client for the diar-native sidecar."""

    def __init__(self, config: Any, base_url: str | None = None):
        self.config = config
        self.base_url = (base_url or default_base_url()).rstrip("/")
        self.is_loaded = False
        self._fallback: SpeakerDiarizer | None = None
        # This object is shared by every task on the worker, so the fallback is too: it is
        # loaded on first use, counted while in use, and released by release_fallback() once
        # the sidecar serves again, never left resident beside it.
        self._fallback_lock = threading.Lock()
        self._fallback_users = 0
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
        #: Why the most recent diarize() ran on PyAnnote instead (issue #1184); None whenever
        #: the sidecar served it. Reset at every native return so it never goes stale.
        self.last_fallback_reason: str | None = None

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
        with self._fallback_lock:
            fallback = self._fallback
            if fallback is None:
                from app.transcription.diarizer import SpeakerDiarizer

                fallback = SpeakerDiarizer(self.config)
                fallback.load_model()
                self._fallback = fallback
            return fallback

    def release_fallback(self) -> bool:
        """Unload the in-process PyAnnote fallback if it is loaded and no job is using it.

        The fallback exists for a sidecar outage. Kept after one, it holds the PyAnnote
        pipeline in VRAM for the rest of the worker's life, beside the sidecar's own models,
        and every later job's ``embed_window`` keeps routing to it. On a card packed with
        concurrent jobs that VRAM is what the next Whisper decode needed. Returns True when
        something was released.
        """
        with self._fallback_lock:
            if self._fallback is None or self._fallback_users:
                return False
            fallback, self._fallback = self._fallback, None
        fallback.unload_model()
        from app.transcription.cuda_health import free_cached_vram

        free_cached_vram()
        return True

    # -- main entry points -------------------------------------------------

    def _post_own_copy(
        self, audio: np.ndarray, timeout: float, request_gender: bool = True
    ) -> tuple[dict, str]:
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
            return self._post_diarize(own_wav, timeout, request_gender), own_wav
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

    def _try_reused_wav(
        self, wav_path: str, timeout: float, request_gender: bool = True
    ) -> dict | None:
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
            return self._post_diarize(wav_path, timeout, request_gender)
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

    def _post_diarize(self, path: str, timeout: float, request_gender: bool = True) -> dict:
        """POST one /diarize request for a WAV the sidecar is expected to be able to open.

        Gender rides this call: the sidecar already has the decoded audio and the speaker
        turns in hand, so classifying costs no second fetch or decode. ``timeout`` is the
        duration-scaled per-request budget from :func:`_diarize_budget_s` (issue #656 Step 4),
        not the flat ``_TIMEOUT_S`` ceiling directly.

        ``request_gender`` is this job's answer from the privacy policy. It can only turn the
        deployment's ``DIAR_NATIVE_GENDER`` switch off for the job, never on, so a file whose
        owner or organization forbids inferring gender never has its audio classified.
        """
        return post_json(
            f"{self.base_url}/diarize",
            {
                "wav_path": path,
                "file_id": "job",
                "gender": _GENDER_ENABLED and request_gender,
            },
            timeout=timeout,
        )

    def _refuse_or_fallback_impl(
        self,
        exc: BaseException,
        audio: np.ndarray,
        allow_local_fallback: bool,
        *,
        return_centroids: bool | None = None,
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
        with self._fallback_lock:
            self._fallback_users += 1
        try:
            fallback_result = self._fallback_engine().diarize(
                audio, return_centroids=return_centroids
            )
        finally:
            with self._fallback_lock:
                self._fallback_users -= 1
        self.last_provider = "pyannote"
        self.last_fallback_reason = (
            exc.reason if isinstance(exc, DiarSidecarUnavailableError) else "error"
        )
        self.last_model = getattr(self._fallback, "_model_name", None)
        return fallback_result

    def _native_embeddings(
        self, out: dict, exclusive: list[dict], return_centroids: bool | None
    ) -> dict[str, np.ndarray]:
        """L2-normalized centroids for the speakers present in the exclusive diarization.

        Extracted out of :meth:`diarize` purely to keep that method's cyclomatic complexity
        under the repo's gate.
        """
        native_embeddings: dict[str, np.ndarray] = {}
        if getattr(self.config, "enable_native_embeddings", True) and return_centroids is not False:
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
        return native_embeddings

    def diarize(
        self,
        audio: np.ndarray,
        wav_path: str | None = None,
        *,
        allow_local_fallback: bool = True,
        skip_sidecar: bool = False,
        return_centroids: bool | None = None,
        request_gender: bool | None = None,
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
            return_centroids: This job's answer to "may speaker centroids leave the diarizer"
                (privacy policy). ``False`` returns none, whatever the config says.
            request_gender: This job's answer to "may the sidecar classify gender". ``False``
                keeps the audio unclassified. Both are PER CALL, not read from ``self.config``:
                the ModelManager shares one diarizer and the config it was built with across
                every job on the worker, so a per-job value held there is some earlier job's.
                Both can only restrict; ``None`` leaves the deployment's own switches alone.
        """
        if not self.is_loaded:
            raise RuntimeError("Diarizer not loaded. Call load_model() first.")
        want_gender = request_gender is not False
        if getattr(self.config, "num_speakers", None) is not None:
            logger.warning(
                "diarizer_backend=native: num_speakers=%s requested but the native engine "
                "runs auto speaker counting (constraint port pending); proceeding with auto.",
                self.config.num_speakers,
            )

        def _refuse_or_fallback(exc: BaseException) -> tuple[DiarizeResult, dict, dict | None]:
            return self._refuse_or_fallback_impl(
                exc, audio, allow_local_fallback, return_centroids=return_centroids
            )

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
                    out = self._try_reused_wav(str(wav_path), timeout, want_gender)
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
                    out, own_wav = self._post_own_copy(audio, timeout, want_gender)
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

        native_embeddings = self._native_embeddings(out, exclusive, return_centroids)

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
        self.last_fallback_reason = None
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
        fallback = self._fallback  # read once: release_fallback() may clear it concurrently
        if fallback is not None:
            # This job already fell back mid-diarization; keep the re-check on the same
            # engine that produced the segments.
            return fallback.embed_window(audio, start, end)
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
