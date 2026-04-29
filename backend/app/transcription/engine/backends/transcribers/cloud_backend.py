"""Cloud ASR backend — adapter for existing cloud ASR providers.

Phase 1 stub. Full implementation in Phase 1b when the cloud-ASR split
path is wired into the Celery chain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class CloudBackend:
    """Transcriber backend using external cloud ASR providers (stub)."""

    @property
    def supports_word_timestamps(self) -> bool:
        return True  # depends on provider, but cloud results include timestamps

    def warmup(self, config: EngineConfig) -> None:
        logger.debug("CloudBackend.warmup: no-op (remote provider)")

    def transcribe(self, audio: np.ndarray, *, language: str | None) -> dict:
        raise NotImplementedError("CloudBackend is not yet implemented")
