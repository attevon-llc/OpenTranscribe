"""Cooperative abort on worker shutdown (issue #809).

#782 gave a stopping GPU worker a measured 30s grace period and a handler that releases the
GPU. That covers an idle or between-stages worker. A worker MID-DECODE was still SIGKILLed,
because every GPU worker runs ``--pool=threads`` and celery's warm shutdown calls
``executor.shutdown(wait=True)`` — which blocks ``worker_shutdown`` until the running task
returns. No signal handler can preempt a decode already in flight, so the task has to stand
down on its own.

What these tests pin, in the order the abort travels:

1. the checkpoint raises only when the flag is armed;
2. the decode loop actually REACHES a checkpoint — the property #809 asks for, since a
   checkpoint nothing reaches is indistinguishable from no checkpoint at all;
3. the task layer turns an abort into ``Reject(requeue=True)`` and a failure into the
   original exception — the two outcomes are different and must not share a path.
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from celery.exceptions import Reject

from app.core import worker_shutdown as ws

_ABORT_EXC_NAME = "TranscriptionAbortedError"


@pytest.fixture(autouse=True)
def _clear_shutdown_flag():
    """The flag is a process-global Event. Restore it, or one test arms it for the next."""
    was_set = ws._SHUTDOWN.is_set()
    ws._SHUTDOWN.clear()
    yield
    (ws._SHUTDOWN.set() if was_set else ws._SHUTDOWN.clear())


class TestCheckpoint:
    def test_it_does_not_raise_while_the_worker_is_healthy(self):
        """The overwhelmingly common case. A checkpoint that fired spuriously would abort
        every transcription on the host."""
        assert not ws._SHUTDOWN.is_set(), (
            "precondition: the autouse fixture must hand this test a healthy worker. Asserted "
            "rather than assumed — if the flag leaked in set, the call below would raise and "
            "this test would fail for a reason that has nothing to do with what it checks."
        )
        # Called for its (absence of) effect: the contract is "does not raise". Asserting on
        # the return value instead is a mypy error (`func-returns-value`) — the function is
        # declared `-> None`, so `... is None` is trivially true and checks nothing.
        ws.raise_if_shutting_down("unit-test")

        assert not ws._SHUTDOWN.is_set(), (
            "the checkpoint must be a pure read — a healthy call that armed the flag would "
            "abort every subsequent stage on this worker"
        )

    def test_it_raises_once_shutdown_is_signalled(self):
        ws.mark_shutting_down()
        with pytest.raises(ws.TranscriptionAbortedError) as exc:
            ws.raise_if_shutting_down("unit-test-checkpoint")
        assert "unit-test-checkpoint" in str(exc.value), (
            "the abort must name WHICH checkpoint stood down — with several checkpoints, "
            "'a checkpoint fired' does not locate the stage"
        )

    def test_the_abort_is_not_a_celery_exception(self):
        """The engine must stay importable on a CPU-only worker and in a bare pytest run,
        so the abort type cannot come from celery. The TASK layer does the translation."""
        assert not issubclass(ws.TranscriptionAbortedError, Reject)


class TestDecodeLoopReachesTheCheckpoint:
    """#809's 'a test that fails if the checkpoint is removed'.

    Asserted through real behaviour rather than by grepping the source: the loop is driven
    with a generator that would yield three segments, the flag is armed, and the abort has
    to surface. Delete the checkpoint and this test hangs onto all three segments and
    returns normally instead of raising.
    """

    def _fake_segment(self, start: float, end: float):
        seg = MagicMock()
        seg.start, seg.end, seg.words = start, end, []
        return seg

    def test_an_armed_flag_aborts_the_segment_loop(self):
        from app.transcription.transcriber import Transcriber

        # __new__ + the two attributes transcribe() actually reads, rather than a real
        # load_model(): this test is about the checkpoint, and loading Whisper to reach a
        # `for` loop would make it a GPU test.
        transcriber = Transcriber.__new__(Transcriber)
        transcriber.config = MagicMock()
        consumed: list[int] = []

        def _segments():
            for i in range(3):
                consumed.append(i)
                yield self._fake_segment(float(i), float(i) + 1.0)

        pipeline = MagicMock()
        pipeline.transcribe.return_value = (_segments(), MagicMock(language="en"))
        transcriber._pipeline = pipeline

        ws.mark_shutting_down()
        with pytest.raises(ws.TranscriptionAbortedError):
            transcriber.transcribe(audio=[0.0] * 16000)

        assert len(consumed) <= 1, (
            f"abort latency is meant to be bounded to ONE decode batch; the loop consumed "
            f"{len(consumed)} segments before standing down"
        )


class TestTaskLayerTranslation:
    """The abort/failure split, at the boundary where celery semantics start mattering."""

    def test_an_abort_becomes_reject_requeue_and_does_not_fail_the_file(self):
        from app.tasks.transcription import core

        with (
            patch.object(core, "_handle_transcription_failure") as failed,
            patch.object(core, "_cleanup_wav_quietly"),
        ):
            with pytest.raises(Reject) as raised:
                core._finish_failed_or_aborted(
                    MagicMock(), "task-1", "file-uuid-1", "", ws.TranscriptionAbortedError("x")
                )

        assert raised.value.requeue is True, (
            "requeue=False would ACK the message under acks_late=True and LOSE the "
            "transcription — worse than the SIGKILL this replaces, which redelivers"
        )
        failed.assert_not_called()  # interrupted work is not broken work

    def test_a_real_failure_still_fails_the_file_and_re_raises(self):
        """The control. Without it, a handler that Rejected on EVERYTHING would pass the
        test above while silently converting every genuine failure into an infinite requeue."""
        from app.tasks.transcription import core

        boom = RuntimeError("cuda oom")
        with (
            patch.object(core, "_handle_transcription_failure") as failed,
            patch.object(core, "_cleanup_wav_quietly"),
            patch.object(core, "_get_user_friendly_error_message", return_value="friendly"),
        ):
            with pytest.raises(RuntimeError) as raised:
                core._finish_failed_or_aborted(MagicMock(), "task-2", "file-uuid-2", "", boom)

        assert raised.value is boom
        failed.assert_called_once()

    def test_the_wav_is_cleaned_up_on_both_paths(self):
        """Whichever way the task ends, the shared-volume WAV must not be left behind."""
        from app.tasks.transcription import core

        for exc in (ws.TranscriptionAbortedError("x"), RuntimeError("boom")):
            with (
                patch.object(core, "_handle_transcription_failure"),
                patch.object(core, "_get_user_friendly_error_message", return_value="f"),
                patch.object(core, "_cleanup_wav_quietly") as cleanup,
            ):
                with pytest.raises((Reject, RuntimeError)):
                    core._finish_failed_or_aborted(
                        MagicMock(), "t", "file-uuid", "wav-sentinel-path", exc
                    )
            cleanup.assert_called_once_with("wav-sentinel-path")


# ── The registry gate ────────────────────────────────────────────────────────────────────
#
# An `acks_late=True` task acks on RETURN — success OR exception. So a task that lets a
# TranscriptionAbortedError reach a generic `except Exception` marks the file ERROR and then
# ACKS the message: the work is silently lost, which is strictly worse than the SIGKILL this
# whole mechanism replaces (a SIGKILL at least redelivers). The gate below walks the LIVE
# celery registry rather than a hand-kept list, because the failure mode that motivated #809's
# P0 was precisely that two such tasks existed and nobody had enumerated them.
#
# Keyed `<module>::<task_name>`; the reason is MANDATORY and must say why no cooperative-abort
# checkpoint is reachable from that task's call graph. "It has never aborted" is not a reason —
# the question is whether it *could*.
_ABORT_HANDLING_EXEMPT: dict[str, str] = {
    "app.tasks.transcription.preprocess::transcription.preprocess": (
        "Stage 1 is ffmpeg decode + MinIO upload via audio_processor; it never constructs an "
        "Engine stage, so no raise_if_shutting_down checkpoint is reachable from this call "
        "graph. Add a handler here the moment _PreprocessStage gains a checkpoint."
    ),
    "app.tasks.transcription.postprocess::transcription.postprocess": (
        "Stage 3 is CPU finalize/index/dispatch — it consumes an already-produced result and "
        "runs no engine stage, so no checkpoint is reachable."
    ),
    "app.tasks.recovery::system.startup_recovery": (
        "Reclaims files stuck in PROCESSING by querying Postgres and the celery inspect API; "
        "runs no engine stage, so no checkpoint is reachable."
    ),
    "app.tasks.recovery::system.recover_user_files": (
        "Same DB-only reclamation as system.startup_recovery, scoped to one user; runs no "
        "engine stage."
    ),
    "app.tasks.recovery::system.health_check": (
        "Periodic DB sweep for orphaned PROCESSING rows; runs no engine stage."
    ),
    "app.tasks.speaker_clustering::speaker.cluster_for_file": (
        "Clusters already-persisted speaker embeddings via OpenSearch kNN; it never loads an "
        "ASR/diarization model and runs no engine stage, so no checkpoint is reachable."
    ),
    "app.tasks.speaker_clustering::speaker.recluster_all": (
        "Corpus-wide variant of speaker.cluster_for_file — same embedding/OpenSearch-only call "
        "graph, no engine stage."
    ),
}


def _abort_names_in(node: ast.AST | None) -> set[str]:
    """Every dotted-tail identifier an ``except`` clause (or isinstance arg) names.

    Handles the three shapes an exception reference takes: a bare ``Name``
    (``except TranscriptionAbortedError``), an ``Attribute``
    (``except ws.TranscriptionAbortedError``), and a ``Tuple`` of either
    (``except (A, TranscriptionAbortedError)``).
    """
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        return {name for elt in node.elts for name in _abort_names_in(elt)}
    return set()


def _module_handles_abort(source: str) -> bool:
    """Whether *source* STRUCTURALLY handles ``TranscriptionAbortedError``.

    AST only — a mention in a docstring, a comment, or a log string does not count, which is
    the whole point: a substring search over this repo's task modules matches the prose in
    half of them.

    Two accepted shapes, because the codebase legitimately uses both:

    1. ``except TranscriptionAbortedError`` — a dedicated handler (``cpu_task``,
       ``diarize_task``).
    2. ``isinstance(exc, TranscriptionAbortedError)`` — a type dispatch inside a terminal
       handler. ``core.py``'s ``_finish_failed_or_aborted`` takes this shape deliberately:
       ``transcribe_gpu_task``'s body sits at the C901 ceiling, so the abort/failure split
       happens one frame down rather than as a second ``except`` clause. Rejecting it would
       push the module toward a worse structure to satisfy a test.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _ABORT_EXC_NAME in _abort_names_in(node.type):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
            and _ABORT_EXC_NAME in _abort_names_in(node.args[1])
        ):
            return True
    return False


@pytest.fixture(scope="module")
def acks_late_tasks() -> dict[str, str]:
    """``{task_name: defining module}`` for every registered ``acks_late=True`` task.

    Derived from the LIVE registry with the lazy ``include=`` modules imported — the same
    approach ``test_celery_app_resolution.py`` uses — so a new acks_late task is covered the
    moment it is registered, with no list here to keep in sync.
    """
    from app.core.celery import celery_app

    celery_app.loader.import_default_modules()
    celery_app.finalize()

    found: dict[str, str] = {}
    for name, task in celery_app.tasks.items():
        if not getattr(task, "acks_late", False):
            continue
        # `__module__` is what the decorator stamps for a function-based task; falling back
        # through inspect covers a task object that only knows its defining module reflectively.
        # Annotated because both getattr chains are untyped — a bare assignment widens the
        # dict's value type to Any and stops mypy checking every later use of it.
        module: str = str(
            getattr(task, "__module__", None) or getattr(inspect.getmodule(task), "__name__", "")
        )
        found[name] = module
    return found


class TestEveryAcksLateTaskHandlesTheAbort:
    """#809 P0-1's regression gate.

    ``diarize_gpu_task`` and ``transcribe_cpu_task`` both ran a ``TranscriptionAbortedError``
    through ``_handle_transcription_failure`` — marking the file ERROR and acking the message
    — until this gate was written. The point of walking the registry is that the *next* such
    task is caught when it is added, not after it has silently dropped a transcription.
    """

    def test_the_registry_actually_yields_acks_late_tasks(self, acks_late_tasks):
        """Guard the guard. If the registry import ever stopped populating, every assertion
        below would iterate an empty dict and pass while checking nothing."""
        assert len(acks_late_tasks) >= 8, (
            f"only {len(acks_late_tasks)} acks_late tasks found ({sorted(acks_late_tasks)}); "
            "the registry import is not populating, so the gate below would pass vacuously"
        )
        assert "transcription.gpu_transcribe" in acks_late_tasks, (
            "the task #809 was filed about is missing from the registry — the fixture is not "
            "importing the transcription modules"
        )

    def test_every_acks_late_task_handles_the_abort_or_is_exempt(self, acks_late_tasks):
        offenders: list[str] = []
        checked = 0

        for task_name, module_name in sorted(acks_late_tasks.items()):
            key = f"{module_name}::{task_name}"
            if key in _ABORT_HANDLING_EXEMPT:
                continue
            module = sys.modules.get(module_name)
            source_file = getattr(module, "__file__", None)
            if source_file is None:
                offenders.append(f"{key} (module source not resolvable)")
                continue
            checked += 1
            if not _module_handles_abort(Path(source_file).read_text(encoding="utf-8")):
                offenders.append(key)

        assert checked > 0, (
            "no non-exempt acks_late task was actually inspected — either everything got "
            "exempted or the module sources stopped resolving; either way this gate is dead"
        )
        assert not offenders, (
            "these acks_late=True tasks neither handle TranscriptionAbortedError nor carry an "
            f"exemption: {offenders}.\n"
            "An abort reaching a generic `except Exception` marks the file ERROR and then ACKS "
            "the message (acks_late acks on RETURN), silently losing the work — worse than the "
            "SIGKILL #809 replaces, which at least redelivers. Add an abort branch that calls "
            "context.requeue_after_abort BEFORE the generic handler, or add a "
            "`<module>::<task_name>` entry to _ABORT_HANDLING_EXEMPT with a written reason "
            "explaining why no checkpoint is reachable from that task's call graph."
        )

    def test_every_exemption_names_a_task_that_still_exists(self, acks_late_tasks):
        """A stale exemption is worse than none: it silently covers a task that has since
        gained a checkpoint, or one that no longer exists at all."""
        live_keys = {f"{module}::{name}" for name, module in acks_late_tasks.items()}
        stale = sorted(set(_ABORT_HANDLING_EXEMPT) - live_keys)

        assert not stale, (
            f"these _ABORT_HANDLING_EXEMPT entries name no live acks_late task: {stale}. "
            "Delete them — an exemption that matches nothing is a comment pretending to be a "
            "gate."
        )

    def test_every_exemption_carries_a_real_reason(self):
        thin = sorted(k for k, reason in _ABORT_HANDLING_EXEMPT.items() if len(reason.strip()) < 40)

        assert not thin, (
            f"these exemptions have no substantive written reason: {thin}. The reason must say "
            "why no cooperative-abort checkpoint is reachable from that task's call graph."
        )


class TestTheAbortDetectorItself:
    """Must-fire / must-stay-clean cases for ``_module_handles_abort``.

    A detector that silently matches nothing reports zero offenders, which is indistinguishable
    from a clean tree — the exact failure this repo's audit tooling exists to prevent.
    """

    def test_it_fires_on_a_dedicated_except_handler(self):
        assert _module_handles_abort(
            textwrap.dedent(
                """
                def task():
                    try:
                        work()
                    except TranscriptionAbortedError as abort:
                        requeue_after_abort("f", abort, stage="x")
                """
            )
        )

    def test_it_fires_on_a_tuple_handler(self):
        assert _module_handles_abort(
            textwrap.dedent(
                """
                def task():
                    try:
                        work()
                    except (ValueError, TranscriptionAbortedError):
                        raise
                """
            )
        )

    def test_it_fires_on_an_isinstance_dispatch(self):
        assert _module_handles_abort(
            textwrap.dedent(
                """
                def terminal(exc):
                    if isinstance(exc, TranscriptionAbortedError):
                        requeue_after_abort("f", exc, stage="x")
                """
            )
        )

    def test_it_fires_on_a_dotted_reference(self):
        assert _module_handles_abort(
            textwrap.dedent(
                """
                def task():
                    try:
                        work()
                    except ws.TranscriptionAbortedError:
                        raise
                """
            )
        )

    def test_it_stays_clean_on_a_module_that_only_mentions_the_abort(self):
        """The must-stay-clean case, and the reason the detector is AST-based. Every one of
        these mentions the class name; none of them handles it."""
        assert not _module_handles_abort(
            textwrap.dedent(
                '''
                """This task predates TranscriptionAbortedError handling."""

                ABORT_DOC = "raises TranscriptionAbortedError on shutdown"

                def task():
                    # TranscriptionAbortedError is handled by the caller
                    try:
                        work()
                    except Exception:
                        logger.error("not a TranscriptionAbortedError")
                        raise
                '''
            )
        )

    def test_it_stays_clean_on_an_unrelated_isinstance(self):
        assert not _module_handles_abort(
            textwrap.dedent(
                """
                def task(exc):
                    if isinstance(exc, ConnectionError):
                        raise
                """
            )
        )


_ABORT_PROBE_CONTEXT = {
    "task_id": "task-abort",
    "file_uuid": "file-uuid-abort",
    "file_id": 7,
    "user_id": 3,
    "storage_path": "s/p",
    "file_name": "f.wav",
    "content_type": "audio/wav",
}


@pytest.fixture
def diarize_seams():
    """Silence ``diarize_gpu_task``'s DB/notification edges and hand back the failure-path spy.

    One fixture rather than a ``with`` stack per test: the four tests below share an identical
    seam set, and the only thing that varies between them is which exception the stage raises.
    Patching the same six names in each body made the DIFFERENCE between an abort and a failure
    — the entire point — the least visible thing in the test.

    ``_handle_transcription_failure`` is deliberately the ONLY seam yielded: it is the
    observable that distinguishes the two outcomes (an abort must not touch it, a failure must),
    so the assertions read against one object instead of six.
    """
    from app.tasks.transcription import diarize_task as dt

    with (
        patch.object(dt, "session_scope"),
        patch.object(dt, "update_task_status"),
        patch.object(dt, "send_progress_notification"),
        patch.object(dt, "benchmark_timing"),
        patch.object(dt, "_get_user_friendly_error_message", return_value="friendly"),
        patch.object(dt, "_handle_transcription_failure") as failed,
    ):
        yield failed


@pytest.fixture
def cpu_seams():
    """``transcribe_cpu_task``'s equivalent of :func:`diarize_seams`."""
    from app.tasks.transcription import cpu_task as ct

    with (
        patch.object(ct, "session_scope"),
        patch.object(ct, "update_task_status"),
        patch.object(ct, "benchmark_timing"),
        patch.object(ct, "_get_user_friendly_error_message", return_value="friendly"),
        patch.object(ct, "_handle_transcription_failure") as failed,
    ):
        yield failed


class TestTheTwoNewlyProtectedTasks:
    """#809 P0-1 behaviour, at the two tasks that had no abort branch at all.

    Driven through each task's real ``except`` chain — the handlers are what the fix added, so
    a test that called ``requeue_after_abort`` directly would pass with the branch deleted.
    Each test raises from the first real call the task makes after entering its ``try``, which
    is where a stage checkpoint's abort would surface from.
    """

    def test_diarize_task_requeues_instead_of_failing_the_file(self, diarize_seams):
        from app.tasks.transcription import diarize_task as dt

        with patch(
            "app.transcription.engine.job.RawTranscriptResult.deserialize",
            side_effect=ws.TranscriptionAbortedError("stood down"),
        ):
            with pytest.raises(Reject) as raised:
                dt.diarize_gpu_task.run({}, dict(_ABORT_PROBE_CONTEXT))

        assert raised.value.requeue is True, (
            "requeue=False acks the message under acks_late=True and loses the diarization"
        )
        # Interrupted work is not broken work: no ERROR status, no error notification.
        diarize_seams.assert_not_called()

    def test_diarize_task_still_fails_the_file_on_a_real_error(self, diarize_seams):
        """The control. Without it, a handler that Rejected on everything would pass the test
        above while converting every genuine diarization failure into an endless requeue."""
        from app.tasks.transcription import diarize_task as dt

        boom = RuntimeError("cuda oom")
        with patch(
            "app.transcription.engine.job.RawTranscriptResult.deserialize", side_effect=boom
        ):
            with pytest.raises(RuntimeError) as raised:
                dt.diarize_gpu_task.run({}, dict(_ABORT_PROBE_CONTEXT))

        assert raised.value is boom
        diarize_seams.assert_called_once()

    def test_cpu_task_requeues_instead_of_failing_the_file(self, cpu_seams):
        from app.tasks.transcription import cpu_task as ct

        with patch(
            "app.services.minio_service.download_temp_audio",
            side_effect=ws.TranscriptionAbortedError("stood down"),
        ):
            with pytest.raises(Reject) as raised:
                ct.transcribe_cpu_task.run(dict(_ABORT_PROBE_CONTEXT))

        assert raised.value.requeue is True, (
            "requeue=False acks the message under acks_late=True and loses the transcription"
        )
        # Interrupted work is not broken work: no ERROR status, no error notification.
        cpu_seams.assert_not_called()

    def test_cpu_task_still_fails_the_file_on_a_real_error(self, cpu_seams):
        """The control, for the same reason as the diarize one above."""
        from app.tasks.transcription import cpu_task as ct

        boom = RuntimeError("decoder exploded")
        with patch("app.services.minio_service.download_temp_audio", side_effect=boom):
            with pytest.raises(RuntimeError) as raised:
                ct.transcribe_cpu_task.run(dict(_ABORT_PROBE_CONTEXT))

        assert raised.value is boom
        cpu_seams.assert_called_once()
