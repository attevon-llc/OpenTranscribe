"""Thin shim: delegates to Engine.process() for back-compat.

All transcription logic now lives in app.transcription.engine.
This file exists so existing imports of TranscriptionPipeline continue to work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.transcription.config import TranscriptionConfig

logger = logging.getLogger(__name__)


class TranscriptionPipeline:
    """Back-compat shim: delegates to Engine.process()."""

    def __init__(self, config: TranscriptionConfig):
        self.config = config
        from app.transcription.engine import Engine
        from app.transcription.engine import EngineConfig

        engine_config = EngineConfig()
        engine_config._transcription_config = config
        self._engine = Engine(engine_config)

    def process(
        self,
        audio_file_path: str,
        progress_callback: Callable[[float, str], None] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Full pipeline: delegates to Engine.process().

        Args:
            audio_file_path: Path to the audio file.
            progress_callback: Optional (progress, message) callback.
            task_id: Optional Celery task ID for VRAM profile storage.

        Returns:
            Dict with "segments", "language", optionally "overlap_info"
            and "native_speaker_embeddings" — same shape as before.
        """
        from app.transcription.engine import JobSpec

        job = JobSpec(
            audio_path=audio_file_path,
            task_id=task_id or "",
        )
        result = self._engine.process(job, progress_callback=progress_callback)
        return result.to_pipeline_dict()
