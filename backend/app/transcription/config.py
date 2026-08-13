"""Transcription pipeline configuration.

Builds configuration from environment variables and hardware detection,
with task-level overrides for per-file settings.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import ClassVar

logger = logging.getLogger(__name__)

# Models small enough to run efficiently on CPU (int8).
# These are routed to the CPU worker instead of GPU.
LIGHTWEIGHT_MODELS = frozenset({"tiny", "tiny.en", "base", "base.en"})

# Module-level guard so the CPU-mode misconfiguration warning fires at most
# once per worker process — without this, every transcription task would
# re-emit the same advice into the worker logs.
_CPU_MODE_WARNING_EMITTED = False


def _parse_optional_float(value: str) -> float | None:
    """Parse a string to float, returning None for empty/whitespace."""
    if not value or not value.strip():
        return None
    return float(value.strip())


@dataclass
class TranscriptionConfig:
    """Configuration for the transcription pipeline."""

    # Class-level pin: set once at worker startup, used for all subsequent tasks.
    # Prevents mid-flight model swaps when admin changes the DB setting.
    _pinned_model_name: ClassVar[str | None] = None

    model_name: str = "large-v3-turbo"
    compute_type: str = "float16"
    beam_size: int = 5
    batch_size: int = 16
    device: str = "cuda"  # transcription device
    diarization_device: str = "cuda"  # diarization device (differs in hybrid mode)
    device_index: int = 0
    source_language: str = "auto"
    translate_to_english: bool = False
    enable_dedup: bool = True
    min_speakers: int = 1
    max_speakers: int = 20
    num_speakers: int | None = None
    hf_token: str | None = None
    enable_native_embeddings: bool = True
    enable_diarization: bool = True  # False to skip PyAnnote entirely
    enable_overlap_detection: bool = True
    overlap_min_duration: float = 0.25

    # VAD settings (Silero VAD used by faster-whisper BatchedInferencePipeline)
    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 2000
    vad_min_speech_ms: int = 250
    vad_speech_pad_ms: int = 400

    # Accuracy settings
    hallucination_silence_threshold: float | None = None
    repetition_penalty: float = 1.0

    # Concurrent GPU model sharing (Phase 2)
    concurrent_requests: int = 1

    def config_hash(self) -> str:
        """Hash of model-loading-relevant config for cache invalidation.

        MD5 is a cache key, never a security control — which is exactly why
        ``usedforsecurity=False`` is required and not merely tidy: without it this call
        RAISES on a host whose OpenSSL enforces FIPS, and every transcription task
        fails at model load. Declaring it to the runtime also satisfies the linters,
        so the suppression comments this line used to carry are gone (matches
        ``services/search/hybrid_search_service.py``).
        """
        key = f"{self.model_name}:{self.compute_type}:{self.device}:{self.device_index}"
        return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:12]

    @classmethod
    def from_environment(cls, **overrides) -> "TranscriptionConfig":
        """Build config from env vars + hardware detection, with task-level overrides."""
        from app.utils.hardware_detection import detect_hardware

        hw = detect_hardware()
        whisperx_config = hw.get_whisperx_config()
        resolved_model = cls._resolve_model_name()

        # Hybrid mode: CPU transcription + GPU/MPS diarization.
        # Auto-activates when the GPU lacks VRAM to run the configured model (or on MPS).
        # Override via WHISPER_HYBRID_MODE=true|false|auto.
        hybrid = hw.should_use_hybrid_mode(resolved_model)
        if hybrid:
            hybrid_model = os.getenv("WHISPER_HYBRID_CPU_MODEL", "small")
            transcription_device = "cpu"
            transcription_compute = "int8"
            transcription_model = hybrid_model
            # Batch size for CPU: small at bs=4 is efficient; override-able
            batch_size_env = os.getenv("BATCH_SIZE", "auto")
            batch_size = 4 if batch_size_env == "auto" else int(batch_size_env)
            # Diarization stays on GPU/MPS (the actual hw.device)
            diarization_device = hw.device
            logger.info(
                "Hybrid mode active: transcription=CPU(%s), diarization=%s",
                transcription_model,
                diarization_device,
            )
        else:
            transcription_device = whisperx_config["device"]
            transcription_compute = os.getenv(
                "WHISPER_COMPUTE_TYPE", whisperx_config["compute_type"]
            )
            transcription_model = resolved_model
            diarization_device = hw.device
            batch_size_env = os.getenv("BATCH_SIZE", "auto")
            batch_size = (
                int(batch_size_env)
                if batch_size_env != "auto"
                else hw._get_optimal_batch_size(resolved_model)
            )

        # Base config from environment and hardware detection
        config = cls(
            model_name=transcription_model,
            compute_type=transcription_compute,
            beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "5")),
            batch_size=batch_size,
            device=transcription_device,
            diarization_device=diarization_device,
            device_index=whisperx_config.get("device_index", 0),
            source_language=os.getenv("SOURCE_LANGUAGE", "auto"),
            translate_to_english=False,
            enable_diarization=os.getenv("ENABLE_DIARIZATION", "true").lower() == "true",
            enable_dedup=os.getenv("ENABLE_SEGMENT_DEDUP", "true").lower() == "true",
            min_speakers=int(os.getenv("MIN_SPEAKERS", "1")),
            max_speakers=int(os.getenv("MAX_SPEAKERS", "20")),
            num_speakers=None,
            hf_token=os.getenv("HUGGINGFACE_TOKEN"),
            enable_native_embeddings=os.getenv("USE_NATIVE_SPEAKER_EMBEDDINGS", "true").lower()
            == "true",
            enable_overlap_detection=os.getenv("ENABLE_OVERLAP_DETECTION", "true").lower()
            == "true",
            overlap_min_duration=float(os.getenv("OVERLAP_MIN_DURATION", "0.25")),
            # VAD settings
            vad_threshold=float(os.getenv("VAD_THRESHOLD", "0.5")),
            vad_min_silence_ms=int(os.getenv("VAD_MIN_SILENCE_MS", "2000")),
            vad_min_speech_ms=int(os.getenv("VAD_MIN_SPEECH_MS", "250")),
            vad_speech_pad_ms=int(os.getenv("VAD_SPEECH_PAD_MS", "400")),
            # Accuracy settings
            hallucination_silence_threshold=_parse_optional_float(
                os.getenv("WHISPER_HALLUCINATION_THRESHOLD", "")
            ),
            repetition_penalty=float(os.getenv("WHISPER_REPETITION_PENALTY", "1.0")),
            concurrent_requests=cls._resolve_concurrent_requests(),
        )

        # Note: batch_size is NOT divided by concurrent_requests. CTranslate2
        # handles GPU compute scheduling internally, and reducing batch_size
        # increases kernel launch overhead without meaningful VRAM savings.
        # The GPU's SM scheduler naturally time-slices between concurrent tasks.

        # Apply task-level overrides (all overrides are intentional, including None
        # values like hallucination_silence_threshold=None meaning "disabled")
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)

        diar_info = (
            f"diarization_device={config.diarization_device}"
            if config.diarization_device != config.device
            else ""
        )
        logger.info(
            f"TranscriptionConfig: model={config.model_name}, device={config.device}"
            f"{' ' + diar_info if diar_info else ''}, "
            f"compute_type={config.compute_type}, batch_size={config.batch_size}, "
            f"beam_size={config.beam_size}, language={config.source_language}, "
            f"translate={config.translate_to_english}, "
            f"concurrent_requests={config.concurrent_requests}"
        )

        cls._maybe_warn_cpu_mode_misconfigured(config)

        return config

    @staticmethod
    def _maybe_warn_cpu_mode_misconfigured(config: "TranscriptionConfig") -> None:
        """Warn once when running on CPU with a heavy model or diarization on.

        PyAnnote diarization requires CUDA; running it on CPU will fail or
        be unusably slow. Whisper large-* on CPU runs >10x realtime. If we
        detect either condition, log a single advisory so admins can
        adjust ``WHISPER_MODEL`` / ``ENABLE_DIARIZATION`` in ``.env``.
        Does not block startup — the user may have intentional reasons.
        """
        global _CPU_MODE_WARNING_EMITTED
        if _CPU_MODE_WARNING_EMITTED or config.device != "cpu":
            return

        heavy_model = config.model_name not in LIGHTWEIGHT_MODELS
        if not heavy_model and not config.enable_diarization:
            return

        issues: list[str] = []
        if heavy_model:
            issues.append(
                f"WHISPER_MODEL={config.model_name} on CPU runs >10x realtime "
                "(recommend WHISPER_MODEL=base or small)"
            )
        if config.enable_diarization:
            issues.append(
                "ENABLE_DIARIZATION=true on CPU is not supported by PyAnnote "
                "(recommend ENABLE_DIARIZATION=false on CPU-only deployments)"
            )

        logger.warning(
            "CPU-only mode detected with a configuration intended for GPU: %s. "
            "Edit .env and restart workers to apply CPU-friendly defaults. "
            "See docs/CPU_MODE_TESTING.md for the full guidance.",
            "; ".join(issues),
        )
        _CPU_MODE_WARNING_EMITTED = True

    @classmethod
    def for_cpu_lightweight(cls, **overrides) -> "TranscriptionConfig":
        """Config for CPU-based lightweight transcription (base/tiny models).

        Uses int8 quantization for optimal CPU throughput. Diarization is
        disabled because PyAnnote requires CUDA.
        """
        model_name = os.getenv("WHISPER_LIGHTWEIGHT_MODEL", "base")
        config = cls(
            model_name=model_name,
            compute_type="int8",
            device="cpu",
            device_index=0,
            batch_size=4,
            beam_size=5,
            concurrent_requests=1,
            enable_diarization=False,
            enable_native_embeddings=False,
            enable_overlap_detection=False,
        )
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)

        logger.info(
            "TranscriptionConfig (CPU lightweight): model=%s, compute_type=%s, "
            "batch_size=%d, language=%s",
            config.model_name,
            config.compute_type,
            config.batch_size,
            config.source_language,
        )
        return config

    @classmethod
    def pin_model(cls, model_name: str) -> None:
        """Pin the model name after preloading at worker startup.

        Once pinned, all subsequent ``from_environment()`` calls use this value
        instead of re-reading the DB.  This prevents mid-flight model swaps when
        the admin changes the DB setting before restarting the worker — running
        tasks always use the model that's actually loaded in VRAM.

        The pin is reset on process restart (new worker reads fresh from DB).
        """
        cls._pinned_model_name = model_name
        logger.info("Model name pinned to '%s' for this worker process", model_name)

    @classmethod
    def _resolve_model_name(cls) -> str:
        """Resolve the Whisper model name: pinned value -> DB -> env -> default.

        Resolution order:
        1. Pinned value (set at worker startup after model preload)
        2. SystemSettings DB key ``asr.local_model`` (admin-set)
        3. WHISPER_MODEL environment variable
        4. Hardcoded default ``large-v3-turbo``

        The resolved name is passed through ``resolve_loadable_model_name`` so custom
        short names (e.g. ``crisperwhisper``) become the HuggingFace repo id that
        faster-whisper's ``WhisperModel`` can actually load. The pinned value is
        already resolved at startup, so the fast path skips re-resolution.
        """
        # Fast path: use pinned value from worker startup (no DB hit, already resolved)
        if cls._pinned_model_name is not None:
            return cls._pinned_model_name

        raw = os.getenv("WHISPER_MODEL", "large-v3-turbo")

        # Startup path: read from DB (first call before pin_model is called)
        try:
            from app.db.session_utils import session_scope
            from app.models.system_settings import SystemSettings

            with session_scope() as db:
                setting = (
                    db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
                )
                if setting and setting.value:
                    raw = str(setting.value)
        except Exception:  # noqa: S110  # nosec B110
            # DB not available (e.g., during testing, worker startup race) — fall back to env
            logger.debug("Could not read asr.local_model from DB, using env var")

        from app.services.asr.model_discovery import resolve_loadable_model_name

        return resolve_loadable_model_name(raw)

    @staticmethod
    def _resolve_concurrent_requests() -> int:
        """Resolve GPU_CONCURRENT_REQUESTS from env, with auto-detection."""
        raw = os.getenv("GPU_CONCURRENT_REQUESTS", "1").strip().lower()
        if raw == "auto":
            return TranscriptionConfig._auto_concurrent()
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning(f"Invalid GPU_CONCURRENT_REQUESTS='{raw}', defaulting to 1")
            return 1

    @staticmethod
    def _auto_concurrent() -> int:
        """Calculate max concurrent tasks from available VRAM.

        Calibrated from whitepaper benchmarks (large-v3-turbo + PyAnnote v4,
        diarization embedding batch pinned at 16):
          - Shared model baseline: ~7 GB (Whisper weights + PyAnnote pipeline)
          - Per-task VRAM overhead: ~4 GB (activation memory, CTranslate2 beam
            buffers, diarization intermediate tensors — measured at 48.5 GB total
            for 10 concurrent tasks on RTX A6000 49 GB)
        Formula: (total_vram - 7000 MB) // 4000 MB per task, capped at 12.

        Representative results:
          RTX A6000  49 GB → 10 concurrent (matches whitepaper 54.6x peak at 8)
          RTX 3090   24 GB →  4 concurrent
          RTX 3080Ti 12 GB →  1 concurrent (safe floor)
        """
        try:
            import torch

            if torch.cuda.is_available():
                total_mb = torch.cuda.get_device_properties(0).total_memory / (1024**2)
                concurrent = int((total_mb - 7000) // 4000)
                return max(1, min(concurrent, 12))
        except Exception as e:
            logger.debug(f"Auto-concurrent VRAM detection failed: {e}")
        return 1
