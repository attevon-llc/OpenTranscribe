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
        """Whether the engine actually serving local diarization honors speaker-count hints.

        Only the in-process PyAnnote fork does: it passes num_speakers/min_speakers/
        max_speakers into the pipeline. The native sidecar runs community-1 auto speaker
        counting — ``NativeSpeakerDiarizer.diarize`` (``diarizer_native.py``) logs a warning
        and ignores an explicit ``num_speakers``, and silently drops min/max entirely — so
        answering True unconditionally overstated what a caller could rely on.
        """
        from app.transcription.config import TranscriptionConfig

        return TranscriptionConfig._resolve_diarizer_backend() != "native"

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
        """Run this server's speaker detection (diar-native, PyAnnote fallback).

        The post stage (``rediarize_task``, plan engine ``local``) runs through here.

        Args:
            audio_path: Path to the audio file on disk.
            config: Speaker counts, and optionally a WAV the native sidecar can open
                directly (``sidecar_wav_path``).
            progress_callback: Optional callback for progress updates.

        Returns:
            DiarizeResult whose ``provider_name``/``model_name`` name the engine that
            ACTUALLY ran (``native`` or ``pyannote``, read off the diarizer after any
            fallback, never the configured value), with the native objects the post stage
            reuses in ``metadata``: ``diarize_df`` (the frame ``assign_speakers`` reads),
            ``overlap_info`` and ``native_embeddings`` (per-speaker vectors, free).
        """
        from app.transcription.audio import load_audio
        from app.transcription.config import TranscriptionConfig
        from app.transcription.engine.stages import _run_diarize
        from app.transcription.model_manager import ModelManager

        if progress_callback:
            progress_callback(0.1, "Loading diarization model")

        trans_config = TranscriptionConfig.from_environment(
            min_speakers=config.min_speakers,
            max_speakers=config.max_speakers,
            num_speakers=config.num_speakers,
            # Only ever a restriction: a job the privacy policy forbids neither returns
            # centroids nor has its audio classified for gender.
            **({} if config.return_centroids else {"enable_native_embeddings": False}),
            **({} if config.request_gender else {"request_gender": False}),
        )

        audio = load_audio(audio_path)
        manager = ModelManager.get_instance()
        diarizer = manager.get_diarizer(trans_config)

        if progress_callback:
            progress_callback(0.3, "Running speaker diarization")

        diarize_df, overlap_info, native_embeddings = _run_diarize(
            diarizer, audio, config.sidecar_wav_path, tc=trans_config
        )

        segments: list[DiarizeSegment] = [
            DiarizeSegment(
                start=float(s),
                end=float(e),
                speaker=self._normalize_speaker_label(str(sp)) or "SPEAKER_00",
            )
            for s, e, sp in zip(diarize_df.start, diarize_df.end, diarize_df.speaker, strict=True)
        ]

        if progress_callback:
            progress_callback(1.0, "Diarization complete")

        result = DiarizeResult(
            segments=segments,
            num_speakers=len({seg.speaker for seg in segments}),
            provider_name=str(getattr(diarizer, "last_provider", None) or "local"),
            model_name=str(getattr(diarizer, "last_model", None) or ""),
        )
        result.metadata["native_embeddings"] = native_embeddings
        result.metadata["overlap_info"] = overlap_info
        result.metadata["diarize_df"] = diarize_df
        return result
