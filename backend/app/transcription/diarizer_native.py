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

This file is standalone/additive: no existing module is modified. Wiring lives in
ModelManager behind ``TranscriptionConfig.diarizer_backend`` (see diar-native
docs/INSTALL_NATIVE.md).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
import wave
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import numpy as np

from app.transcription.diarize_result import DiarizeResult

if TYPE_CHECKING:
    from app.transcription.diarizer import SpeakerDiarizer

logger = logging.getLogger(__name__)

_DEFAULT_URL = os.environ.get("DIAR_NATIVE_URL", "http://diar-native:8701")
# Directory shared (bind/volume) between this worker and the sidecar container.
_SHARED_DIR = os.environ.get("DIAR_NATIVE_SHARED_DIR", "/tmp/diar-native")  # noqa: S108  # nosec B108 — container volume mount point, not a host temp file
_TIMEOUT_S = float(os.environ.get("DIAR_NATIVE_TIMEOUT_S", "3600"))
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
    """
    url = (base_url or _DEFAULT_URL).rstrip("/")
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
            f"{url}/healthz", timeout=5
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
    """
    url = (base_url or _DEFAULT_URL).rstrip("/")
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
            f"{url}/readyz", timeout=5
        ) as resp:
            return bool(resp.status == 200)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("diar-native sidecar has no /readyz (pre-0.3.0); falling back to /healthz")
            return sidecar_healthy(base_url)
        status = sidecar_status(base_url)
        logger.warning(
            "diar-native sidecar is up but not ready (HTTP %s, models_state=%s): %s",
            exc.code,
            status.get("models_state", "unknown"),
            status.get("models_reason", "no reason given"),
        )
        return False
    except Exception:  # noqa: BLE001 — unreachable for any reason means "not ready"
        return False


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
        self.base_url = (base_url or _DEFAULT_URL).rstrip("/")
        self.is_loaded = False
        self._fallback: SpeakerDiarizer | None = None

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

    def diarize(
        self, audio: np.ndarray
    ) -> tuple[DiarizeResult, dict, dict[str, np.ndarray] | None]:
        if not self.is_loaded:
            raise RuntimeError("Diarizer not loaded. Call load_model() first.")
        if getattr(self.config, "num_speakers", None) is not None:
            logger.warning(
                "diarizer_backend=native: num_speakers=%s requested but the native engine "
                "runs auto speaker counting (constraint port pending); proceeding with auto.",
                self.config.num_speakers,
            )

        step_start = time.perf_counter()
        os.makedirs(_SHARED_DIR, exist_ok=True)
        wav_path = os.path.join(_SHARED_DIR, f"diar_{uuid.uuid4().hex}.wav")
        try:
            # The WAV write shares the sidecar-loss fallback, not just the HTTP call:
            # a permission or disk-space failure on the shared scratch volume (e.g.
            # the volume landing root-owned on first creation, see PR history) is exactly
            # as recoverable as an unreachable sidecar — both mean "this job cannot
            # use diar-native right now" — and previously only the HTTP call was
            # covered, so a write failure propagated out of diarize() uncaught and
            # hard-failed the whole transcription instead of degrading to PyAnnote.
            try:
                clip = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
                pcm = (clip * 32767.0).astype("<i2")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(pcm.tobytes())

                out = post_json(
                    f"{self.base_url}/diarize",
                    # Gender rides this call: the sidecar has the decoded audio and the
                    # speaker turns in hand, so classifying costs no second fetch or decode.
                    {"wav_path": wav_path, "file_id": "job", "gender": _GENDER_ENABLED},
                    timeout=_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001 — a sidecar/scratch-volume loss must not fail the job
                logger.warning(
                    "diar-native /diarize failed mid-job (%s); falling back to PyAnnote", exc
                )
                return self._fallback_engine().diarize(audio)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(wav_path)

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
