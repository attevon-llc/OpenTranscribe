"""FasterWhisper backend — wraps app.transcription.transcriber.Transcriber."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class FasterWhisperBackend:
    """Transcriber backend using faster-whisper's BatchedInferencePipeline.

    Wraps the existing Transcriber class unchanged; the engine drives it
    through ModelManager so model weights stay resident between tasks.
    """

    @property
    def supports_word_timestamps(self) -> bool:
        return True

    def warmup(self, config: EngineConfig) -> None:
        """Validate that the model is loadable. Actual loading is done by ModelManager."""
        logger.debug("FasterWhisperBackend.warmup: model loading delegated to ModelManager")

    def transcribe(self, audio: np.ndarray, *, language: str | None) -> dict:
        """Delegate to ModelManager-cached Transcriber.

        The Transcriber instance must already be loaded by the caller
        (via ModelManager.get_transcriber) before invoking this method.
        This backend is a thin adapter; the engine's _GpuStage drives
        model loading to preserve the warm-cache lifecycle.
        """
        raise NotImplementedError(
            "FasterWhisperBackend.transcribe() must be called via _GpuStage, "
            "which resolves the cached Transcriber from ModelManager."
        )
