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

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from celery.exceptions import Reject

from app.core import worker_shutdown as ws


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
