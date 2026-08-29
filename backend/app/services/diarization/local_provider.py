"""Local diarization provider -- wraps ModelManager.get_diarizer().

Runs local (on-GPU) speaker diarization via ModelManager.get_diarizer(), which uses
diar-native (the Rust/ONNX "speakrs" engine) as the primary engine and automatically
falls back to the in-process PyAnnote fork whenever the native sidecar is unavailable
or fails mid-job (see app/transcription/diarizer_native.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .base import DiarizationProvider
from .types import DiarizeConfig
from .types import DiarizeResult
from .types import DiarizeSegment

logger = logging.getLogger(__name__)


class LocalDiarizationProvider(DiarizationProvider):
    """Run local speaker diarization on GPU via diar-native, with automatic PyAnnote fallback."""

    @property
    def provider_name(self) -> str:
        return "local"

    def supports_speaker_count(self) -> bool:
        return True

    def validate_connection(self) -> tuple[bool, str, float]:
        """Check CUDA availability as a proxy for local diarization readiness."""
        start = time.time()
        try:
            import torch

            if torch.cuda.is_available():
                ms = (time.time() - start) * 1000
                return True, f"CUDA available ({torch.cuda.device_count()} GPU(s))", ms
            ms = (time.time() - start) * 1000
            return False, "CUDA not available -- local diarization requires GPU", ms
        except ImportError:
            ms = (time.time() - start) * 1000
            return False, "PyTorch not installed", ms

    def diarize(
        self,
        audio_path: str,
        config: DiarizeConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> DiarizeResult:
        """Run local diarization using the model manager (diar-native, PyAnnote fallback).

        Args:
            audio_path: Path to the audio file on disk.
            config: Diarization configuration (speaker counts, etc.).
            progress_callback: Optional callback for progress updates.

        Returns:
            DiarizeResult with segments, speaker count, and native metadata.
        """
        from app.core.config import settings
        from app.transcription.audio import load_audio
        from app.transcription.config import TranscriptionConfig
        from app.transcription.model_manager import ModelManager

        if progress_callback:
            progress_callback(0.1, "Loading diarization model")

        trans_config = TranscriptionConfig.from_environment(
            min_speakers=config.min_speakers,
            max_speakers=config.max_speakers,
            num_speakers=config.num_speakers,
            hf_token=settings.HUGGINGFACE_TOKEN,
        )

        audio = load_audio(audio_path)
        manager = ModelManager.get_instance()
        diarizer = manager.get_diarizer(trans_config)

        if progress_callback:
            progress_callback(0.3, "Running speaker diarization")

        diarize_df, overlap_info, native_embeddings = diarizer.diarize(audio)

        # Convert DiarizeResult to DiarizeSegment list
        import numpy as np

        segments: list[DiarizeSegment] = []
        for s, e, sp in zip(diarize_df.start, diarize_df.end, diarize_df.speaker, strict=True):
            segments.append(
                DiarizeSegment(
                    start=float(s),
                    end=float(e),
                    speaker=self._normalize_speaker_label(str(sp)) or "SPEAKER_00",
                )
            )

        unique_speakers = int(np.unique(diarize_df.speaker).size) if len(diarize_df) > 0 else 0

        if progress_callback:
            progress_callback(1.0, "Diarization complete")

        result = DiarizeResult(
            segments=segments,
            num_speakers=unique_speakers,
            provider_name="local",
            model_name="pyannote/speaker-diarization-community-1",
        )
        # Attach native objects as metadata for downstream pipeline compatibility
        # (e.g. assign_speakers(), embedding extraction)
        result.metadata["native_embeddings"] = native_embeddings
        result.metadata["overlap_info"] = overlap_info
        result.metadata["diarize_df"] = diarize_df
        return result
