"""Engine — main entry point for the combined transcription + diarization engine.

Phase 1a: process() — combined entry (byte-identical to TranscriptionPipeline).
Phase 1b: run_preprocess() / run_gpu_stage() / run_cpu_finalize() — split-stage.
Phase 4:  run_transcribe_only() / run_diarize_only() — multi-GPU split
          (ENGINE_GPU_SPLIT=true; transcription and diarization on separate GPUs).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from typing import Callable

if TYPE_CHECKING:
    from app.transcription.engine.job import JobResult
    from app.transcription.engine.job import JobSpec
    from app.transcription.engine.job import PreprocessResult
    from app.transcription.engine.job import RawInferenceResult
    from app.transcription.engine.job import RawTranscriptResult

from app.transcription.engine.config import EngineConfig
from app.transcription.engine.metrics import MetricsCollector

logger = logging.getLogger(__name__)


class Engine:
    """Orchestrates the combined transcription + diarization pipeline.

    A single Engine instance is created per worker process and shared by all
    Celery threads (stateless after construction — all per-task state lives
    in JobSpec / PreprocessResult / RawInferenceResult dataclasses).

    Usage (combined path, Phase 1a):
        engine = Engine(EngineConfig.from_environment())
        result = engine.process(JobSpec(audio_path=..., task_id=...))

    Usage (split-stage path, Phase 1b):
        pre  = engine.run_preprocess(spec)
        raw  = engine.run_gpu_stage(pre)
        result = engine.run_cpu_finalize(raw)
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.metrics = MetricsCollector()

    def warmup(self) -> None:
        """Validate backend wiring at worker startup.

        Called from @worker_ready alongside ModelManager.ensure_models_loaded().
        Sets PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True when not already
        set (reduces VRAM fragmentation under concurrent load — PyTorch >= 2.1).
        """
        _set_cuda_alloc_conf()
        logger.info(
            "Engine.warmup: transcriber_backend=%s diarizer_backend=%s",
            self.config.transcriber_backend,
            self.config.diarizer_backend,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 1a — combined entry point
    # ──────────────────────────────────────────────────────────────────────────

    def process(
        self,
        job: JobSpec,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> JobResult:
        """Run the full pipeline in a single call.

        Produces byte-identical output to TranscriptionPipeline.process().
        Used for tests, scripts, and the Phase 1a shim.

        Args:
            job: Input spec (audio_path, task_id, …).
            progress_callback: Optional (progress, message) callback compatible
                with the existing Celery task callback signature.

        Returns:
            JobResult with segments, language, overlap_info, embeddings.
        """
        from app.transcription.engine.progress import adapt_legacy
        from app.transcription.engine.stages import _GpuStage

        engine_callback = adapt_legacy(progress_callback)
        stage = _GpuStage()
        return stage.run(job, self.config, engine_callback)

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 1b — split-stage entry points
    # ──────────────────────────────────────────────────────────────────────────

    def run_preprocess(
        self,
        job: JobSpec,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> PreprocessResult:
        """Stage 1 (CPU): decode audio to WAV and write to shared volume.

        Args:
            job: Input spec with audio_path and task_id.
            progress_callback: Optional legacy (float, str) callback.

        Returns:
            PreprocessResult with local_wav_path and audio metadata.
        """
        from app.transcription.engine.progress import adapt_legacy
        from app.transcription.engine.stages import _PreprocessStage

        stage = _PreprocessStage()
        return stage.run(job, self.config, adapt_legacy(progress_callback))

    def run_gpu_stage(
        self,
        pre: PreprocessResult,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> RawInferenceResult:
        """Stage 2 (GPU): transcription + diarization without speaker assignment.

        Args:
            pre: PreprocessResult from run_preprocess(); must have local_wav_path set.
            progress_callback: Optional legacy (float, str) callback.

        Returns:
            RawInferenceResult with raw segments and serialized diarization records.
        """
        from app.transcription.engine.config import EngineConfig
        from app.transcription.engine.progress import adapt_legacy
        from app.transcription.engine.stages import _GpuRawStage

        stage = _GpuRawStage()
        stage_config = EngineConfig.from_snapshot(pre.config_snapshot)
        return stage.run(pre, stage_config, adapt_legacy(progress_callback))

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 4 — multi-GPU split entry points
    # ──────────────────────────────────────────────────────────────────────────

    def run_transcribe_only(
        self,
        pre: PreprocessResult,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> RawTranscriptResult:
        """Stage 2a (GPU-transcribe): transcription only, no diarization.

        Args:
            pre: PreprocessResult from run_preprocess(); must have local_wav_path set.
            progress_callback: Optional legacy (float, str) callback.

        Returns:
            RawTranscriptResult carrying raw Whisper segments for Stage 2b.
        """
        from app.transcription.engine.config import EngineConfig
        from app.transcription.engine.progress import adapt_legacy
        from app.transcription.engine.stages import _TranscribeOnlyStage

        stage = _TranscribeOnlyStage()
        stage_config = EngineConfig.from_snapshot(pre.config_snapshot)
        return stage.run(pre, stage_config, adapt_legacy(progress_callback))

    def run_diarize_only(
        self,
        transcript: RawTranscriptResult,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> RawInferenceResult:
        """Stage 2b (GPU-diarize): diarization only, no transcription.

        Args:
            transcript: RawTranscriptResult from run_transcribe_only().
            progress_callback: Optional legacy (float, str) callback.

        Returns:
            RawInferenceResult with the same shape as run_gpu_stage() so that
            the existing run_cpu_finalize() / postprocess chain is unaffected.
        """
        from app.transcription.engine.config import EngineConfig
        from app.transcription.engine.progress import adapt_legacy
        from app.transcription.engine.stages import _DiarizerOnlyStage

        stage = _DiarizerOnlyStage()
        stage_config = EngineConfig.from_snapshot(transcript.config_snapshot)
        return stage.run(transcript, stage_config, adapt_legacy(progress_callback))

    def run_cpu_finalize(
        self,
        raw: RawInferenceResult,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> JobResult:
        """Stage 3 (CPU): dedup segments and assign speakers.

        Args:
            raw: RawInferenceResult from run_gpu_stage().
            progress_callback: Optional legacy (float, str) callback.

        Returns:
            JobResult with fully assigned segments and embeddings.
        """
        from app.transcription.engine.config import EngineConfig
        from app.transcription.engine.progress import adapt_legacy
        from app.transcription.engine.stages import _FinalizeStage

        stage = _FinalizeStage()
        stage_config = EngineConfig.from_snapshot(raw.config_snapshot)
        return stage.run(raw, stage_config, adapt_legacy(progress_callback))


def _set_cuda_alloc_conf() -> None:
    """Set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True if not already set.

    Reduces VRAM fragmentation when concurrent_requests > 1.
    Only effective before PyTorch initializes the CUDA context.
    """
    key = "PYTORCH_CUDA_ALLOC_CONF"
    existing = os.environ.get(key, "")
    if "expandable_segments" not in existing:
        new_val = f"{existing},expandable_segments:True" if existing else "expandable_segments:True"
        os.environ[key] = new_val
        logger.info("Engine.warmup: set %s=%s", key, new_val)
    else:
        logger.debug("Engine.warmup: %s already set (%s)", key, existing)
