"""Native diarization engine client (diar-native sidecar) — drop-in for SpeakerDiarizer.

Speaks to the diar-server sidecar (Rust/speakrs engine; see /mnt/nvm/repos/diar-native).
Implements the same surface as SpeakerDiarizer.diarize()/embed_window() so callers are
untouched. Selection is env-gated (DIARIZER_ENGINE=native) via ModelManager — the pyannote
fork path stays the default and the automatic fallback.

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
ModelManager behind DIARIZER_ENGINE (see diar-native docs/INSTALL_NATIVE.md).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import time
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


def _post_json(url: str, payload: dict, timeout: float) -> dict:
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
        self.base_url = (base_url or _DEFAULT_URL).rstrip("/")
        self.is_loaded = False
        self._fallback: SpeakerDiarizer | None = None

    # -- lifecycle ---------------------------------------------------------

    def load_model(self) -> None:
        """Health-check the sidecar (weights live server-side; nothing loads here)."""
        try:
            with urllib.request.urlopen(  # noqa: S310  # nosec B310 — internal service
                f"{self.base_url}/healthz", timeout=10
            ) as resp:
                self.is_loaded = resp.status == 200
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"diar-native sidecar unavailable at {self.base_url}: {exc}"
            ) from exc
        if not self.is_loaded:
            raise RuntimeError(f"diar-native sidecar unhealthy at {self.base_url}")
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
                "DIARIZER_ENGINE=native: num_speakers=%s requested but the native engine "
                "runs auto speaker counting (constraint port pending); proceeding with auto.",
                self.config.num_speakers,
            )

        step_start = time.perf_counter()
        os.makedirs(_SHARED_DIR, exist_ok=True)
        wav_path = os.path.join(_SHARED_DIR, f"diar_{uuid.uuid4().hex}.wav")
        try:
            clip = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
            pcm = (clip * 32767.0).astype("<i2")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm.tobytes())

            try:
                out = _post_json(
                    f"{self.base_url}/diarize",
                    {"wav_path": wav_path, "file_id": "job"},
                    timeout=_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001 — a sidecar loss must not fail the job
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

        logger.info(
            "native diarization done in %.1fs: %d segments, %d speakers",
            time.perf_counter() - step_start,
            len(diarize_df),
            out.get("num_speakers", len(native_embeddings)),
        )
        return diarize_df, overlap_info, native_embeddings

    def embed_window(self, audio: np.ndarray, start: float, end: float) -> np.ndarray | None:
        """Same padding + never-raise semantics as SpeakerDiarizer.embed_window."""
        if self._fallback is not None:
            # This job already fell back mid-diarization; keep the re-check on the same
            # engine that produced the segments.
            return self._fallback.embed_window(audio, start, end)
        try:
            sr = 16000
            min_samples = int(0.8 * sr)
            s, e = int(start * sr), int(end * sr)
            if e - s < min_samples:
                mid = (s + e) // 2
                s, e = mid - min_samples // 2, mid + min_samples // 2
            s, e = max(0, s), min(len(audio), e)
            if e - s < min_samples // 2:
                return None
            clip = np.ascontiguousarray(audio[s:e]).astype("<f4")
            out = _post_json(
                f"{self.base_url}/embed_window",
                {"samples_b64": base64.b64encode(clip.tobytes()).decode()},
                timeout=60,
            )
            return np.asarray(out["embedding"], dtype=np.float32).reshape(-1)
        except Exception as exc:  # noqa: BLE001 — never break diarization on a re-check embed
            logger.debug("native embed_window failed (%s); skipping", exc)
            return None
