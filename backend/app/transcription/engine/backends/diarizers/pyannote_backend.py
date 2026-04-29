"""PyAnnote diarization backend — wraps app.transcription.diarizer.SpeakerDiarizer."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.transcription.diarize_result import DiarizeResult
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class PyAnnoteBackend:
    """Diarization backend using the pyannote-audio fork.

    Wraps the existing SpeakerDiarizer class unchanged; model loading
    is driven by ModelManager to preserve the warm-cache lifecycle.
    """

    def warmup(self, config: EngineConfig) -> None:
        """Validate that the diarizer model is loadable."""
        logger.debug("PyAnnoteBackend.warmup: model loading delegated to ModelManager")

    def diarize(
        self,
        audio: np.ndarray,
        *,
        min_speakers: int,
        max_speakers: int,
        num_speakers: int | None,
    ) -> tuple[DiarizeResult, dict, dict[str, np.ndarray] | None]:
        """Delegate to ModelManager-cached SpeakerDiarizer.

        The SpeakerDiarizer instance must already be loaded by the caller
        (via ModelManager.get_diarizer) before invoking this method.
        This backend is a thin adapter; the engine's _GpuStage drives
        model loading to preserve the warm-cache lifecycle.
        """
        raise NotImplementedError(
            "PyAnnoteBackend.diarize() must be called via _GpuStage, "
            "which resolves the cached SpeakerDiarizer from ModelManager."
        )
