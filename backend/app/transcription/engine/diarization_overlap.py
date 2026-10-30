"""Overlapped / sequential diarization plumbing shared by the engine's GPU stages.

Everything about *how* diarization is run inside a stage: whether it overlaps transcription
(``_overlap_diarization_enabled``), making VRAM room for an in-process diarizer, the single
diarize call (``_run_diarize``), the join point (``_collect_diarization``) and the background
thread wrapper (``_AsyncDiarization``) with its bounded shutdown join. Split out of ``stages.py``
so the stage classes and the diarization mechanics change independently.
"""

from __future__ import annotations

import logging
import threading
import time

from app.core import stage_timing
from app.core.enums import TranscriptionStage
from app.core.worker_shutdown import shutdown_requested

logger = logging.getLogger(__name__)

#: How long a stage will wait for the overlapped-diarization thread when the worker is SHUTTING
#: DOWN, before abandoning it and letting the stage's abort propagate (issue #809).
#:
#: Derived, not tunable — a new env var here would be a knob whose only correct value is this
#: arithmetic, and getting it wrong silently reintroduces the SIGKILL this whole mechanism exists
#: to avoid:
#:
#:     30s   docker-compose.yml `stop_grace_period: ${OT_STOP_GRACE_GPU:-30}s`
#:           -- the total SIGTERM-to-SIGKILL window for every CUDA-holding service
#:   - 20s   worker_shutdown._BUDGET_S (`OT_WORKER_SHUTDOWN_BUDGET_S`), the model-release
#:           watchdog, which only STARTS once celery has drained the pool
#:   ------
#:     10s   left for everything between the checkpoint firing and the release beginning:
#:           the abort unwinding out of the stage, the task layer's Reject, celery's drain
#:   / 2  -> 5.0s for this join, leaving the other half for the rest of that path
#:
#: Unbounded (the pre-#809 behaviour) this join could block for DIAR_NATIVE_TIMEOUT_S -- 1800s by
#: default -- so docker would SIGKILL the worker at 30s and the cooperative abort would have
#: achieved nothing.
_SHUTDOWN_JOIN_BUDGET_S = 5.0


def _shutdown_join_timeout() -> float | None:
    """The join bound to pass to :meth:`_AsyncDiarization.close`, or ``None`` when healthy.

    ``None`` on the normal path is deliberate: outside a shutdown there is no deadline to race,
    and abandoning a live diarization thread would be a pure regression (it still holds a
    reference to the WAV a caller is about to unlink -- the issue #661 phase 1.1 hazard the
    unbounded join was added for).

    Imported at module scope rather than per call: ``app.core.worker_shutdown`` is stdlib-only
    (no torch, no celery), so it costs a CPU-only or bare-pytest importer nothing — the reason
    the repo's "heavy optional deps go inside the function" rule does not apply to it.
    """
    return _SHUTDOWN_JOIN_BUDGET_S if shutdown_requested() else None


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

    ``sidecar_ready()`` carries its own short TTL cache (``diar_sidecar_status.py``, issue #661
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

    from app.transcription.diar_sidecar_status import sidecar_ready

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


def _run_diarize(
    diarizer,
    audio,
    wav_path: str | None,
    *,
    allow_local_fallback: bool = True,
    skip_sidecar: bool = False,
    tc=None,
):
    """Call diarizer.diarize(), handing NativeSpeakerDiarizer a pre-existing WAV when we have one.

    ``tc`` is THIS job's ``TranscriptionConfig``. Its ``enable_native_embeddings`` and
    ``request_gender`` (the privacy policy's answers for the file) are handed to the diarizer
    as per-call arguments, never left on the diarizer's own config: the ModelManager caches
    one diarizer per worker, keyed by model, so the config it holds belongs to whichever job
    built it. Only a restriction travels this way; a missing ``tc`` restricts nothing.

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
    from app.transcription.diarization_runs import record_diarization_run
    from app.transcription.diarizer_native import NativeSpeakerDiarizer

    return_centroids = None if tc is None else bool(tc.enable_native_embeddings)
    if isinstance(diarizer, NativeSpeakerDiarizer):
        result = diarizer.diarize(
            audio,
            wav_path=wav_path,
            allow_local_fallback=allow_local_fallback,
            skip_sidecar=skip_sidecar,
            return_centroids=return_centroids,
            request_gender=None if tc is None else bool(tc.request_gender),
        )
    else:
        result = diarizer.diarize(audio, return_centroids=return_centroids)
    # After the call, so the engine that ACTUALLY served is on the instance (issue #1184).
    record_diarization_run(diarizer)
    return result


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

    emit(callback, 0.55, "Analyzing speaker patterns", TranscriptionStage.DIARIZE)
    step_start = time.perf_counter()
    overlapped = None
    provider: str | None = None
    model: str | None = None
    with stage_timing.stage("diarization"), profiler.step("diarization"):
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
            result = _run_diarize(diarizer, audio, wav_path, skip_sidecar=skip_sidecar, tc=tc)
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
                    self._diarizer, audio, wav_path, allow_local_fallback=False, tc=tc
                )
            except BaseException as exc:  # noqa: BLE001 — handed to the caller, not swallowed
                self._error = exc

        self._thread = threading.Thread(target=_run, name="diarize-async", daemon=True)
        self._thread.start()

    @property
    def _holds_no_local_cuda(self) -> bool:
        """Whether this thread is provably free of in-process CUDA state (issue #809).

        THE safety predicate for :meth:`close`'s bounded join, and the reason that join is
        conditional rather than unconditional. Abandoning a thread that holds a CUDA context is
        exactly the class of shutdown this repo has twice wedged a GPU with: the context is not
        guaranteed to be released, and no userspace command recovers the device.

        True only for ``NativeSpeakerDiarizer``, whose ``diarize()`` on this thread is a numpy
        clip/convert, a ``wave`` write, one ``urllib`` POST to the diar-native SIDECAR (a
        different container, holding its own GPU memory) and a numpy parse of the reply. There is
        no local model, no torch call, and no CUDA context in this process. Crucially,
        ``_run()`` passes ``allow_local_fallback=False``, so a sidecar failure RAISES here rather
        than loading the in-process PyAnnote fallback -- that flag is what makes the property
        hold for the whole life of the call, not just its happy path.

        It is NOT unconditionally true, which is the finding that made this a predicate:
        ``_AsyncDiarization`` is constructed only when ``_overlap_diarization_enabled`` saw a
        ready sidecar, but that probe is TTL-cached, and ``manager.get_diarizer`` can still hand
        back a plain in-process ``SpeakerDiarizer`` when ``_build_diarizer``'s native branch
        raises in that window (``model_manager._build_diarizer`` falls back on ANY exception).
        ``_run_diarize`` then calls the PyAnnote engine on this thread, which does hold CUDA. In
        that case the join below stays UNBOUNDED: a late SIGKILL is a worse outcome than a wedged
        card, and the narrow window makes it rare.
        """
        from app.transcription.diarizer_native import NativeSpeakerDiarizer

        return isinstance(self._diarizer, NativeSpeakerDiarizer)

    def close(self, timeout: float | None = None) -> None:
        """Join the diarization thread if it hasn't been joined yet, idempotently.

        ``threading.Thread.join()`` is itself idempotent (joining an already-joined thread is a
        no-op). Callers wrap the whole span from construction to their last use of this object in
        ``try/finally: async_diarization.close()`` so an early return (e.g. no transcript
        segments) can never leave this daemon thread running past ``run()`` while holding a
        reference to a WAV another caller is about to unlink (issue #661 phase 1.1).

        Args:
            timeout: Bound the join, in seconds, instead of waiting indefinitely. Passed only
                during a worker shutdown (:func:`_shutdown_join_timeout`), and HONORED only when
                :attr:`_holds_no_local_cuda` says the thread cannot be holding a CUDA context.

                Issue #809: unbounded, this ``finally`` defeats the entire cooperative abort. A
                checkpoint fires, the abort starts unwinding, and then the stage blocks here
                waiting on a sidecar request that may have up to ``DIAR_NATIVE_TIMEOUT_S``
                (1800s) left to run -- 60x docker's 30s grace period, so the worker is SIGKILLed
                mid-CUDA anyway and nothing was gained.
        """
        if timeout is None:
            self._thread.join()
            return

        if not self._holds_no_local_cuda:
            # See _holds_no_local_cuda: this thread fell through to the in-process PyAnnote
            # engine, so abandoning it risks orphaning a CUDA context on the device.
            logger.warning(
                "worker shutdown: overlapped diarization for task %s is running the in-process "
                "engine, which may hold a CUDA context -- waiting for it rather than abandoning "
                "it, even though that risks a SIGKILL",
                self._task_id,
            )
            self._thread.join()
            return

        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning(
                "worker shutdown: abandoning the overlapped diarization thread for task %s after "
                "%.1fs -- it is blocked on the diar-native sidecar (another container) and holds "
                "no CUDA state in this process, so the abort proceeds without it. The task is "
                "requeued; the redelivered attempt re-runs diarization from the start.",
                self._task_id,
                timeout,
            )

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
        from app.transcription.diar_sidecar_status import DiarSidecarUnavailableError

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


def _get_total_vram_mb() -> int:
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.get_device_properties(0).total_memory / (1024**2))
    except Exception as e:
        logger.debug(f"VRAM query failed: {e}")
    return 0
