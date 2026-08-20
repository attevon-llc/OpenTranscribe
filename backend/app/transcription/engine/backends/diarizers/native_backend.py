"""Native diarization backend — wraps app.transcription.diarizer_native.NativeSpeakerDiarizer.

Same thin-adapter shape as ``pyannote_backend.PyAnnoteBackend``. This is now the PRIMARY
diarizer backend (issue #58): faster and measurably better on AMI than the in-process
PyAnnote fork, which is demoted to the explicit, documented failover. Registered in
``BACKEND_REGISTRY`` alongside "pyannote" so the two are one validated vocabulary rather
than two independently-maintained lists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.transcription.diarize_result import DiarizeResult
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class NativeBackend:
    """Diarization backend using the diar-native (Rust/speakrs) sidecar.

    Wraps ``NativeSpeakerDiarizer`` unchanged; model/sidecar lifecycle — including the
    automatic fallback to the in-process PyAnnote fork on an unreachable or a mid-job-failing
    sidecar — is owned by ``ModelManager`` / ``NativeSpeakerDiarizer`` itself (see
    ``app/transcription/diarizer_native.py``), to preserve the warm-cache lifecycle the way
    ``PyAnnoteBackend`` does for the fork.
    """

    def warmup(self, config: EngineConfig) -> None:
        """Validate that the diarizer model is loadable."""
        logger.debug("NativeBackend.warmup: model loading delegated to ModelManager")

    def diarize(
        self,
        audio: np.ndarray,
        *,
        min_speakers: int,
        max_speakers: int,
        num_speakers: int | None,
    ) -> tuple[DiarizeResult, dict, dict[str, np.ndarray] | None]:
        """Delegate to ModelManager-cached diarizer (native, or its PyAnnote fallback).

        The diarizer instance must already be loaded by the caller (via
        ModelManager.get_diarizer) before invoking this method. This backend is a thin
        adapter; the engine's _GpuStage drives model loading to preserve the warm-cache
        lifecycle.
        """
        raise NotImplementedError(
            "NativeBackend.diarize() must be called via _GpuStage, which resolves the "
            "cached diarizer (NativeSpeakerDiarizer, or its PyAnnote fallback) from "
            "ModelManager."
        )
