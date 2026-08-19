"""Engine stage implementations.

Stages mirror the existing Celery chain:
- _PreprocessStage     → cpu queue Stage 1
- _GpuStage            → gpu queue Stage 2 (Phase 1a combined)
- _GpuRawStage         → gpu queue Stage 2 (Phase 1b split: transcribe + diarize together)
- _TranscribeOnlyStage → gpu-transcribe queue Stage 2a (Phase 4 multi-GPU split)
- _DiarizerOnlyStage   → gpu-diarize queue Stage 2b (Phase 4 multi-GPU split)
- _FinalizeStage       → cpu queue Stage 3

In Phase 1a, _GpuStage combines the full pipeline (identical to
TranscriptionPipeline.process) so Engine.process() is byte-equal.
Phase 1b splits preprocess and finalize into the proper stages.
Phase 4 further splits Stage 2 so transcription and diarization run on
separate GPUs (ENGINE_GPU_SPLIT=true).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.job import JobResult
    from app.transcription.engine.job import JobSpec
    from app.transcription.engine.job import PreprocessResult
    from app.transcription.engine.job import RawInferenceResult
    from app.transcription.engine.job import RawTranscriptResult
    from app.transcription.engine.progress import ProgressCallback

logger = logging.getLogger(__name__)


def _overlap_diarization_enabled() -> bool:
    """True when diarization should run alongside transcription instead of after it.

    Requires the sidecar engine: only then is diarization another process's work, so the two
    genuinely run at once rather than contending for this worker's VRAM and GIL. Set
    ``DIAR_OVERLAP=0`` to force the sequential order back (an escape hatch for debugging, and
    the control used to prove the two orders produce identical output).
    """
    import os

    if os.environ.get("DIARIZER_ENGINE", "python").lower() != "native":
        return False
    return os.environ.get("DIAR_OVERLAP", "1").lower() not in ("0", "false", "no")


def _make_room_for_local_diarizer(manager, hw, profiler, tc, total_vram_mb: int) -> None:
    """Free VRAM for a diarizer that shares this process, and wait until there is enough.

    Only meaningful for the in-process engine: the sidecar holds its own memory, so a caller
    that overlapped diarization skips this entirely.
    """
    if tc.concurrent_requests > 1:
        logger.info(
            "Concurrent mode (concurrent_requests=%d): keeping transcriber loaded",
            tc.concurrent_requests,
        )
    elif total_vram_mb >= 16_000:
        logger.info("Keeping transcriber loaded (%dMB VRAM total, both models fit)", total_vram_mb)
    else:
        manager.release_transcriber()
        hw.log_vram_usage("after transcriber release")
        profiler.snapshot("diarizer_only_warm")

    if tc.concurrent_requests > 1:
        _wait_for_vram(2000, "diarization")


def _collect_diarization(audio, tc, manager, hw, profiler, callback, async_diarization):
    """Diarize, taking the overlapped result when one is in flight.

    Returns the diarizer's ``(diarize_df, overlap_info, native_embeddings)``. An overlapped
    attempt that failed falls back to a plain inline run, so the outcome is the same either way.
    """
    from app.transcription.engine.progress import emit

    hw.log_vram_usage("after transcription, before diarizer load")
    total_vram_mb = _get_total_vram_mb()
    profiler.snapshot("models_warm_no_inference")

    # Making room for a diarizer only matters when it lives in this process.
    if async_diarization is None:
        _make_room_for_local_diarizer(manager, hw, profiler, tc, total_vram_mb)

    emit(callback, 0.55, "Analyzing speaker patterns", "diarize")
    step_start = time.perf_counter()
    overlapped = None
    with profiler.step("diarization"):
        if async_diarization is not None:
            overlapped = async_diarization.result()
        result = overlapped if overlapped is not None else manager.get_diarizer(tc).diarize(audio)
    logger.info(
        "TIMING: diarization step completed in %.3fs%s",
        time.perf_counter() - step_start,
        " (overlapped with transcription)" if overlapped is not None else "",
    )
    profiler.snapshot("after_diarization")
    return result


class _AsyncDiarization:
    """Diarization running alongside transcription over the same in-memory audio.

    ``result()`` joins and returns the diarizer's tuple, or ``None`` if the attempt failed —
    the caller then runs the ordinary sequential path, so overlapping can never turn a
    working job into a failed one.
    """

    def __init__(self, audio, tc, manager, task_id: str | None):
        from app.utils.benchmark_timing import mark

        self._value: tuple | None = None
        self._error: BaseException | None = None
        self._task_id = task_id
        mark(task_id, "diarize_request_sent")

        def _run() -> None:
            try:
                self._value = manager.get_diarizer(tc).diarize(audio)
            except BaseException as exc:  # noqa: BLE001 — handed to the caller, not swallowed
                self._error = exc

        self._thread = threading.Thread(target=_run, name="diarize-async", daemon=True)
        self._thread.start()

    def result(self) -> tuple | None:
        from app.utils.benchmark_timing import mark

        self._thread.join()
        mark(self._task_id, "diarize_joined")
        if self._error is not None:
            logger.warning("Overlapped diarization failed (%s); retrying inline", self._error)
            return None
        return self._value


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

        profiler.snapshot("pipeline_start")

        # Steps 1+2: Load audio and ensure model is warm in parallel
        emit(progress_callback, 0.42, "Loading audio", "preprocess")
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

        if tc.concurrent_requests > 1:
            _wait_for_vram(1500, "transcriber_load")

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
        if tc.enable_diarization and _overlap_diarization_enabled():
            async_diarization = _AsyncDiarization(audio, tc, manager, job.task_id)

        # Overlap diarizer load with transcription when single-request mode. Pointless once
        # diarization is already running — there are no local weights to warm.
        diarizer_preload_thread: threading.Thread | None = None
        if tc.enable_diarization and tc.concurrent_requests <= 1 and async_diarization is None:

            def _preload_diarizer() -> None:
                try:
                    manager.get_diarizer(tc)
                except Exception as preload_err:
                    logger.debug(f"Diarizer preload (non-fatal, will retry inline): {preload_err}")

            diarizer_preload_thread = threading.Thread(
                target=_preload_diarizer, name="diarizer-preload", daemon=True
            )
            diarizer_preload_thread.start()

        # Transcribe
        emit(progress_callback, 0.43, "Running AI transcription", "transcribe")
        step_start = time.perf_counter()
        with profiler.step("transcription"):
            transcript = transcriber.transcribe(audio)
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

        emit(progress_callback, 0.52, "Preparing speaker analysis", "diarize")
        diarize_df, overlap_info, native_embeddings = _collect_diarization(
            audio, tc, manager, hw, profiler, progress_callback, async_diarization
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
        emit(progress_callback, 0.65, "Assigning speakers to transcript", "finalize")
        step_start = time.perf_counter()
        with profiler.step("speaker_assignment"):
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

        # Phase 3 (issue #193): acoustic re-check of short disputed/overlap words while
        # audio + speaker centroids are still in memory. Off by default; DB-controlled via
        # the admin Engine panel (engine.boundary_acoustic_*), injected through EngineConfig
        # so the engine stays DB-free. Reassigns absorbed backchannels ("yeah"/"mm-hmm") by
        # voiceprint — relabels existing words only, never fabricates. Flows to the core.py
        # chokepoint before resegment/merge, so the corrected labels drive segmentation.
        if native_embeddings and config is not None and config.boundary_acoustic_recheck_enabled:
            from app.transcription.boundary_resolver import acoustic_recheck

            words = [
                w
                for s in result.get("segments", [])
                for w in s.get("words", []) or []
                if "speaker" in w and "start" in w
            ]
            recheck_diarizer = manager.get_diarizer(tc)
            try:
                acoustic_recheck(
                    words,
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


class _PreprocessStage:
    """Stage 1 (CPU): decode audio to 16kHz WAV and write to shared volume."""

    def run(
        self,
        job: JobSpec,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> PreprocessResult:
        """Decode audio and write WAV to shared volume for Stage 2."""
        import os
        import time

        from app.transcription.engine.audio_loader import write_wav_to_shared_volume
        from app.transcription.engine.job import PreprocessResult
        from app.transcription.engine.progress import emit

        emit(callback, 0.10, "Decoding audio", "preprocess")
        t0 = time.perf_counter()

        from faster_whisper.audio import decode_audio  # type: ignore[import]

        audio = decode_audio(job.audio_path, sampling_rate=16000)
        elapsed_decode = time.perf_counter() - t0
        audio_duration_s = len(audio) / 16000

        task_id = job.task_id or os.path.basename(job.audio_path)
        wav_path = write_wav_to_shared_volume(audio, config.shared_volume_path, task_id) or ""

        return PreprocessResult(
            task_id=job.task_id,
            file_id=job.file_id,
            user_id=job.user_id,
            local_wav_path=wav_path,
            minio_temp_object="",
            audio_duration_s=audio_duration_s,
            audio_sample_rate=16000,
            audio_channels=1,
            audio_size_bytes=audio.nbytes,
            vad_regions=None,
            config_snapshot=config.to_snapshot(),
            stage1_timings={"decode": elapsed_decode},
        )


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

        profiler.snapshot("pipeline_start")

        emit(callback, 0.42, "Loading audio", "preprocess")

        audio = load_from_shared_volume(pre.local_wav_path)
        if audio is None:
            raise RuntimeError(
                f"Shared-volume WAV missing or unreadable: {pre.local_wav_path!r}. "
                "Stage 1 must write the WAV before Stage 2 runs."
            )

        if tc.concurrent_requests > 1:
            _wait_for_vram(1500, "transcriber_load")

        with profiler.step("model_load_transcriber"):
            transcriber = manager.get_transcriber(tc)

        profiler.snapshot("after_transcriber_loaded")

        # Sidecar diarization is another process's work — start it now and collect it after
        # transcription, so the GPU stage costs max(transcribe, diarize) rather than the sum.
        async_diarization: _AsyncDiarization | None = None
        if tc.enable_diarization and _overlap_diarization_enabled():
            async_diarization = _AsyncDiarization(audio, tc, manager, pre.task_id)

        diarizer_preload_thread: threading.Thread | None = None
        if tc.enable_diarization and tc.concurrent_requests <= 1 and async_diarization is None:

            def _preload_diarizer() -> None:
                try:
                    manager.get_diarizer(tc)
                except Exception as preload_err:
                    logger.debug(f"Diarizer preload (non-fatal, will retry inline): {preload_err}")

            diarizer_preload_thread = threading.Thread(
                target=_preload_diarizer, name="diarizer-preload", daemon=True
            )
            diarizer_preload_thread.start()

        emit(callback, 0.43, "Running AI transcription", "transcribe")
        step_start = time.perf_counter()
        with profiler.step("transcription"):
            transcript = transcriber.transcribe(audio)
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

        if tc.enable_diarization:
            diarize_df, overlap_info, native_embeddings = _collect_diarization(
                audio, tc, manager, hw, profiler, callback, async_diarization
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
            num_speakers = len({r["speaker"] for r in diarize_records}) if diarize_records else 1
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
        )


class _FinalizeStage:
    """Stage 3 (CPU): segment dedup and speaker assignment."""

    def run(
        self,
        raw: RawInferenceResult,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> JobResult:
        """Reconstruct diarization result, dedup segments, assign speakers."""
        import time

        import numpy as np

        from app.transcription.diarize_result import DiarizeResult
        from app.transcription.engine.job import JobResult
        from app.transcription.engine.progress import emit

        t0 = time.perf_counter()
        tc = config.transcription_config

        transcript: dict = {"segments": raw.raw_segments, "language": raw.language}

        if raw.diarize_records:
            diarize_df = DiarizeResult(
                start=np.array([r["start"] for r in raw.diarize_records]),
                end=np.array([r["end"] for r in raw.diarize_records]),
                speaker=np.array([r["speaker"] for r in raw.diarize_records]),
            )

            if tc.enable_dedup:
                from app.utils.segment_dedup import clean_segments

                transcript["segments"] = clean_segments(transcript["segments"])

            emit(callback, 0.65, "Assigning speakers to transcript", "finalize")
            from app.transcription.speaker_assigner import assign_speakers

            result = assign_speakers(diarize_df, transcript)

            if raw.overlap_info.get("count", 0) > 0:
                result["overlap_info"] = raw.overlap_info
            if raw.native_speaker_embeddings:
                result["native_speaker_embeddings"] = raw.native_speaker_embeddings
            if raw.speaker_gender:
                result["speaker_gender"] = raw.speaker_gender
        else:
            from app.utils.segment_dedup import clean_segments

            transcript["segments"] = clean_segments(transcript["segments"])
            for seg in transcript.get("segments", []):
                seg["speaker"] = "SPEAKER_00"
                for word in seg.get("words", []):
                    word["speaker"] = "SPEAKER_00"
            result = transcript

        return JobResult(
            segments=result.get("segments", []),
            language=result.get("language", ""),
            overlap_info=result.get("overlap_info", {}),
            native_speaker_embeddings=result.get("native_speaker_embeddings"),
            speaker_gender=result.get("speaker_gender"),
            stage_timings={"finalize": time.perf_counter() - t0},
        )


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
        import time

        from app.transcription.engine.audio_loader import load_from_shared_volume
        from app.transcription.engine.job import RawTranscriptResult
        from app.transcription.engine.progress import emit
        from app.transcription.model_manager import ModelManager
        from app.utils.vram_profiler import VRAMProfiler

        tc = config.transcription_config
        pipeline_start = time.perf_counter()
        profiler = VRAMProfiler()
        manager = ModelManager.get_instance()

        profiler.snapshot("pipeline_start")

        emit(callback, 0.42, "Loading audio", "preprocess")

        audio = load_from_shared_volume(pre.local_wav_path)
        if audio is None:
            raise RuntimeError(
                f"Shared-volume WAV missing or unreadable: {pre.local_wav_path!r}. "
                "Stage 1 must write the WAV before Stage 2a runs."
            )

        if tc.concurrent_requests > 1:
            _wait_for_vram(1500, "transcriber_load")

        with profiler.step("model_load_transcriber"):
            transcriber = manager.get_transcriber(tc)

        profiler.snapshot("after_transcriber_loaded")

        emit(callback, 0.43, "Running AI transcription", "transcribe")
        step_start = time.perf_counter()
        with profiler.step("transcription"):
            transcript = transcriber.transcribe(audio)
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

        profiler.snapshot("pipeline_start")

        emit(callback, 0.52, "Preparing speaker analysis", "diarize")

        audio = load_from_shared_volume(transcript.local_wav_path)
        if audio is None:
            raise RuntimeError(
                f"Shared-volume WAV missing or unreadable: {transcript.local_wav_path!r}. "
                "Stage 2a must retain the WAV for Stage 2b to consume."
            )

        diarize_records: list[dict] = []
        overlap_info: dict = {}
        native_embs_serialized: dict[str, list[float]] | None = None

        if tc.enable_diarization:
            if tc.concurrent_requests > 1:
                _wait_for_vram(2000, "diarization")

            emit(callback, 0.55, "Analyzing speaker patterns", "diarize")
            step_start = time.perf_counter()
            with profiler.step("diarization"):
                diarizer = manager.get_diarizer(tc)
                diarize_df, overlap_info, native_embeddings = diarizer.diarize(audio)
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
        )


def _wait_for_vram(min_free_mb: int, stage: str, timeout: int = 120) -> None:
    """Block until the GPU has enough free VRAM. Mirrors TranscriptionPipeline._wait_for_vram."""
    try:
        import torch

        if not torch.cuda.is_available():
            return
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            free_mb = torch.cuda.mem_get_info(0)[0] / (1024**2)
            if free_mb >= min_free_mb:
                return
            logger.info(
                f"VRAM gate [{stage}]: {free_mb:.0f}MB free < {min_free_mb}MB required, waiting..."
            )
            time.sleep(2)
        free_mb = torch.cuda.mem_get_info(0)[0] / (1024**2)
        logger.warning(
            f"VRAM gate [{stage}]: timeout after {timeout}s, proceeding with {free_mb:.0f}MB free"
        )
    except Exception as e:
        logger.debug(f"VRAM gate check skipped: {e}")


def _get_total_vram_mb() -> int:
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.get_device_properties(0).total_memory / (1024**2))
    except Exception as e:
        logger.debug(f"VRAM query failed: {e}")
    return 0
