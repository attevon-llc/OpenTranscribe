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


def _overlap_diarization_enabled(tc) -> bool:
    """True when diarization should run alongside transcription instead of after it.

    Requires the sidecar to actually be REACHABLE, not merely configured
    (``tc.diarizer_backend == "native"``). A deployment configured "native" whose sidecar is
    down still runs diarization in-process on the PyAnnote fallback, and overlapping that
    with transcription would leave both models co-resident on this worker's GPU — exactly
    what ``_make_room_for_local_diarizer`` exists to prevent, and it is skipped whenever this
    returns True (see the ``async_diarization is None`` check at each call site). Set
    ``DIAR_OVERLAP=0`` to force the sequential order back regardless of reachability (an
    escape hatch for debugging, and the control used to prove the two orders produce
    identical output).

    ``sidecar_ready()`` carries its own short TTL cache (``diarizer_native.py``, issue #661
    probe-cost fix) rather than one here — it is the single home for that cache because
    ``ModelManager._diarizer_current`` calls the same function per task, and one cache keyed
    on base_url serves both call sites. The cheap config/env checks here still run first so
    the probe fires only when it can actually change the answer.
    """
    import os

    if tc.diarizer_backend.lower() != "native":
        return False
    if os.environ.get("DIAR_OVERLAP", "1").lower() in ("0", "false", "no"):
        return False

    from app.transcription.diarizer_native import sidecar_ready

    return sidecar_ready()


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


def _run_diarize(
    diarizer,
    audio,
    wav_path: str | None,
    *,
    allow_local_fallback: bool = True,
    skip_sidecar: bool = False,
):
    """Call diarizer.diarize(), handing NativeSpeakerDiarizer a pre-existing WAV when we have one.

    NativeSpeakerDiarizer.diarize() accepts an optional wav_path to skip re-encoding audio it
    can already reach on the shared volume (issue #661) — Stage 1's WAV in the 3-stage Celery
    pipeline. The in-process PyAnnote fork's diarize(audio) has no such argument and never will
    (the two engines are a duck-typed drop-in for each other; diarizer.py is not touched for
    this), so the isinstance check below is what keeps the extra kwarg from ever reaching a
    diarizer that doesn't accept it — callers here only ever hold ``SpeakerDiarizer`` typed as
    the common interface, never knowing which concrete engine ``manager.get_diarizer`` handed
    back.

    ``allow_local_fallback=False`` (only ever passed by ``_AsyncDiarization``, running on its
    own thread concurrently with transcription) tells NativeSpeakerDiarizer to raise instead of
    loading the in-process PyAnnote fallback on THIS thread — see its docstring for why that
    matters (issue #665). Non-native diarizers ignore the flag; there is nothing for it to gate.
    """
    from app.transcription.diarizer_native import NativeSpeakerDiarizer

    if isinstance(diarizer, NativeSpeakerDiarizer):
        return diarizer.diarize(
            audio,
            wav_path=wav_path,
            allow_local_fallback=allow_local_fallback,
            skip_sidecar=skip_sidecar,
        )
    return diarizer.diarize(audio)


def _collect_diarization(
    audio, tc, manager, hw, profiler, callback, async_diarization, wav_path: str | None = None
):
    """Diarize, taking the overlapped result when one is in flight.

    Returns ``(diarize_df, overlap_info, native_embeddings, provider, model)``, where
    ``provider``/``model`` name the engine that ACTUALLY served the request — resolved after
    any in-process fallback (issue #706), read off the diarizer instance's ``last_provider`` /
    ``last_model`` attributes (both ``NativeSpeakerDiarizer`` and ``SpeakerDiarizer`` expose
    them, set at the point each ``diarize()`` call returns — never derived from
    ``tc.diarizer_backend``, which only names what was *configured*). An overlapped attempt
    that failed falls back to a plain inline run, so the outcome is the same either way.
    ``wav_path``, when given, is Stage 1's already-materialized shared-volume WAV — passed
    through to the diarizer so it can skip re-encoding ``audio`` a second time (issue #661).

    By the time this function runs, transcription has already completed on THIS (the calling)
    thread — every caller invokes it only after ``transcriber.transcribe(audio)`` has returned.
    That holds even when ``async_diarization`` is not None: its own diarize() call has been
    running concurrently with transcription since it was constructed, but this function's own
    call to ``async_diarization.result()`` joins that thread, which cannot happen before
    transcription (on the main thread) is done. So it is always safe, here, to release the
    transcriber for an in-process diarizer — whether the overlap never happened at all, or it
    was attempted and failed mid-job (issue #665: previously ``_make_room_for_local_diarizer``
    was skipped whenever ``async_diarization`` was merely non-None, even after its attempt
    failed and fell through to an inline PyAnnote run that never freed the VRAM it needed).
    """
    from app.transcription.engine.progress import emit

    hw.log_vram_usage("after transcription, before diarizer load")
    total_vram_mb = _get_total_vram_mb()
    profiler.snapshot("models_warm_no_inference")

    emit(callback, 0.55, "Analyzing speaker patterns", "diarize")
    step_start = time.perf_counter()
    overlapped = None
    provider: str | None = None
    model: str | None = None
    with profiler.step("diarization"):
        if async_diarization is not None:
            overlapped = async_diarization.result()
        if overlapped is not None:
            result = overlapped
            provider, model = async_diarization.served_by
        else:
            # Either overlap was never attempted, or it was and failed — either way we are
            # about to diarize on this (already-past-transcription) thread, so make room for
            # an in-process diarizer first. See the docstring above for why this is safe here
            # even when async_diarization is not None.
            _make_room_for_local_diarizer(manager, hw, profiler, tc, total_vram_mb)
            diarizer = manager.get_diarizer(tc)
            # issue #656 Step 2: an overlapped attempt that failed because the sidecar was
            # unreachable/wedged/backpressured (DiarSidecarUnavailableError) must not be retried
            # at the sidecar a second time here — see _run_diarize's skip_sidecar docstring.
            skip_sidecar = async_diarization is not None and async_diarization.failed_unavailable
            result = _run_diarize(diarizer, audio, wav_path, skip_sidecar=skip_sidecar)
            provider, model = diarizer.last_provider, diarizer.last_model
    logger.info(
        "TIMING: diarization step completed in %.3fs%s",
        time.perf_counter() - step_start,
        " (overlapped with transcription)" if overlapped is not None else "",
    )
    profiler.snapshot("after_diarization")
    return (*result, provider, model)


class _AsyncDiarization:
    """Diarization running alongside transcription over the same in-memory audio.

    ``result()`` joins and returns the diarizer's tuple, or ``None`` if the attempt failed —
    the caller then runs the ordinary sequential path, so overlapping can never turn a
    working job into a failed one.
    """

    def __init__(self, audio, tc, manager, task_id: str | None, wav_path: str | None = None):
        from app.utils.benchmark_timing import mark

        self._value: tuple | None = None
        self._error: BaseException | None = None
        self._task_id = task_id
        # Captured once, up front, rather than re-fetched from `manager` later: a re-probe in
        # ModelManager._diarizer_current can swap engines between this thread starting and the
        # caller reading served_by, which would attribute this call's result to the WRONG
        # instance's (freshly-reset) last_provider/last_model (issue #706).
        self._diarizer = manager.get_diarizer(tc)
        mark(task_id, "diarize_request_sent")

        def _run() -> None:
            try:
                # allow_local_fallback=False: this runs on a daemon thread concurrently with
                # transcription on the main thread. A sidecar failure here must NOT load the
                # in-process PyAnnote fallback on this thread — see NativeSpeakerDiarizer
                # .diarize()'s docstring (issue #665). It raises instead, and result() below
                # treats that exactly like any other overlapped failure: log, return None, and
                # let the caller retry sequentially once transcription is known to be done.
                self._value = _run_diarize(
                    self._diarizer, audio, wav_path, allow_local_fallback=False
                )
            except BaseException as exc:  # noqa: BLE001 — handed to the caller, not swallowed
                self._error = exc

        self._thread = threading.Thread(target=_run, name="diarize-async", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Join the diarization thread if it hasn't been joined yet, idempotently.

        ``threading.Thread.join()`` is itself idempotent (joining an already-joined thread is a
        no-op), so this is just a documented name for it. Callers wrap the whole span from
        construction to their last use of this object in ``try/finally: async_diarization.close()``
        so an early return (e.g. no transcript segments) can never leave this daemon thread
        running past ``run()`` while holding a reference to a WAV another caller is about to
        unlink (issue #661 phase 1.1).
        """
        self._thread.join()

    def result(self) -> tuple | None:
        from app.utils.benchmark_timing import mark

        self.close()
        mark(self._task_id, "diarize_joined")
        if self._error is not None:
            logger.warning("Overlapped diarization failed (%s); retrying inline", self._error)
            return None
        return self._value

    @property
    def failed_unavailable(self) -> bool:
        """True when the overlapped attempt failed because the sidecar itself could not be
        reached/served in time (issue #656 Step 2) — never for a 422 or a genuine engine
        error, which a fresh sidecar attempt can still recover from.

        Only meaningful after ``result()`` has been called (it joins the thread first).
        ``allow_local_fallback=False`` (always the case on this thread) makes
        NativeSpeakerDiarizer.diarize() wrap the classified exception in a plain
        ``RuntimeError`` (``... from exc``) rather than raising it directly — so this checks
        ``__cause__`` too, not just the outer exception's own type.
        """
        from app.transcription.diarizer_native import DiarSidecarUnavailableError

        return isinstance(self._error, DiarSidecarUnavailableError) or isinstance(
            getattr(self._error, "__cause__", None), DiarSidecarUnavailableError
        )

    @property
    def served_by(self) -> tuple[str | None, str | None]:
        """``(provider, model)`` for a successful overlapped call, else ``(None, None)``.

        Only meaningful after ``result()`` has returned non-``None`` — a failed overlapped
        attempt never updates ``self._diarizer``'s ``last_provider``/``last_model``, so reading
        this after a failure would report whatever the diarizer's PREVIOUS call left behind.
        """
        if self._value is None:
            return None, None
        return self._diarizer.last_provider, self._diarizer.last_model


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

            return self._finalize_job_result(
                result_dict, diarize_df, audio, hw, tc, profiler, job, pipeline_start
            )
        finally:
            if async_diarization is not None:
                async_diarization.close()

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

        emit(progress_callback, 0.52, "Preparing speaker analysis", "diarize")
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
            diar_provider: str | None = None
            diar_model: str | None = None

            if tc.enable_diarization:
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
                async_diarization.close()


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
            stage_timings={**raw.stage_timings, "finalize": time.perf_counter() - t0},
            diarization_provider=raw.diarization_provider,
            diarization_model=raw.diarization_model,
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
        diar_provider: str | None = None
        diar_model: str | None = None

        if tc.enable_diarization:
            if tc.concurrent_requests > 1:
                _wait_for_vram(2000, "diarization")

            emit(callback, 0.55, "Analyzing speaker patterns", "diarize")
            step_start = time.perf_counter()
            with profiler.step("diarization"):
                diarizer = manager.get_diarizer(tc)
                diarize_df, overlap_info, native_embeddings = _run_diarize(
                    diarizer, audio, transcript.local_wav_path
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
