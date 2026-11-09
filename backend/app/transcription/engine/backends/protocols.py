"""Backend Protocol definitions for pluggable transcriber and diarizer backends."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Protocol
from typing import runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig


@runtime_checkable
class TranscriberBackend(Protocol):
    """Protocol for transcription backends."""

    def warmup(self, config: EngineConfig) -> None:
        """Pre-load model weights into VRAM. Called once at worker startup."""
        ...

    def transcribe(self, audio: np.ndarray, *, language: str | None) -> dict:
        """Run transcription on GPU.

        Args:
            audio: 16kHz mono float32 numpy array.
            language: ISO language code, or None for auto-detect.

        Returns:
            Dict with "segments" (list of dicts) and "language" (str).
        """
        ...

    @property
    def supports_word_timestamps(self) -> bool:
        """True if this backend returns per-word timestamps."""
        ...
