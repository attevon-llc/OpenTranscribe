"""
Speaker attribute detection service.

Uses prithivMLmods/Common-Voice-Gender-Detection for gender prediction from audio.
This model (~380MB, Apache 2.0) is fine-tuned from wav2vec2-base-960h and achieves
98.46% accuracy on gender classification (female/male).

Model card: https://huggingface.co/prithivMLmods/Common-Voice-Gender-Detection
"""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "prithivMLmods/Common-Voice-Gender-Detection"

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
