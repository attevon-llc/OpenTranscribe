"""Multi-GPU split stages (``ENGINE_GPU_SPLIT=true``): ``_TranscribeOnlyStage`` (Stage 2a) and
``_DiarizerOnlyStage`` (Stage 2b). Split out of ``stages.py``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

# The ONE cooperative checkpoint, consulted at every stage boundary below. It answers both
# stand-down triggers — worker shutdown (#809, requeue) and a user cancel of THIS file (#823,
# stop for good) — so a checkpoint cannot accidentally honour only one of them. That is why
# #823 extended these call sites instead of adding a second set beside them.
from app.core import stage_timing
from app.core.enums import TranscriptionStage
from app.core.task_cancellation import stand_down_if_requested
from app.transcription.engine.diarization_overlap import _run_diarize

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.job import PreprocessResult
    from app.transcription.engine.job import RawInferenceResult
    from app.transcription.engine.job import RawTranscriptResult
    from app.transcription.engine.progress import ProgressCallback

logger = logging.getLogger(__name__)


class _TranscribeOnlyStage:
    """Stage 2a (GPU-transcribe): transcription only — no diarization.

    Used when ENGINE_GPU_SPLIT=true to run Whisper on a dedicated GPU while
    diarization is offloaded to a separate gpu-diarize worker (_DiarizerOnlyStage).
    """

    def run(
        self,
        pre: PreprocessResult,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> RawTranscriptResult:
        """Load WAV from shared volume, transcribe, return raw transcript without diarizing."""

        from app.transcription.engine.audio_loader import load_from_shared_volume
        from app.transcription.engine.job import RawTranscriptResult
        from app.transcription.engine.progress import emit
        from app.transcription.model_manager import ModelManager
        from app.utils.vram_profiler import VRAMProfiler

        tc = config.transcription_config
        pipeline_start = time.perf_counter()
        profiler = VRAMProfiler()
        manager = ModelManager.get_instance()

        # Issue #809: stand down before loading the WAV or warming Whisper. This stage runs
        # under transcribe_gpu_task, whose handler requeues the abort (core.py).
        stand_down_if_requested("_TranscribeOnlyStage.run entry")

        profiler.snapshot("pipeline_start")

        emit(callback, 0.42, "Loading audio", TranscriptionStage.TRANSCRIBE)

        audio = load_from_shared_volume(pre.local_wav_path)
        if audio is None:
            raise RuntimeError(
                f"Shared-volume WAV missing or unreadable: {pre.local_wav_path!r}. "
                "Stage 1 must write the WAV before Stage 2a runs."
            )

        with profiler.step("model_load_transcriber"):
            transcriber = manager.get_transcriber(tc)

        profiler.snapshot("after_transcriber_loaded")

        emit(callback, 0.43, "Running AI transcription", TranscriptionStage.TRANSCRIBE)
        step_start = time.perf_counter()
        with profiler.step("transcription"):
            transcript = transcriber.transcribe(audio, options=tc)
        logger.info(
            f"TIMING: transcription step completed in {time.perf_counter() - step_start:.3f}s"
        )

        # Release transcriber VRAM before the diarize worker claims the other GPU.
        # Avoids the inter-process VRAM peak from both models being loaded simultaneously
        # when the two workers share the same physical device (misconfigured split).
        manager.release_transcriber()

        elapsed = time.perf_counter() - pipeline_start
        profiler.log_report()

        return RawTranscriptResult(
            task_id=pre.task_id,
            audio_path="",
            audio_duration_s=pre.audio_duration_s,
            language=transcript.get("language", ""),
            raw_segments=transcript.get("segments", []),
            local_wav_path=pre.local_wav_path,
            config_snapshot=pre.config_snapshot,
            stage_timings={"transcribe_only": elapsed},
        )


class _DiarizerOnlyStage:
    """Stage 2b (GPU-diarize): diarization only — no transcription.

    Receives the raw Whisper output from _TranscribeOnlyStage via Redis,
    reloads the audio from the shared volume, and runs PyAnnote diarization.
    Returns a RawInferenceResult with the same shape as _GpuRawStage so the
    existing _FinalizeStage / postprocess chain can run unchanged.
    """

    def run(
        self,
        transcript: RawTranscriptResult,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> RawInferenceResult:
        """Reload WAV, diarize, combine with raw segments — return RawInferenceResult."""

        from app.transcription.engine.audio_loader import load_from_shared_volume
        from app.transcription.engine.job import RawInferenceResult
        from app.transcription.engine.progress import emit
        from app.transcription.model_manager import ModelManager
        from app.utils.hardware_detection import detect_hardware
        from app.utils.vram_profiler import VRAMProfiler

        tc = config.transcription_config
        pipeline_start = time.perf_counter()
        profiler = VRAMProfiler()
        hw = detect_hardware()
        manager = ModelManager.get_instance()

        # Issue #809: stand down before reloading the WAV or loading the diarizer.
        #
        # ⚠️ This checkpoint is only SAFE because diarize_gpu_task now has an abort branch that
        # requeues instead of failing the file. Before that, an abort here would have travelled
        # diarize_task's generic `except Exception`, marked the file ERROR and ACKed the message
        # under acks_late -- losing the transcription outright. Do not add a checkpoint to a
        # stage whose task cannot requeue the abort -- nor, since #823, to one that cannot
        # finish a TranscriptionCancelledError: routed through the generic handler THAT reads
        # to the user as "your file failed" for a stop they asked for.
        stand_down_if_requested("_DiarizerOnlyStage.run entry")

        profiler.snapshot("pipeline_start")

        emit(callback, 0.52, "Preparing speaker analysis", TranscriptionStage.DIARIZE)

        audio = load_from_shared_volume(transcript.local_wav_path)
        if audio is None:
            raise RuntimeError(
                f"Shared-volume WAV missing or unreadable: {transcript.local_wav_path!r}. "
                "Stage 2a must retain the WAV for Stage 2b to consume."
            )

        diarize_records: list[dict] = []
        overlap_info: dict = {}
        native_embs_serialized: dict[str, list[float]] | None = None
        word_overrides: dict[str, str] | None = None
        diar_provider: str | None = None
        diar_model: str | None = None

        if tc.enable_diarization:
            # Issue #809: the last bounded point before the diarizer load and the opaque
            # diarize() call. There is no try/finally to sit inside here — this stage never
            # constructs an _AsyncDiarization (it IS the diarization leg).
            stand_down_if_requested("_DiarizerOnlyStage.run before diarization")

            emit(callback, 0.55, "Analyzing speaker patterns", TranscriptionStage.DIARIZE)
            step_start = time.perf_counter()
            with stage_timing.stage("diarization"), profiler.step("diarization"):
                diarizer = manager.get_diarizer(tc)
                diarize_df, overlap_info, native_embeddings = _run_diarize(
                    diarizer, audio, transcript.local_wav_path, tc=tc
                )
                diar_provider, diar_model = diarizer.last_provider, diarizer.last_model
            logger.info(
                f"TIMING: diarization step completed in {time.perf_counter() - step_start:.3f}s"
            )
            profiler.snapshot("after_diarization")

            diarize_records = diarize_df.to_records()
            if native_embeddings:
                native_embs_serialized = {
                    k: v.tolist() if hasattr(v, "tolist") else list(v)
                    for k, v in native_embeddings.items()
                }
                if config.boundary_acoustic_recheck_enabled:
                    word_overrides = self._acoustic_recheck_overrides(
                        audio,
                        transcript,
                        diarize_df,
                        overlap_info,
                        native_embeddings,
                        diarizer,
                        tc,
                        config,
                    )

        # Re-enable TF32 after PyAnnote (its fix_reproducibility disables it)
        if tc.device == "cuda":
            try:
                import torch

                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
            except Exception as e:
                logger.debug(f"TF32 re-enable skipped: {e}")

        hw.log_vram_usage("before final diarize-only cleanup")
        del audio
        hw.optimize_memory_usage()
        hw.log_vram_usage("after final diarize-only cleanup")

        elapsed = time.perf_counter() - pipeline_start
        profiler.log_report()
        if transcript.task_id:
            num_speakers = len({r["speaker"] for r in diarize_records}) if diarize_records else 1
            profiler.save_to_redis(transcript.task_id, transcript.audio_duration_s, num_speakers)

        # Merge stage timings from both Stage 2a and 2b for observability
        merged_timings = {**transcript.stage_timings, "diarize_only": elapsed}

        return RawInferenceResult(
            task_id=transcript.task_id or "",
            audio_path=transcript.audio_path,
            audio_duration_s=transcript.audio_duration_s,
            language=transcript.language,
            raw_segments=transcript.raw_segments,
            diarize_records=diarize_records,
            overlap_info=overlap_info,
            native_speaker_embeddings=native_embs_serialized,
            config_snapshot=transcript.config_snapshot,
            stage_timings=merged_timings,
            diarization_provider=diar_provider,
            diarization_model=diar_model,
            word_speaker_overrides=word_overrides,
        )

    @staticmethod
    def _acoustic_recheck_overrides(
        audio,
        transcript,
        diarize_df,
        overlap_info: dict,
        native_embeddings,
        diarizer,
        tc,
        config,
    ) -> dict[str, str] | None:
        """Phase 3 acoustic re-check for the gpu-split path (issue #1205).

        Only this worker has the audio and the speaker centroids, but word-to-speaker
        assignment happens later on CPU in ``_FinalizeStage``. So assign on a private copy of
        the segments exactly as finalize will, relabel by voiceprint, and ship only the words
        that changed (``word_key`` -> speaker) for finalize to re-apply after its own
        assignment. Returns None when nothing changed or the re-check failed (the max-overlap
        labels are kept).
        """
        import copy

        from app.transcription.boundary_resolver import recheck_segment_words
        from app.transcription.boundary_resolver import word_key
        from app.transcription.speaker_assigner import assign_speakers

        segments = copy.deepcopy(transcript.raw_segments)
        if tc.enable_dedup:
            from app.utils.segment_dedup import clean_segments

            segments = clean_segments(segments)
        try:
            assigned = assign_speakers(diarize_df, {"segments": segments})["segments"]
            words = [
                w
                for seg in assigned
                for w in seg.get("words", []) or []
                if "speaker" in w and "start" in w and "end" in w
            ]
            before = [w["speaker"] for w in words]
            recheck_segment_words(
                assigned,
                native_embeddings,
                lambda s, e: diarizer.embed_window(audio, s, e),
                overlap_regions=overlap_info.get("regions"),
                cosine_margin=config.boundary_acoustic_cosine_margin,
                max_word_dur=config.boundary_acoustic_max_word_dur,
            )
            overrides = {
                word_key(w["start"], w["end"]): w["speaker"]
                for w, prev in zip(words, before, strict=True)
                if w["speaker"] != prev
            }
        except Exception:
            logger.exception("acoustic_recheck failed; keeping max-overlap labels")
            return None
        return overrides or None
