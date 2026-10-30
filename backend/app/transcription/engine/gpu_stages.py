"""Combined and split-raw GPU stages: ``_GpuStage`` (Phase 1a) and ``_GpuRawStage`` (Phase 1b).

Both run transcription and diarization on one GPU; ``_GpuStage`` also assigns speakers, while
``_GpuRawStage`` returns raw results for ``_FinalizeStage``. Split out of ``stages.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

# The ONE cooperative checkpoint, consulted at every stage boundary below. It answers both
# stand-down triggers — worker shutdown (#809, requeue) and a user cancel of THIS file (#823,
# stop for good) — so a checkpoint cannot accidentally honour only one of them. That is why
# #823 extended these call sites instead of adding a second set beside them.
from app.core import stage_timing
from app.core.enums import TranscriptionStage
from app.core.task_cancellation import stand_down_if_requested
from app.transcription.engine.diarization_overlap import _AsyncDiarization
from app.transcription.engine.diarization_overlap import _collect_diarization
from app.transcription.engine.diarization_overlap import _overlap_diarization_enabled
from app.transcription.engine.diarization_overlap import _shutdown_join_timeout

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.job import JobResult
    from app.transcription.engine.job import JobSpec
    from app.transcription.engine.job import PreprocessResult
    from app.transcription.engine.job import RawInferenceResult
    from app.transcription.engine.progress import ProgressCallback

logger = logging.getLogger(__name__)


class _GpuStage:
    """Combined GPU + CPU stage (Phase 1a).

    Replicates TranscriptionPipeline.process() exactly to guarantee
    byte-identical output. Phase 1b will split this into three separate stages.
    """

    def run(
        self,
        job: JobSpec,
        config: EngineConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> JobResult:
        """Run the full pipeline on a single file, producing a JobResult."""
        from app.transcription.engine.job import JobResult
        from app.transcription.engine.progress import emit
        from app.transcription.model_manager import ModelManager
        from app.utils.hardware_detection import detect_hardware
        from app.utils.vram_profiler import VRAMProfiler

        tc = config.transcription_config
        pipeline_start = time.perf_counter()
        profiler = VRAMProfiler()
        hw = detect_hardware()
        manager = ModelManager.get_instance()

        # Issue #809: stand down BEFORE loading audio or warming Whisper. A worker that is
        # already stopping must not start a multi-minute job it cannot finish — the message is
        # requeued and the next worker runs it from the top, so nothing is lost by refusing here.
        stand_down_if_requested("_GpuStage.run entry")

        profiler.snapshot("pipeline_start")

        # Steps 1+2: Load audio and ensure model is warm in parallel.
        # TRANSCRIBE, not PREPARE: the task layer has already announced transcription
        # (0.40) by now, and the client must not see a finished step reopen. The same
        # applies to every in-stage "Loading audio" below.
        emit(progress_callback, 0.42, "Loading audio", TranscriptionStage.TRANSCRIBE)
        audio_result: list = [None]
        audio_error: list = [None]

        def _load_audio():
            try:
                from app.transcription.audio import load_audio

                audio_result[0] = load_audio(job.audio_path)
            except Exception as e:
                audio_error[0] = e

        audio_thread = threading.Thread(target=_load_audio, name="audio-load", daemon=True)
        audio_thread.start()

        with profiler.step("model_load_transcriber"):
            transcriber = manager.get_transcriber(tc)
        audio_thread.join()

        if audio_error[0]:
            raise audio_error[0]
        audio: np.ndarray = audio_result[0]

        profiler.snapshot("after_transcriber_loaded")

        # With the sidecar engine, diarization is another process's work — run it against the
        # same audio while this worker transcribes, instead of queueing it behind whisper.
        async_diarization: _AsyncDiarization | None = None
        if tc.enable_diarization and _overlap_diarization_enabled(tc):
            async_diarization = _AsyncDiarization(audio, tc, manager, job.task_id)

        # Everything below this point may reference `audio` (and, transitively, the shared-volume
        # WAV `async_diarization`'s thread is reading) — including the two early returns further
        # down. `close()` is idempotent, so wrapping the whole remainder guarantees the daemon
        # thread is always joined before `run()` returns, on every exit path (issue #661 phase
        # 1.1: an early return here previously left it running under a WAV a caller was about to
        # unlink).
        try:
            # Overlap diarizer load with transcription when single-request mode. Pointless once
            # diarization is already running — there are no local weights to warm.
            diarizer_preload_thread: threading.Thread | None = None
            if tc.enable_diarization and tc.concurrent_requests <= 1 and async_diarization is None:

                def _preload_diarizer() -> None:
                    try:
                        manager.get_diarizer(tc)
                    except Exception as preload_err:
                        logger.debug(
                            f"Diarizer preload (non-fatal, will retry inline): {preload_err}"
                        )

                diarizer_preload_thread = threading.Thread(
                    target=_preload_diarizer, name="diarizer-preload", daemon=True
                )
                diarizer_preload_thread.start()

            # Transcribe
            emit(progress_callback, 0.43, "Running AI transcription", TranscriptionStage.TRANSCRIBE)
            step_start = time.perf_counter()
            with profiler.step("transcription"):
                transcript = transcriber.transcribe(audio, options=tc)
            logger.info(
                f"TIMING: transcription step completed in {time.perf_counter() - step_start:.3f}s"
            )

            if diarizer_preload_thread is not None:
                diarizer_preload_thread.join()

            if not transcript.get("segments"):
                logger.warning("Transcription produced no segments")
                return JobResult(
                    segments=[],
                    language=transcript.get("language", ""),
                    stage_timings={"transcription": time.perf_counter() - pipeline_start},
                )

            if tc.enable_diarization:
                # Issue #809: transcription is done and its result is about to be thrown away
                # either way, so standing down here costs the diarization only. INSIDE the try,
                # deliberately — raised outside it, the `finally` below never runs and the
                # overlapped diarization thread is left alive holding the WAV (issue #661
                # phase 1.1, the hazard that try/finally was added for).
                stand_down_if_requested("_GpuStage.run before diarization")
                result_dict, diarize_df = self._run_diarization(
                    audio,
                    transcript,
                    profiler,
                    hw,
                    tc,
                    manager,
                    progress_callback,
                    config,
                    async_diarization=async_diarization,
                )
            else:
                result_dict, diarize_df = self._skip_diarization(transcript, tc)

            return self._finalize_job_result(
                result_dict, diarize_df, audio, hw, tc, profiler, job, pipeline_start
            )
        finally:
            if async_diarization is not None:
                # Bounded ONLY while shutting down (issue #809) — see close()'s docstring for
                # why an unbounded join here would otherwise swallow the whole cooperative abort.
                async_diarization.close(timeout=_shutdown_join_timeout())

    @staticmethod
    def _finalize_job_result(
        result_dict: dict,
        diarize_df,
        audio,
        hw,
        tc,
        profiler,
        job: JobSpec,
        pipeline_start: float,
    ) -> JobResult:
        """Re-enable TF32, release audio/diarizer memory, log timing, build the JobResult.

        Split out of ``run()`` (issue #661 phase 1.1) purely to keep that method's cyclomatic
        complexity under the lint threshold after the try/finally wrap added there — no
        behavior change from the inline version it replaces.
        """
        from app.transcription.engine.job import JobResult

        # Re-enable TF32 after diarization (PyAnnote's fix_reproducibility disables it)
        if tc.device == "cuda":
            try:
                import torch

                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
            except Exception as e:
                logger.debug(f"TF32 re-enable skipped: {e}")

        # Cleanup
        hw.log_vram_usage("before final pipeline cleanup")
        audio_duration = len(audio) / 16000 if audio is not None else 0.0
        if diarize_df is not None and len(diarize_df) > 0:
            num_speakers = int(np.unique(diarize_df.speaker).size)
        else:
            num_speakers = 1 if not tc.enable_diarization else 0
        del diarize_df, audio
        hw.optimize_memory_usage()
        hw.log_vram_usage("after final pipeline cleanup")

        profiler.log_report()
        if job.task_id:
            profiler.save_to_redis(job.task_id, audio_duration, num_speakers)

        elapsed = time.perf_counter() - pipeline_start
        logger.info(
            f"TIMING: Engine._GpuStage.run TOTAL completed in {elapsed:.3f}s - "
            f"{len(result_dict.get('segments', []))} segments, "
            f"language={result_dict.get('language', 'unknown')}"
        )

        return JobResult(
            segments=result_dict.get("segments", []),
            language=result_dict.get("language", ""),
            overlap_info=result_dict.get("overlap_info", {}),
            native_speaker_embeddings=result_dict.get("native_speaker_embeddings"),
            speaker_gender=result_dict.get("speaker_gender"),
            stage_timings={"total": elapsed},
            diarization_provider=result_dict.get("diarization_provider"),
            diarization_model=result_dict.get("diarization_model"),
        )

    def _run_diarization(
        self,
        audio,
        transcript,
        profiler,
        hw,
        tc,
        manager,
        progress_callback,
        config=None,
        async_diarization: _AsyncDiarization | None = None,
    ) -> tuple[dict, Any]:
        from app.transcription.engine.progress import emit

        emit(progress_callback, 0.52, "Preparing speaker analysis", TranscriptionStage.DIARIZE)
        diarize_df, overlap_info, native_embeddings, diar_provider, diar_model = (
            _collect_diarization(
                audio, tc, manager, hw, profiler, progress_callback, async_diarization
            )
        )

        # Step 5: Segment dedup BEFORE speaker assignment
        if tc.enable_dedup:
            step_start = time.perf_counter()
            from app.utils.segment_dedup import clean_segments

            original_count = len(transcript.get("segments", []))
            transcript["segments"] = clean_segments(transcript["segments"])
            logger.info(
                f"TIMING: segment_dedup completed in "
                f"{time.perf_counter() - step_start:.3f}s - "
                f"{original_count} -> {len(transcript['segments'])} segments"
            )

        # Step 6: Assign speakers
        emit(
            progress_callback, 0.65, "Assigning speakers to transcript", TranscriptionStage.FINALIZE
        )
        step_start = time.perf_counter()
        with stage_timing.stage("speaker_assignment"), profiler.step("speaker_assignment"):
            from app.transcription.speaker_assigner import assign_speakers

            result = assign_speakers(diarize_df, transcript)
        logger.info(
            f"TIMING: speaker assignment completed in {time.perf_counter() - step_start:.3f}s"
        )

        if overlap_info.get("count", 0) > 0:
            result["overlap_info"] = overlap_info
        if native_embeddings:
            result["native_speaker_embeddings"] = native_embeddings
        # Carried on the DiarizeResult so the engine contract stayed unchanged.
        if getattr(diarize_df, "speaker_gender", None):
            result["speaker_gender"] = diarize_df.speaker_gender
        # Resolved AFTER any fallback (issue #706) — never the configured backend.
        result["diarization_provider"] = diar_provider
        result["diarization_model"] = diar_model

        # Phase 3 (issue #193): acoustic re-check of short disputed/overlap words while
        # audio + speaker centroids are still in memory. Off by default; DB-controlled via
        # the admin Engine panel (engine.boundary_acoustic_*), injected through EngineConfig
        # so the engine stays DB-free. Reassigns absorbed backchannels ("yeah"/"mm-hmm") by
        # voiceprint — relabels existing words only, never fabricates. Flows to the core.py
        # chokepoint before resegment/merge, so the corrected labels drive segmentation.
        if native_embeddings and config is not None and config.boundary_acoustic_recheck_enabled:
            from app.transcription.boundary_resolver import recheck_segment_words

            try:
                # Inside the guard: in strict mode (engine.diarizer_require_sidecar) a sidecar
                # that dropped after diarization finished makes this raise, and a re-check that
                # cannot run must cost the job nothing but the re-check.
                recheck_diarizer = manager.get_diarizer(tc)
                recheck_segment_words(
                    result.get("segments", []),
                    native_embeddings,
                    lambda s, e: recheck_diarizer.embed_window(audio, s, e),
                    overlap_regions=overlap_info.get("regions"),
                    cosine_margin=config.boundary_acoustic_cosine_margin,
                    max_word_dur=config.boundary_acoustic_max_word_dur,
                )
            except Exception:
                logger.exception("acoustic_recheck failed; keeping max-overlap labels")

        return result, diarize_df

    @staticmethod
    def _skip_diarization(transcript: dict, tc) -> tuple[dict, None]:
        from app.utils.segment_dedup import clean_segments

        logger.info("Diarization disabled — assigning SPEAKER_00 to all segments")
        transcript["segments"] = clean_segments(transcript["segments"])
        for seg in transcript.get("segments", []):
            seg["speaker"] = "SPEAKER_00"
            for word in seg.get("words", []):
                word["speaker"] = "SPEAKER_00"
        return transcript, None


class _GpuRawStage:
    """Stage 2 (GPU): transcription + diarization, returning raw results without speaker assignment."""

    def run(
        self,
        pre: PreprocessResult,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> RawInferenceResult:
        """Load WAV, transcribe, diarize — return raw results without speaker assignment."""
        import time

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

        # Issue #809: stand down before loading the WAV or warming Whisper — see the twin
        # comment in _GpuStage.run. The shared-volume WAV is deliberately NOT cleaned up on an
        # abort (core.py::_finish_failed_or_aborted), so the redelivered attempt still finds it.
        stand_down_if_requested("_GpuRawStage.run entry")

        profiler.snapshot("pipeline_start")

        emit(callback, 0.42, "Loading audio", TranscriptionStage.TRANSCRIBE)

        audio = load_from_shared_volume(pre.local_wav_path)
        if audio is None:
            raise RuntimeError(
                f"Shared-volume WAV missing or unreadable: {pre.local_wav_path!r}. "
                "Stage 1 must write the WAV before Stage 2 runs."
            )

        with profiler.step("model_load_transcriber"):
            transcriber = manager.get_transcriber(tc)

        profiler.snapshot("after_transcriber_loaded")

        # Sidecar diarization is another process's work — start it now and collect it after
        # transcription, so the GPU stage costs max(transcribe, diarize) rather than the sum.
        async_diarization: _AsyncDiarization | None = None
        if tc.enable_diarization and _overlap_diarization_enabled(tc):
            async_diarization = _AsyncDiarization(
                audio, tc, manager, pre.task_id, wav_path=pre.local_wav_path
            )

        # See the matching comment in `_GpuStage.run` — same reasoning, same fix (issue #661
        # phase 1.1). `async_diarization` here also carries `wav_path=pre.local_wav_path`, the
        # shared-volume WAV a caller's `cleanup_shared_volume_wav` may unlink once `run()`
        # returns, which is exactly what made this early-return leak live rather than harmless.
        try:
            diarizer_preload_thread: threading.Thread | None = None
            if tc.enable_diarization and tc.concurrent_requests <= 1 and async_diarization is None:

                def _preload_diarizer() -> None:
                    try:
                        manager.get_diarizer(tc)
                    except Exception as preload_err:
                        logger.debug(
                            f"Diarizer preload (non-fatal, will retry inline): {preload_err}"
                        )

                diarizer_preload_thread = threading.Thread(
                    target=_preload_diarizer, name="diarizer-preload", daemon=True
                )
                diarizer_preload_thread.start()

            emit(callback, 0.43, "Running AI transcription", TranscriptionStage.TRANSCRIBE)
            step_start = time.perf_counter()
            with profiler.step("transcription"):
                transcript = transcriber.transcribe(audio, options=tc)
            logger.info(
                f"TIMING: transcription step completed in {time.perf_counter() - step_start:.3f}s"
            )

            if diarizer_preload_thread is not None:
                diarizer_preload_thread.join()

            if not transcript.get("segments"):
                logger.warning("Transcription produced no segments")
                return RawInferenceResult(
                    task_id=pre.task_id,
                    audio_path="",
                    audio_duration_s=pre.audio_duration_s,
                    language=transcript.get("language", ""),
                    raw_segments=[],
                    diarize_records=[],
                    overlap_info={},
                    native_speaker_embeddings=None,
                    config_snapshot=pre.config_snapshot,
                    stage_timings={"transcription": time.perf_counter() - pipeline_start},
                )

            diarize_records: list[dict] = []
            overlap_info: dict = {}
            speaker_gender: dict | None = None
            native_embs_serialized: dict[str, list[float]] | None = None
            diar_provider: str | None = None
            diar_model: str | None = None

            if tc.enable_diarization:
                # Issue #809: INSIDE the try, for the same reason as _GpuStage.run's — an abort
                # raised outside it skips `finally: async_diarization.close()` and leaks the
                # overlapped thread under a WAV a caller may unlink.
                stand_down_if_requested("_GpuRawStage.run before diarization")
                diarize_df, overlap_info, native_embeddings, diar_provider, diar_model = (
                    _collect_diarization(
                        audio,
                        tc,
                        manager,
                        hw,
                        profiler,
                        callback,
                        async_diarization,
                        wav_path=pre.local_wav_path,
                    )
                )
                diarize_records = diarize_df.to_records()
                speaker_gender = getattr(diarize_df, "speaker_gender", None)
                if native_embeddings:
                    native_embs_serialized = {
                        k: v.tolist() if hasattr(v, "tolist") else list(v)
                        for k, v in native_embeddings.items()
                    }

            if tc.device == "cuda":
                try:
                    import torch

                    torch.backends.cuda.matmul.allow_tf32 = True
                    torch.backends.cudnn.allow_tf32 = True
                except Exception as e:
                    logger.debug(f"TF32 re-enable skipped: {e}")

            hw.log_vram_usage("before final pipeline cleanup")
            del audio
            hw.optimize_memory_usage()
            hw.log_vram_usage("after final pipeline cleanup")

            elapsed = time.perf_counter() - pipeline_start
            profiler.log_report()
            if pre.task_id:
                num_speakers = (
                    len({r["speaker"] for r in diarize_records}) if diarize_records else 1
                )
                profiler.save_to_redis(pre.task_id, pre.audio_duration_s, num_speakers)

            return RawInferenceResult(
                task_id=pre.task_id,
                audio_path="",
                audio_duration_s=pre.audio_duration_s,
                language=transcript.get("language", ""),
                raw_segments=transcript.get("segments", []),
                diarize_records=diarize_records,
                overlap_info=overlap_info,
                native_speaker_embeddings=native_embs_serialized,
                speaker_gender=speaker_gender,
                config_snapshot=pre.config_snapshot,
                stage_timings={"gpu_total": elapsed},
                diarization_provider=diar_provider,
                diarization_model=diar_model,
            )
        finally:
            if async_diarization is not None:
                # Bounded ONLY while shutting down (issue #809) — see close()'s docstring for
                # why an unbounded join here would otherwise swallow the whole cooperative abort.
                async_diarization.close(timeout=_shutdown_join_timeout())
