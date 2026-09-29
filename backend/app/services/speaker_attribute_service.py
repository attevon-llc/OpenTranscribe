"""
Speaker attribute detection service.

Uses prithivMLmods/Common-Voice-Gender-Detection for gender prediction from audio.
This model (~380MB, Apache 2.0) is fine-tuned from wav2vec2-base-960h and achieves
98.46% accuracy on gender classification (female/male).

Model card: https://huggingface.co/prithivMLmods/Common-Voice-Gender-Detection
"""

import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "prithivMLmods/Common-Voice-Gender-Detection"

SAMPLE_RATE = 16000

# Longest clip the model ever sees (issue #1066). wav2vec2's activation memory grows with
# input length, and the callers hand it whole merged speaking turns, which run to minutes
# in a meeting: one process peaked at 1.7 GB on a 60 s clip, 3.6 GB on 180 s and 5.5 GB on
# 300 s, and a 6 GiB CPU worker was OOM-killed by two such tasks. The model was fine-tuned
# on Common Voice clips a few seconds long, so a 20 s window gives it plenty to classify.
DEFAULT_MAX_CLIP_SECONDS = 20.0


def max_clip_seconds() -> float:
    """``SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS``, falling back to the default on garbage."""
    raw = os.getenv("SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MAX_CLIP_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS=%r is not a number; using default", raw)
        return DEFAULT_MAX_CLIP_SECONDS
    if value < 2.0:
        logger.warning("SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS=%s is below 2 s; using default", raw)
        return DEFAULT_MAX_CLIP_SECONDS
    return value


def center_crop(
    audio_np: np.ndarray, max_seconds: float, sample_rate: int = SAMPLE_RATE
) -> np.ndarray:
    """Return at most ``max_seconds`` of ``audio_np``, taken from its middle.

    The middle rather than the head: a merged speaking turn starts and ends at the
    boundaries diarization is least sure about.
    """
    max_samples = int(max_seconds * sample_rate)
    if len(audio_np) <= max_samples:
        return audio_np
    start = (len(audio_np) - max_samples) // 2
    return audio_np[start : start + max_samples]


# Label mapping: model index → gender string
GENDER_ID2LABEL = {0: "female", 1: "male"}


class SpeakerAttributeService:
    """Predicts speaker gender from audio using wav2vec2 sequence classification."""

    def __init__(self, force_cpu: bool = False) -> None:
        self._model: Any | None = None
        self._feature_extractor: Any | None = None
        self._model_loaded = False
        self._device: str = "cpu"
        self._force_cpu = force_cpu

    def load_models(self) -> None:
        """Lazy-load the gender model from HuggingFace (cached after first run).

        Uses GPU if available (and not force_cpu) for faster inference.
        The model is small (~380MB) and fits alongside WhisperX on GPU.
        """
        if self._model_loaded:
            return

        try:
            import torch
            from transformers import Wav2Vec2FeatureExtractor
            from transformers import Wav2Vec2ForSequenceClassification

            from app.utils.hf_hub_offline import hf_offline_requested

            from_pretrained_kwargs: dict[str, Any] = {}
            if hf_offline_requested():
                from_pretrained_kwargs["local_files_only"] = True

            # nosec B615 - MODEL_NAME is a fixed, trusted repo (Apache-2.0, ~380MB)
            # pre-downloaded into the controlled MODEL_CACHE_DIR by scripts/download-models.py;
            # not user-controlled. Same posture as the diarizer/embedding from_pretrained calls.
            self._feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(  # nosec B615
                MODEL_NAME, **from_pretrained_kwargs
            )
            self._model = Wav2Vec2ForSequenceClassification.from_pretrained(  # nosec B615
                MODEL_NAME, **from_pretrained_kwargs
            )
            self._model.eval()

            # Use GPU only on GPU workers (PRELOAD_GPU_MODELS=true).
            # On CPU workers, this model would leak a CUDA context (~5GB)
            # in the prefork child process that never gets released.
            import os

            is_gpu_worker = os.environ.get("PRELOAD_GPU_MODELS", "").lower() == "true"
            if not self._force_cpu and is_gpu_worker and torch.cuda.is_available():
                self._device = "cuda"
                self._model = self._model.to(self._device)
                logger.info(f"Gender model loaded on GPU: {MODEL_NAME}")
            else:
                self._device = "cpu"
                logger.info(f"Gender model loaded on CPU: {MODEL_NAME}")

            self._model_loaded = True

        except Exception as e:
            logger.error(f"Failed to load gender model: {e}")
            raise

    @staticmethod
    def _load_audio_ffmpeg(audio_path: str, target_sr: int = 16000) -> np.ndarray:
        """Load audio to float32 numpy array at target_sr via ffmpeg.

        Delegates to audio_segment_utils.load_full_audio_np().
        """
        from app.services.audio_segment_utils import load_full_audio_np

        return load_full_audio_np(audio_path, target_sr)

    def _run_inference(self, audio_np: np.ndarray) -> tuple[str, float]:
        """Run model inference on a 1-D float32 audio array at 16kHz.

        Returns:
            (gender, confidence) where gender is "female" or "male".
        """
        if not self._model_loaded:
            self.load_models()

        import torch

        audio_np = center_crop(audio_np, max_clip_seconds())
        inputs = self._feature_extractor(  # type: ignore[misc]
            audio_np,
            sampling_rate=16000,
            return_tensors="pt",
            padding=True,
        )

        # Move inputs to same device as model
        if self._device != "cpu":
            inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.inference_mode():
            logits = self._model(**inputs).logits  # type: ignore[misc]

        probs = torch.nn.functional.softmax(logits, dim=1).squeeze().cpu().numpy()
        predicted_id = int(np.argmax(probs))
        gender = GENDER_ID2LABEL.get(predicted_id, "male")
        confidence = float(probs[predicted_id])
        return gender, confidence

    def cleanup(self) -> None:
        """Release model resources and free GPU memory."""
        self._model = None
        self._feature_extractor = None
        self._model_loaded = False

        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        logger.info("Speaker attribute models released")


# Module-level cached instance
_cached_service: SpeakerAttributeService | None = None


def _return_freed_memory_to_os() -> None:
    """Ask glibc to hand freed heap back to the kernel.

    Without it, a CPU worker process keeps the high-water mark of the model and its
    activations as RSS long after both are freed, which is what the container's memory
    limit is measured against. No-op where glibc isn't the allocator.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def release_cached_attribute_service() -> None:
    """Drop the process-wide cached model and return its memory.

    CPU workers call this after each detection (issue #1066): every prefork child that
    has ever run the task otherwise keeps its own ~0.7 GB copy, so a pool of 8 idles at
    ~5.6 GB. Reloading from the local HF cache costs a few seconds per task.
    """
    global _cached_service
    if _cached_service is None:
        return
    service, _cached_service = _cached_service, None
    service.cleanup()
    _return_freed_memory_to_os()


def get_cached_attribute_service(force_cpu: bool = False) -> SpeakerAttributeService:
    """Get or create a cached SpeakerAttributeService instance.

    The model is loaded once and kept warm in GPU memory between tasks.
    Subsequent calls return the same instance, avoiding model reload overhead.
    """
    global _cached_service
    if _cached_service is None:
        _cached_service = SpeakerAttributeService(force_cpu=force_cpu)
        _cached_service.load_models()
    return _cached_service
