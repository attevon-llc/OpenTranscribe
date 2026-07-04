"""Local ASR provider — wraps the existing TranscriptionPipeline (zero changes)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult

logger = logging.getLogger(__name__)


class LocalASRProvider(ASRProvider):
    """Thin wrapper around the existing GPU-based TranscriptionPipeline.

    The actual transcription logic lives in the pipeline — this class exists
    purely for interface completeness and connection testing.
    """

    @property
    def provider_name(self) -> str:
        return "local"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        # Local faster-whisper supports hotwords / initial_prompt
        return True

    def supports_translation(self) -> bool:
        return True

    def validate_connection(self) -> tuple[bool, str, float]:
        start = time.time()
        try:
            import torch

            if torch.cuda.is_available():
                name = torch.cuda.get_device_name(0)
                ms = (time.time() - start) * 1000
                return True, f"GPU available: {name}", ms
            ms = (time.time() - start) * 1000
            return True, "CPU mode (no GPU detected)", ms
        except ImportError:
            ms = (time.time() - start) * 1000
            return True, "Local provider available (torch not imported yet)", ms

    def transcribe(
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        """Not called directly — core.py uses TranscriptionPipeline for local."""
        raise NotImplementedError(
            "LocalASRProvider.transcribe() is not called directly. "
            "core.py handles local transcription via TranscriptionPipeline."
        )
