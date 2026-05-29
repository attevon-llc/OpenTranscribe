"""WhisperX backend — alternative transcriber using whisperx library.

Phase 1 stub. Full implementation in Phase 1b when the whisperx backend
is needed for drift-prone languages (Thai, Cantonese, Vietnamese).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class WhisperxBackend:
    """Transcriber backend using whisperx library (stub)."""

    @property
    def supports_word_timestamps(self) -> bool:
        return True

    def warmup(self, config: EngineConfig) -> None:
        logger.warning("WhisperxBackend is a stub — not yet implemented")

    def transcribe(self, audio: np.ndarray, *, language: str | None) -> dict:
        raise NotImplementedError("WhisperxBackend is not yet implemented")
