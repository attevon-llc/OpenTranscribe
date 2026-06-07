"""
Protocol, adapters, and multi-model runner for speaker analysis pipelines.

Provides a unified interface for running one or more speaker analysis models
(embedding extraction, gender detection) on the same audio segments. Audio
is loaded once by I/O threads and fed to each model back-to-back.

Three operational modes:
  Mode 1 — Embedding only: MultiModelRunner([EmbeddingModelAdapter(...)])
  Mode 2 — Gender only:    MultiModelRunner([GenderModelAdapter(...)])
  Mode 3 — Combined:       MultiModelRunner([EmbeddingModelAdapter(...), GenderModelAdapter(...)])
"""

import logging
from dataclasses import dataclass
from typing import Any
from typing import Protocol
from typing import cast
from typing import runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class SegmentResult:
    """Result from a single model processing a single audio segment."""

    model_name: str
    speaker_id: int
    value: Any  # np.ndarray for embeddings, tuple[str, float] for gender


@runtime_checkable
class SpeakerAnalysisModel(Protocol):
    """Interface for speaker analysis models.

    Both embedding and gender models accept the same input (1-D float32
    numpy audio at 16kHz) and produce per-speaker results. The only
    difference is the output shape.
    """

    name: str
    min_segment_duration: float

    def process_audio(self, audio_np: np.ndarray, sample_rate: int) -> Any | None:
        """Run inference on a single audio segment.

        Args:
            audio_np: 1-D float32 numpy array at sample_rate Hz.
            sample_rate: Sample rate of audio_np.

        Returns:
            Model-specific result, or None on failure.
        """
        ...

    def cleanup(self) -> None:
        """Release model resources."""
        ...


class EmbeddingModelAdapter:
    """Wraps SpeakerEmbeddingService for the SpeakerAnalysisModel protocol.

    Converts numpy audio → torch tensor → PyAnnote inference → L2-normalized
    embedding vector.
    """

    name = "embedding"
    min_segment_duration = 1.0

    def __init__(self, embedding_service: Any) -> None:
        self._service = embedding_service

    def process_audio(self, audio_np: np.ndarray, sample_rate: int) -> np.ndarray | None:
        """Extract L2-normalized embedding from audio segment."""
        import torch

        try:
            waveform = torch.from_numpy(audio_np).unsqueeze(0)  # [1, samples]
            return cast(
                "np.ndarray | None",
                self._service.extract_embedding_from_waveform(waveform, sample_rate),
            )
        except Exception as e:
            logger.debug("Embedding extraction failed: %s", e)
            return None

    def cleanup(self) -> None:
        self._service.cleanup()


class GenderModelAdapter:
    """Wraps SpeakerAttributeService for the SpeakerAnalysisModel protocol.

    Feeds numpy audio directly to wav2vec2 feature extractor → gender
    classification.
    """

    name = "gender"
    min_segment_duration = 1.0

    def __init__(self, attribute_service: Any) -> None:
        self._service = attribute_service

    def process_audio(self, audio_np: np.ndarray, sample_rate: int) -> tuple[str, float] | None:
        """Run gender classification on audio segment.

        Returns:
            (gender, confidence) tuple or None on failure.
        """
        if len(audio_np) < sample_rate:  # Skip clips under 1s
            return None
        try:
            return self._service._run_inference(audio_np)  # type: ignore[no-any-return]
        except Exception as e:
            logger.debug("Gender inference failed: %s", e)
            return None

    def cleanup(self) -> None:
        self._service.cleanup()


class MultiModelRunner:
    """Feeds one loaded audio segment to 1..N models back-to-back.

    Audio is loaded ONCE by I/O threads, then each model runs inference
    sequentially on the same numpy array. No re-fetch, no re-decode.
    """

    def __init__(self, models: list[SpeakerAnalysisModel]) -> None:
        if not models:
            raise ValueError("At least one model is required")
        self._models = models

    @property
    def model_names(self) -> list[str]:
        return [m.name for m in self._models]

    @property
    def min_segment_duration(self) -> float:
        """Use the smallest threshold — both models get optimal input."""
        return min(m.min_segment_duration for m in self._models)

    def process_segment(
        self,
        audio_np: np.ndarray,
        sample_rate: int,
        speaker_id: int,
    ) -> list[SegmentResult]:
        """Run all models on a single audio segment.

        Args:
            audio_np: 1-D float32 numpy array (same reference for all models).
            sample_rate: Sample rate of audio_np.
            speaker_id: Database ID of the speaker.

        Returns:
            List of SegmentResult (one per model that succeeded).
        """
        results = []
        for model in self._models:
            value = model.process_audio(audio_np, sample_rate)
            if value is not None:
                results.append(
                    SegmentResult(
                        model_name=model.name,
                        speaker_id=speaker_id,
                        value=value,
                    )
                )
        return results

    def cleanup(self) -> None:
        """Release all model resources."""
        for model in self._models:
            try:
                model.cleanup()
            except Exception as e:
                logger.debug("Model cleanup failed for %s: %s", model.name, e)
