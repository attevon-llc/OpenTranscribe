"""PyAnnote v4 speaker diarization.

Direct PyAnnote v4 API usage for speaker diarization with configurable
speaker count parameters.
"""

import gc
import logging
import os
import time
from typing import Any
from typing import ClassVar
from typing import NoReturn

import numpy as np
import torch

from app.transcription.config import TranscriptionConfig
from app.transcription.diarize_result import DiarizeResult
from app.utils.pyannote_utils import build_native_embeddings
from app.utils.pyannote_utils import extract_overlap_regions

logger = logging.getLogger(__name__)

# Retry sequence for segmentation batch_size on OOM
_BATCH_SIZE_RETRY_SEQUENCE = [16, 8, 4, 2, 1]

PYANNOTE_V4_MODEL = "pyannote/speaker-diarization-community-1"
PYANNOTE_V3_FALLBACK = "pyannote/speaker-diarization-3.1"

# Bounds a stalled Hub round trip (DNS retry storms observed against
# huggingface.co, ~23-30s per retry cycle) rather than the pipeline load itself,
# which also builds the model graph on CPU before any GPU move — larger budget
# than the reranker/embedding models. See app/utils/hf_hub_offline.py.
_PIPELINE_LOAD_TIMEOUT_S = 60.0


class SpeakerDiarizer:
    """PyAnnote v4 speaker diarization."""

    #: Fixed identity for the in-process engine (issue #706) — unlike NativeSpeakerDiarizer
    #: there is no internal failover here, so the provider never changes call to call.
    last_provider = "pyannote"

    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self._pipeline: Any = None
        self._model_name: str | None = None

    @property
    def last_model(self) -> str | None:
        """The model actually loaded (community-1, or the v3.1 fallback) — set by load_model()."""
        return self._model_name

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    def load_model(self) -> None:
        """Load the PyAnnote diarization pipeline."""
        from pyannote.audio import Pipeline

        from app.utils.hf_hub_offline import force_offline_if_requested
        from app.utils.hf_hub_offline import load_with_timeout

        step_start = time.perf_counter()

        logger.info(f"Loading PyAnnote v4 pipeline: {PYANNOTE_V4_MODEL}")

        try:
            with force_offline_if_requested():
                self._pipeline = load_with_timeout(
                    lambda: Pipeline.from_pretrained(PYANNOTE_V4_MODEL, token=self.config.hf_token),
                    timeout=_PIPELINE_LOAD_TIMEOUT_S,
                    label=f"PyAnnote pipeline ({PYANNOTE_V4_MODEL})",
                )
            if self._pipeline is None:
                import os

                if os.getenv("HF_HUB_OFFLINE") == "1":
                    msg = (
                        f"PyAnnote model '{PYANNOTE_V4_MODEL}' returned None. "
                        "In offline mode, models must be pre-downloaded. "
                        "Ensure the model cache directory contains the PyAnnote models."
                    )
                else:
                    msg = (
                        f"PyAnnote model '{PYANNOTE_V4_MODEL}' returned None. "
                        "Ensure: 1) HUGGINGFACE_TOKEN is set in .env, "
                        "2) You accepted the gated model agreement for "
                        "speaker-diarization-community-1 "
                        "(https://huggingface.co/pyannote/speaker-diarization-community-1) "
                        "— that is the only repo this app is gated on; accepting the older "
                        "segmentation-3.0/speaker-diarization-3.1 agreements only helps the "
                        "internal last-resort fallback below, not this model, "
                        "3) Restart the containers."
                    )
                raise PermissionError(msg)
            self._model_name = PYANNOTE_V4_MODEL
            logger.info(f"Loaded PyAnnote v4 model: {PYANNOTE_V4_MODEL}")
        except Exception as e:
            logger.warning(
                f"Failed to load v4 model '{PYANNOTE_V4_MODEL}': {e}. "
                f"Trying fallback: {PYANNOTE_V3_FALLBACK}"
            )
            try:
                with force_offline_if_requested():
                    self._pipeline = load_with_timeout(
                        lambda: Pipeline.from_pretrained(
                            PYANNOTE_V3_FALLBACK, token=self.config.hf_token
                        ),
                        timeout=_PIPELINE_LOAD_TIMEOUT_S,
                        label=f"PyAnnote fallback pipeline ({PYANNOTE_V3_FALLBACK})",
                    )
                if self._pipeline is None:
                    import os

                    if os.getenv("HF_HUB_OFFLINE") == "1":
                        msg = (
                            f"PyAnnote fallback model '{PYANNOTE_V3_FALLBACK}' returned None. "
                            "In offline mode, models must be pre-downloaded. "
                            "Ensure the model cache directory contains the PyAnnote models."
                        )
                    else:
                        msg = (
                            f"PyAnnote fallback model '{PYANNOTE_V3_FALLBACK}' returned None. "
                            "This usually means your HuggingFace token lacks access to this gated model. "
                            "Please verify your HUGGINGFACE_TOKEN and accept the model agreement on HuggingFace."
                        )
                    raise PermissionError(msg)
                self._model_name = PYANNOTE_V3_FALLBACK
                logger.info(f"Loaded fallback model: {PYANNOTE_V3_FALLBACK}")
            except Exception as fallback_e:
                raise RuntimeError(
                    f"Could not load PyAnnote diarization model. "
                    f"Primary: {e}, Fallback: {fallback_e}"
                ) from e

        # Move to diarization device (may differ from transcription device in hybrid mode)
        device = torch.device(self.config.diarization_device)
        self._pipeline = self._pipeline.to(device)  # type: ignore[attr-defined]

        # Configure segmentation batch_size based on GPU VRAM or env override.
        # PyAnnote defaults to 32 which causes OOM on GPUs with ≤12GB VRAM.
        self._configure_segmentation_batch_size()

        # Embedding batch_size is pinned at 16 (Phase A finding: throughput
        # saturates at bs=16 on both CUDA and MPS; larger batches waste VRAM
        # for no speed gain). See docs/diarization-vram-profile/README.md.
        self._configure_embedding_batch_size()

        elapsed = time.perf_counter() - step_start
        logger.info(f"TIMING: diarizer model loaded in {elapsed:.3f}s on {device}")

    def _configure_segmentation_batch_size(self) -> None:
        """Set PyAnnote's segmentation batch_size based on GPU VRAM or env override.

        PyAnnote's pretrained pipeline defaults to batch_size=32 for the internal
        segmentation model. This causes OOM on GPUs with limited VRAM, especially
        when the WhisperX model is still resident in memory.
        """
        if self._pipeline is None:
            return

        # Check env override first
        env_batch = os.getenv("DIARIZATION_BATCH_SIZE")
        if env_batch is not None:
            try:
                batch_size = int(env_batch)
                self._pipeline.segmentation_batch_size = batch_size
                logger.info(f"Diarization segmentation batch_size set to {batch_size} (from env)")
                return
            except ValueError:
                logger.warning(
                    f"Invalid DIARIZATION_BATCH_SIZE='{env_batch}', using auto-detection"
                )

        # Auto-detect based on diarization device VRAM (may differ from transcription device)
        diar_device = self.config.diarization_device
        if diar_device == "cuda" and torch.cuda.is_available():
            total_memory = torch.cuda.get_device_properties(0).total_memory
            memory_gb = total_memory / (1024**3)

            if memory_gb >= 40:
                batch_size = 32  # A6000/A100 — keep default
            elif memory_gb >= 24:
                batch_size = 24
            elif memory_gb >= 16:
                batch_size = 16
            elif memory_gb >= 12:
                batch_size = 8  # RTX 3080 Ti — conservative, WhisperX still in memory
            else:
                batch_size = 4
        elif diar_device == "mps":
            batch_size = 8
        else:
            batch_size = 4  # CPU

        current = self._pipeline.segmentation_batch_size
        if batch_size != current:
            self._pipeline.segmentation_batch_size = batch_size
            logger.info(
                f"Diarization segmentation batch_size: {current} → {batch_size} "
                f"(auto-detected for {diar_device})"
            )
        else:
            logger.info(f"Diarization segmentation batch_size: {batch_size} (default OK)")

    # Pinned embedding batch size. Phase A (2026-04-20) measured identical
    # throughput for bs in {16, 32, 64, 128} at fp32: bs=16 takes 103 s on the
    # 2.2 h reference file; bs=128 takes 100 s. Going above 16 costs 3-7 GB of
    # VRAM for a <3 % speed gain. Speaker count + DER are invariant across
    # fp32 batch sizes. Hard-coding 16 means predictable ~1 GB diarization
    # footprint on every GPU, every run. See docs/diarization-vram-profile/.
    EMBEDDING_BATCH_SIZE: ClassVar[int] = 16

    def _configure_embedding_batch_size(self) -> None:
        """Pin the embedding batch size to 16. No VRAM-budget adjustment.

        Environment override `DIARIZATION_EMBEDDING_BATCH_SIZE` is still
        honored for research / debugging (e.g. the Phase A probe harness),
        but the supported production setting is the class constant.
        """
        if self._pipeline is None:
            return

        env_batch = os.getenv("DIARIZATION_EMBEDDING_BATCH_SIZE")
        if env_batch is not None:
            try:
                batch_size = int(env_batch)
            except ValueError:
                logger.warning(
                    f"Invalid DIARIZATION_EMBEDDING_BATCH_SIZE='{env_batch}'; "
                    f"falling back to {self.EMBEDDING_BATCH_SIZE}"
                )
                batch_size = self.EMBEDDING_BATCH_SIZE
        else:
            batch_size = self.EMBEDDING_BATCH_SIZE

        # Force the fork to use the exact value we ask for, bypassing its
        # auto-scaler (which kicks in for values <= 32 on cuda/mps).
        os.environ["PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE"] = str(batch_size)
        self._pipeline.embedding_batch_size = batch_size
        logger.info(f"Diarization embedding batch_size: {batch_size} (pinned)")

    def diarize(
        self, audio: np.ndarray
    ) -> tuple[DiarizeResult, dict, dict[str, np.ndarray] | None]:
        """Run speaker diarization on audio.

        Args:
            audio: Audio waveform as 16kHz mono float32 numpy array.

        Returns:
            Tuple of:
                diarize_df: DataFrame with columns [segment, label, speaker, start, end]
                    Has attrs: overlaps, overlap_count, overlap_duration
                overlap_info: Dict with count, duration, regions keys.
                native_embeddings: Dict mapping speaker labels to L2-normalized
                    centroid vectors, or None if disabled/unavailable.

        Raises:
            RuntimeError: If model not loaded or diarization fails.
            PermissionError: If HuggingFace token lacks access.
        """
        if not self.is_loaded:
            raise RuntimeError("Diarizer model not loaded. Call load_model() first.")

        step_start = time.perf_counter()

        # Prepare audio input
        waveform = torch.from_numpy(audio)
        if waveform.ndim == 1:
            waveform = waveform.unsqueeze(0)
        audio_input = {"waveform": waveform, "sample_rate": 16000}

        # Build kwargs
        pipeline_kwargs = {}
        if self.config.num_speakers is not None:
            pipeline_kwargs["num_speakers"] = self.config.num_speakers
        else:
            pipeline_kwargs["min_speakers"] = self.config.min_speakers
            pipeline_kwargs["max_speakers"] = self.config.max_speakers

        logger.info(f"Running diarization with kwargs: {pipeline_kwargs}")

        # Sub-stage timing via hook callback
        stage_timing: dict[str, dict[str, float]] = {}

        def timing_hook(step_name, step_artefact, **kwargs):
            now = time.perf_counter()
            if step_name not in stage_timing:
                stage_timing[step_name] = {"start": now, "last": now}
            else:
                stage_timing[step_name]["last"] = now

        raw_output = self._run_pipeline_with_oom_retry(
            audio_input, pipeline_kwargs, hook=timing_hook
        )

        centroids = getattr(raw_output, "speaker_embeddings", None)
        output = raw_output

        # Extract diarization results (handles v4 output format)
        exclusive = self._extract_diarization(output, prefer_exclusive=True)
        full = self._extract_diarization(output, prefer_exclusive=False)

        tracks = list(exclusive.itertracks(yield_label=True))
        diarize_df = DiarizeResult(
            start=np.asarray([seg.start for seg, _, _ in tracks], dtype=np.float64),
            end=np.asarray([seg.end for seg, _, _ in tracks], dtype=np.float64),
            speaker=np.asarray([spk for _, _, spk in tracks], dtype=object),
        )

        # Extract overlap info (gated by config, shared utility)
        overlaps = (
            extract_overlap_regions(full, self.config.overlap_min_duration)
            if self.config.enable_overlap_detection
            else []
        )
        overlap_info = {"count": 0, "duration": 0.0, "regions": []}

        if overlaps:
            total_duration = sum(o["end"] - o["start"] for o in overlaps)
            overlap_info = {
                "count": len(overlaps),
                "duration": total_duration,
                "regions": overlaps,
            }
            logger.info(
                f"Detected {len(overlaps)} overlapping regions (total: {total_duration:.2f}s)"
            )

        # Build native embeddings from PyAnnote centroids (shared utility)
        native_embeddings = (
            build_native_embeddings(exclusive, centroids)
            if self.config.enable_native_embeddings
            else {}
        )

        # Log per-stage breakdown
        if stage_timing:
            stages = sorted(stage_timing.items(), key=lambda x: x[1].get("start", 0))
            breakdown = []
            for name, times in stages:
                duration = times["last"] - times["start"]
                breakdown.append(f"{name}={duration:.1f}s")
            logger.info(f"TIMING: diarization stages: {', '.join(breakdown)}")
            overlap_info["stage_timing"] = {
                name: round(times["last"] - times["start"], 3) for name, times in stages
            }

        elapsed = time.perf_counter() - step_start
        num_speakers = int(np.unique(diarize_df.speaker).size)
        logger.info(
            f"TIMING: diarization completed in {elapsed:.3f}s - "
            f"{num_speakers} speakers, {len(diarize_df)} segments"
        )

        return diarize_df, overlap_info, native_embeddings

    def embed_window(self, audio: np.ndarray, start: float, end: float) -> np.ndarray | None:
        """Embed an audio window with the pipeline's WeSpeaker model (issue #193 Phase 3).

        Used by the acoustic re-check to test which speaker a short disputed word actually
        sounds like. The window is padded to a minimum embeddable length (centered) because
        speaker embeddings of sub-second clips are unreliable. Returns a 256-d embedding,
        or None on any failure / if the model is unavailable (graceful — never raises into
        the pipeline).

        Args:
            audio: 16 kHz mono float32 waveform (the same array passed to diarize()).
            start: Window start (seconds).
            end: Window end (seconds).

        Returns:
            A 1-D embedding vector, or None.
        """
        try:
            import torch

            embedder = getattr(self._pipeline, "_embedding", None)
            if embedder is None or audio is None:
                return None
            sr = int(getattr(embedder, "sample_rate", 16000))
            # Center-pad to at least the model minimum (fallback ~0.8 s) so the embedding is
            # stable; the caller's cosine-margin guard rejects ambiguous matches.
            min_samples = int(getattr(embedder, "min_num_samples", 0) or 0) or int(0.8 * sr)
            s, e = int(start * sr), int(end * sr)
            if e - s < min_samples:
                mid = (s + e) // 2
                s, e = mid - min_samples // 2, mid + min_samples // 2
            s, e = max(0, s), min(len(audio), e)
            if e - s < min_samples // 2:
                return None
            clip = np.ascontiguousarray(audio[s:e]).astype(np.float32)
            # CPU tensor: the fork's embedding wrapper pins + transfers to its device.
            wf = torch.from_numpy(clip).view(1, 1, -1)
            out = embedder(wf)
            arr = out.detach().cpu().numpy() if hasattr(out, "detach") else np.asarray(out)
            return arr.reshape(-1)
        except Exception as exc:  # noqa: BLE001 — never break diarization on a re-check embed
            logger.debug("embed_window failed (%s); skipping acoustic re-check for window", exc)
            return None

    def _run_pipeline_with_oom_retry(self, audio_input: dict, pipeline_kwargs: dict, hook=None):
        """Run the diarization pipeline with automatic batch_size reduction on OOM.

        If the segmentation model hits an OOM error due to batch_size being too
        large, this method halves the batch_size and retries, down to batch_size=1.
        """
        assert self._pipeline is not None, "Pipeline not initialized"

        current_batch = self._pipeline.segmentation_batch_size

        # Build retry sequence: current value, then smaller values from the predefined list
        retry_sizes = [current_batch] + [s for s in _BATCH_SIZE_RETRY_SEQUENCE if s < current_batch]

        last_error: Exception | None = None
        for batch_size in retry_sizes:
            try:
                self._pipeline.segmentation_batch_size = batch_size
                call_kwargs = dict(pipeline_kwargs)
                if hook is not None:
                    call_kwargs["hook"] = hook
                raw_output = self._pipeline(audio_input, **call_kwargs)  # type: ignore[misc]
                if batch_size != current_batch:
                    logger.info(
                        f"Diarization succeeded with reduced batch_size={batch_size} "
                        f"(original: {current_batch})"
                    )
                return raw_output
            except Exception as e:
                if self._is_oom_error(e):
                    last_error = e
                    logger.warning(
                        f"Diarization OOM with batch_size={batch_size}, reducing and retrying..."
                    )
                    # Free GPU memory before retry
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    continue
                # Non-OOM error — don't retry
                self._handle_diarization_error(e)

        # All retries exhausted
        assert last_error is not None
        self._handle_diarization_error(last_error)

    @staticmethod
    def _is_oom_error(e: Exception) -> bool:
        """Check if an exception is a GPU out-of-memory error."""
        error_msg = str(e).lower()
        return (
            ("batch_size" in error_msg and "too large" in error_msg)
            or "out of memory" in error_msg
            or isinstance(e, torch.cuda.OutOfMemoryError)
        )

    def unload_model(self) -> None:
        """Release model memory."""
        if self._pipeline is not None:
            del self._pipeline
            self._pipeline = None
        logger.info("Diarizer model unloaded")

    def _extract_diarization(self, output, prefer_exclusive: bool = True):
        """Extract diarization annotation from PyAnnote output."""
        if prefer_exclusive and hasattr(output, "exclusive_speaker_diarization"):
            return output.exclusive_speaker_diarization
        elif hasattr(output, "speaker_diarization"):
            return output.speaker_diarization
        elif hasattr(output, "exclusive_speaker_diarization"):
            return output.exclusive_speaker_diarization
        else:
            return output

    def _handle_diarization_error(self, e: Exception) -> NoReturn:
        """Convert diarization errors to user-friendly messages."""
        error_msg = str(e)

        if self._is_oom_error(e):
            raise RuntimeError(
                "Speaker diarization ran out of GPU memory even at minimum batch size. "
                "This audio file may be too long for the available VRAM. "
                "Try setting DIARIZATION_BATCH_SIZE=1 in .env, or process shorter audio."
            ) from e

        if "401" in error_msg or "unauthorized" in error_msg.lower():
            raise PermissionError(
                "Cannot access PyAnnote speaker diarization models. "
                "Your HuggingFace token does not have access to the required gated model. "
                "You must accept the model agreement on HuggingFace: "
                "pyannote/speaker-diarization-community-1 "
                "(https://huggingface.co/pyannote/speaker-diarization-community-1). "
                "After accepting, wait 1-2 minutes, restart containers, and retry."
            ) from e

        if "403" in error_msg or "forbidden" in error_msg.lower():
            raise PermissionError(
                "Access forbidden to PyAnnote models. "
                "Your HuggingFace token may not have Read permissions, "
                "or you have not accepted the gated model agreement. "
                "Please verify your token at https://huggingface.co/settings/tokens "
                "and accept: pyannote/speaker-diarization-community-1."
            ) from e

        if (
            "cannot find the requested files" in error_msg.lower()
            or "locate the file on the hub" in error_msg.lower()
        ):
            raise PermissionError(
                "Cannot download PyAnnote models from HuggingFace. "
                "Please accept the model agreement at "
                "https://huggingface.co/pyannote/speaker-diarization-community-1, "
                "wait 1-2 minutes, then restart and retry."
            ) from e

        if "libcudnn" in error_msg.lower():
            raise RuntimeError(
                "CUDA cuDNN library compatibility error detected during diarization. "
                f"Technical details: {error_msg}"
            ) from e

        if "cuda" in error_msg.lower() or "gpu" in error_msg.lower():
            raise RuntimeError(
                f"GPU processing error during speaker diarization: {error_msg}"
            ) from e

        raise RuntimeError(f"Speaker diarization failed: {error_msg}") from e
