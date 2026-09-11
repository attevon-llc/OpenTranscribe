"""User-initiated cancel actually stops the GPU, and stops only the file that was cancelled.

Issue #823. ``cancel_active_task`` called ``revoke(terminate=True)`` and flipped the file to
``CANCELLED``; the decode carried on holding VRAM while the UI said it had stopped. The fix
routes user-cancel through the SAME cooperative checkpoints issue #809 built for worker
shutdown, with a per-run signal instead of #809's process-wide flag and a terminal outcome
instead of #809's requeue.

Four properties are pinned here, in rising order of "easy to get wrong":

1. **The checkpoint fires for the cancelled run.** ``stand_down_if_requested`` raises
   ``TranscriptionCancelledError`` at a stage boundary, and the engine stage stands down
   *before* loading a model — the same assertion shape ``test_cooperative_abort.py`` uses for
   the shutdown trigger.
2. **It fires for NOTHING ELSE.** ``TestOnlyTheCancelledRunStandsDown`` is the centrepiece: a
   naive implementation reuses #809's global ``threading.Event`` and aborts every job on the
   worker. Two of those tests run genuinely concurrent threads, because a per-thread
   ``ContextVar`` and a module-level global are indistinguishable in a single-threaded test.
3. **A cancelled job is NOT requeued.** #809's outcome is ``Reject(requeue=True)`` — correct
   for a shutdown, catastrophic for a cancel: the file would go back to processing seconds
   after the user stopped it. ``TranscriptionCancelledError`` must therefore not be a subclass
   of ``TranscriptionAbortedError``, or the tasks' existing abort handler would catch it first.
4. **The status distinguishes the request from the confirmed stop.** ``CANCELLING`` is written
   by the API, ``CANCELLED`` only by the worker that actually stood down.

**Nothing here reaches a real Redis, a real broker or a GPU.** The Redis client is a
dict-backed stand-in; the engine stage tests patch the model manager and the audio loader; the
DB tests run on the savepoint session.

⚠️ **This file REPLACES ``test_cancel_active_task_pool_limitation.py``**, which characterized
the broken behaviour and said so in its own docstring: *"replace this assertion once #809's
cooperative-abort checkpoint lands and this function can condition the status flip on confirmed
termination instead."* Every assertion it made has a successor here — its ``revoke`` assertion
inverted (``test_it_no_longer_calls_revoke``), its optimistic-``CANCELLED`` characterization
inverted (``test_the_file_moves_to_cancelling_not_cancelled``), and its log-honesty check
narrowed to the one path that still writes ``CANCELLED`` unconfirmed
(``test_the_unarmed_fallback_says_the_work_may_still_be_running``). Leaving the old file in
place would have kept a suite whose name and docstring assert a limitation that no longer
exists.
"""

from __future__ import annotations

import ast
import threading
import uuid as uuid_module
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core import task_cancellation as tcancel
from app.core import worker_shutdown as ws
from app.core.task_cancellation import TranscriptionCancelledError
from app.core.task_cancellation import cancellation_scope
from app.core.task_cancellation import current_cancellation_task_id
from app.core.task_cancellation import request_cancel
from app.core.task_cancellation import stand_down_if_requested
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task

_CANCELLATION = "app.tasks.transcription.cancellation"
_GET_REDIS = "app.core.redis.get_redis"


class _StandInRedis:
    """The three operations the cancellation flag uses, plus a call counter.

    The counter is what makes the poll-throttle test possible: without it, "the checkpoint
    does not hammer Redis" is unobservable and the constant could be dead.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.exists_calls = 0

    def exists(self, key: str) -> int:
        self.exists_calls += 1
        return 1 if key in self.store else 0

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class _ExplodingRedis:
    """Every operation raises — stands in for an unreachable Redis."""

    def exists(self, key: str):
        raise ConnectionError("redis is down")

    def setex(self, key: str, _ttl: int, value: str):
        raise ConnectionError("redis is down")

    def delete(self, key: str):
        raise ConnectionError("redis is down")


@pytest.fixture
def fake_redis():
    """Point every ``get_redis()`` call site in the cancellation plane at a dict."""
    client = _StandInRedis()
    with patch(_GET_REDIS, return_value=client):
        yield client


@pytest.fixture(autouse=True)
def _healthy_worker():
    """Every test starts from a worker that is NOT shutting down.

    ``worker_shutdown._SHUTDOWN`` is a module-level ``threading.Event`` shared by the whole
    process, so a test that arms it would otherwise make every later test in this file raise
    ``TranscriptionAbortedError`` from the shutdown half of the checkpoint — which reads as the
    cancel half working.
    """
    ws._SHUTDOWN.clear()
    yield
    ws._SHUTDOWN.clear()


# ──────────────────────────────────────────────────────────────────────────────────────────
# 1. The exception taxonomy — the property the whole "do not requeue" outcome rests on
# ──────────────────────────────────────────────────────────────────────────────────────────


class TestTheCancelIsNotAnAbort:
    def test_cancelled_is_not_a_subclass_of_the_shutdown_abort(self):
        """LOAD-BEARING, not taxonomy.

        All three transcription tasks carry ``except TranscriptionAbortedError`` ->
        ``requeue_after_abort`` -> ``Reject(requeue=True)``. If the cancel exception were a
        subclass, that clause would catch it first and the broker would redeliver the job the
        user just cancelled — it would come back on the next worker and the file would return
        to processing. Make it a subclass and this is the test that goes red.
        """
        assert not issubclass(TranscriptionCancelledError, ws.TranscriptionAbortedError)
        assert not issubclass(ws.TranscriptionAbortedError, TranscriptionCancelledError)

    def test_the_checkpoint_names_where_it_fired(self, fake_redis):
        request_cancel("run-1", "file-uuid-1")
        with cancellation_scope("run-1", "file-uuid-1"):
            with pytest.raises(TranscriptionCancelledError) as exc:
                stand_down_if_requested("a-named-checkpoint")
        assert "a-named-checkpoint" in str(exc.value)


# ──────────────────────────────────────────────────────────────────────────────────────────
# 2. THE CENTREPIECE — cancelling one run must not touch any other
# ──────────────────────────────────────────────────────────────────────────────────────────


class TestOnlyTheCancelledRunStandsDown:
    """#809's signal is one process-wide ``threading.Event``; reusing it here would abort every
    transcription on the worker whenever any one of them was cancelled. Every test in this
    class fails against such an implementation."""

    def test_a_sibling_run_in_the_same_process_is_untouched(self, fake_redis):
        request_cancel("run-cancelled", "uuid-cancelled")

        with cancellation_scope("run-cancelled", "uuid-cancelled"):
            with pytest.raises(TranscriptionCancelledError):
                stand_down_if_requested("checkpoint")

        with cancellation_scope("run-healthy", "uuid-healthy"):
            stand_down_if_requested("checkpoint")  # must NOT raise

    def test_a_concurrent_sibling_task_runs_to_completion(self, fake_redis):
        """The one shape a single-threaded test cannot distinguish.

        ⚠️ **A barrier alone is NOT enough here, and an earlier draft of this test was wrong
        because of it.** Releasing both threads from a barrier guarantees both scopes are
        *entered*, not that both are *checking* while the other is still bound: with the GIL
        and a five-iteration body, the cancelled thread reliably ran to completion and left its
        scope before the sibling was next scheduled — so a shared-global implementation passed.
        The explicit hand-off below is what makes the overlap real: the sibling performs its
        checkpoints strictly **after** the cancelled run has observed its flag and **before**
        that run's scope is unwound, which is exactly the window ``--pool=threads`` puts two
        transcriptions in.
        """
        request_cancel("run-A", "uuid-A")

        sibling_bound = threading.Event()  # B's scope is live
        cancelled_observed = threading.Event()  # A has seen its flag, still inside its scope
        sibling_observed = threading.Event()  # B has done its checks
        results: dict[str, object] = {}

        def cancelled_worker():
            sibling_bound.wait(timeout=10)
            with cancellation_scope("run-A", "uuid-A"):
                try:
                    for _ in range(5):
                        stand_down_if_requested("A-checkpoint")
                    results["A"] = "never-stood-down"
                except TranscriptionCancelledError as exc:
                    results["A"] = type(exc).__name__
                cancelled_observed.set()
                # Hold this run's scope OPEN across B's checks. Leaving it here is what let a
                # global implementation restore B's binding and slip past.
                sibling_observed.wait(timeout=10)

        def healthy_worker():
            with cancellation_scope("run-B", "uuid-B"):
                sibling_bound.set()
                cancelled_observed.wait(timeout=10)
                try:
                    for _ in range(5):
                        stand_down_if_requested("B-checkpoint")
                    results["B"] = "completed"
                except BaseException as exc:  # noqa: BLE001 - the failure IS what we report
                    results["B"] = f"{type(exc).__name__}: {exc}"
                sibling_observed.set()

        threads = [
            threading.Thread(target=cancelled_worker),
            threading.Thread(target=healthy_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), "a worker thread hung — the hand-off never completed"

        assert results["A"] == "TranscriptionCancelledError", (
            "the CANCELLED run did not stand down; the checkpoint is not reading the flag"
        )
        assert results["B"] == "completed", (
            f"a concurrently-running SIBLING transcription was stood down too: {results['B']}. "
            "The cancellation signal is not scoped to one run — this is the exact regression "
            "that reusing #809's process-wide _SHUTDOWN event produces."
        )

    def test_two_concurrent_scopes_read_their_own_run_id(self, fake_redis):
        """Guard the guard for the test above.

        The binding itself, with no Redis in the picture: two live scopes must report two
        different run ids. A shared module-level binding makes the second entrant overwrite the
        first, so both threads report the SAME id — and if that were true, the sibling test
        above could pass for the wrong reason (B reading run-B's flag only because it happened
        to be the last writer).

        Sequenced with events rather than a barrier, for the reason spelled out above: a
        barrier only proves both scopes were entered, and a thread that reads *after* the other
        has already unwound is not observing an overlap at all.
        """
        seen: dict[str, str | None] = {}
        y_bound = threading.Event()
        x_read = threading.Event()
        y_read = threading.Event()

        def x_observer():
            y_bound.wait(timeout=10)
            with cancellation_scope("run-X", "uuid-run-X"):
                seen["run-X"] = current_cancellation_task_id()
                x_read.set()
                # Hold run-X's scope bound across Y's read; without this the unwind races Y
                # and a shared binding would restore run-Y's value in time to look correct.
                y_read.wait(timeout=10)

        def y_observer():
            with cancellation_scope("run-Y", "uuid-run-Y"):
                y_bound.set()
                x_read.wait(timeout=10)
                # Read while run-X's scope is provably still live — under a shared binding this
                # reports "run-X".
                seen["run-Y"] = current_cancellation_task_id()
                y_read.set()

        threads = [threading.Thread(target=x_observer), threading.Thread(target=y_observer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), "an observer thread hung — the hand-off never completed"

        assert seen == {"run-X": "run-X", "run-Y": "run-Y"}

    def test_the_binding_does_not_leak_into_the_next_task_on_a_reused_thread(self, fake_redis):
        """Celery's threads pool REUSES its threads, so a scope that is not unwound would make
        the next task on that thread poll the previous task's flag — a cancelled file would
        cancel whatever ran after it."""
        request_cancel("run-first", "uuid-first")

        with cancellation_scope("run-first", "uuid-first"):
            with pytest.raises(TranscriptionCancelledError):
                stand_down_if_requested("checkpoint")

        assert current_cancellation_task_id() is None, "the scope leaked past its context manager"

        # The same thread now runs the next task, exactly as the pool would.
        with cancellation_scope("run-second", "uuid-second"):
            assert current_cancellation_task_id() == "run-second"
            stand_down_if_requested("checkpoint")  # must NOT raise

    def test_an_unrelated_flag_does_not_stand_this_run_down(self, fake_redis):
        """The flag is keyed per run, so a leftover from an unrelated (or earlier) run is
        invisible here. This is why #823 needs none of #691's "the flag names the run"
        machinery: the key IS the run."""
        request_cancel("some-other-run", "uuid-other")
        with cancellation_scope("my-run", "uuid-mine"):
            stand_down_if_requested("checkpoint")
        assert fake_redis.exists_calls >= 1, "the checkpoint never actually consulted Redis"


# ──────────────────────────────────────────────────────────────────────────────────────────
# 3. The checkpoint's own contract
# ──────────────────────────────────────────────────────────────────────────────────────────


class TestTheCheckpoint:
    def test_outside_a_scope_it_never_touches_redis(self, fake_redis):
        """The must-stay-clean control. The engine is also driven by the benchmark harness, the
        eval runner and plain unit tests; none of those has a run to cancel, and making them
        pay a broker round trip per decoded segment would be a real regression."""
        stand_down_if_requested("no-scope-bound")
        assert fake_redis.exists_calls == 0

    def test_it_polls_at_most_once_per_interval(self, fake_redis):
        """``transcriber.transcribe`` calls this once per decoded segment — thousands of times
        on a long file. Without the throttle that is a Redis round trip in the ASR hot loop."""
        with cancellation_scope("run-hot", "uuid-hot"):
            for _ in range(2000):
                stand_down_if_requested("segment-loop")
        assert fake_redis.exists_calls == 1, (
            f"the checkpoint issued {fake_redis.exists_calls} Redis reads for 2000 calls; the "
            "poll throttle is not in effect"
        )

    def test_it_does_poll_again_after_the_interval(self, fake_redis, monkeypatch):
        """The other half of the throttle: a cancel arriving mid-file must still be SEEN.
        Without this, a throttle that simply cached forever would pass the test above."""
        monkeypatch.setattr(tcancel, "CANCEL_POLL_INTERVAL_S", 0.0)
        with cancellation_scope("run-hot", "uuid-hot"):
            stand_down_if_requested("segment-loop")
            request_cancel("run-hot", "uuid-hot")
            with pytest.raises(TranscriptionCancelledError):
                stand_down_if_requested("segment-loop")

    def test_once_observed_the_cancellation_latches(self, fake_redis):
        """A flag that expires between the observation and the raise must not resurrect the
        job mid-unwind."""
        request_cancel("run-latch", "uuid-latch")
        with cancellation_scope("run-latch", "uuid-latch"):
            with pytest.raises(TranscriptionCancelledError):
                stand_down_if_requested("first")
            fake_redis.store.clear()
            with pytest.raises(TranscriptionCancelledError):
                stand_down_if_requested("second")

    def test_an_unreachable_redis_reads_as_not_cancelled(self):
        """Fails OPEN, deliberately and in the same direction as
        ``reindex_cancel.cancel_target``: a Redis blip must not stand down every transcription
        on the host. The write side reports the failure instead — see
        ``TestCancelActiveTask.test_when_the_flag_cannot_be_armed_the_file_is_finalized``."""
        checkpoints_survived = 0
        with patch(_GET_REDIS, return_value=_ExplodingRedis()):
            assert tcancel.cancel_requested("run-blip") is False, (
                "an unreachable Redis must read as 'not cancelled', not propagate"
            )
            with cancellation_scope("run-blip", "uuid-blip"):
                for _ in range(3):
                    stand_down_if_requested("checkpoint")
                    checkpoints_survived += 1
        assert checkpoints_survived == 3, "a Redis blip stood the transcription down"

    def test_the_shutdown_trigger_still_works_through_the_same_checkpoint(self, fake_redis):
        """#823 rewired every one of #809's call sites; this is the non-regression that the
        shutdown half still fires from them."""
        ws.mark_shutting_down()
        with cancellation_scope("run-healthy", "uuid-healthy"):
            with pytest.raises(ws.TranscriptionAbortedError):
                stand_down_if_requested("checkpoint")

    def test_a_cancel_beats_a_simultaneous_shutdown(self, fake_redis):
        """Deliberate ordering. Honouring the shutdown would ``Reject(requeue=True)`` and hand
        the cancelled job to the next worker, which stands it down again — churn, plus a window
        in which the file reads as processing after the user stopped it."""
        request_cancel("run-both", "uuid-both")
        ws.mark_shutting_down()
        with cancellation_scope("run-both", "uuid-both"):
            with pytest.raises(TranscriptionCancelledError):
                stand_down_if_requested("checkpoint")


# ──────────────────────────────────────────────────────────────────────────────────────────
# 4. The engine stages actually stand down — i.e. the GPU work stops
# ──────────────────────────────────────────────────────────────────────────────────────────


class TestTheEngineStagesStandDown:
    """Mirrors ``test_cooperative_abort.py::TestEngineStageCheckpoints`` for the cancel
    trigger. The assertion that matters is ``get_transcriber``/``get_diarizer`` never being
    called: the whole point of #823 is that no model is warmed and no VRAM is taken for a file
    the user has already stopped."""

    @pytest.fixture
    def engine_collaborators(self):
        manager = MagicMock()
        with (
            patch(
                "app.transcription.model_manager.ModelManager.get_instance", return_value=manager
            ),
            patch("app.utils.hardware_detection.detect_hardware", return_value=MagicMock()),
            patch("app.utils.vram_profiler.VRAMProfiler", return_value=MagicMock()),
            patch("app.transcription.engine.audio_loader.load_from_shared_volume") as loader,
        ):
            yield {"manager": manager, "load_from_shared_volume": loader}

    @staticmethod
    def _config():
        config = MagicMock()
        tc = config.transcription_config
        tc.enable_diarization = True
        tc.concurrent_requests = 1
        tc.device = "cpu"
        tc.diarizer_backend = "native"
        return config

    @pytest.mark.parametrize(
        ("stage_name", "checkpoint"),
        [
            ("_GpuStage", "_GpuStage.run entry"),
            ("_GpuRawStage", "_GpuRawStage.run entry"),
            ("_TranscribeOnlyStage", "_TranscribeOnlyStage.run entry"),
            ("_DiarizerOnlyStage", "_DiarizerOnlyStage.run entry"),
        ],
    )
    def test_the_stage_refuses_before_warming_a_model(
        self, fake_redis, engine_collaborators, stage_name, checkpoint
    ):
        import app.transcription.engine.stages as stages_mod

        request_cancel("run-gpu", "uuid-gpu")
        stage = getattr(stages_mod, stage_name)()

        with cancellation_scope("run-gpu", "uuid-gpu"):
            with pytest.raises(TranscriptionCancelledError) as exc:
                stage.run(MagicMock(), self._config())

        assert checkpoint in str(exc.value)
        engine_collaborators["manager"].get_transcriber.assert_not_called()
        engine_collaborators["manager"].get_diarizer.assert_not_called()
        engine_collaborators["load_from_shared_volume"].assert_not_called()

    def test_an_uncancelled_run_gets_past_the_entry_checkpoint(
        self, fake_redis, engine_collaborators
    ):
        """The control. Without it every test above would also pass against a stage that stood
        down unconditionally — which would break every transcription on the host."""
        from app.transcription.engine.stages import _DiarizerOnlyStage

        engine_collaborators["load_from_shared_volume"].return_value = None

        with cancellation_scope("run-healthy", "uuid-healthy"):
            # RuntimeError is the stage's own "WAV unreadable" guard, reachable only AFTER the
            # entry checkpoint — so reaching it proves the checkpoint let this run through.
            with pytest.raises(RuntimeError) as exc:
                _DiarizerOnlyStage().run(MagicMock(), self._config())

        assert not isinstance(exc.value, TranscriptionCancelledError)


# ──────────────────────────────────────────────────────────────────────────────────────────
# 5. Structural: every task that can reach a checkpoint handles the cancel
# ──────────────────────────────────────────────────────────────────────────────────────────


_TASK_MODULES = (
    "app/tasks/transcription/core.py",
    "app/tasks/transcription/cpu_task.py",
    "app/tasks/transcription/diarize_task.py",
)


def _handler_names(source: str) -> set[str]:
    """Exception names caught by any ``except`` clause in *source*."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        candidates = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        for item in candidates:
            if isinstance(item, ast.Name):
                names.add(item.id)
            elif isinstance(item, ast.Attribute):
                names.add(item.attr)
    return names


class TestEveryAbortHandlerHasACancelSibling:
    """The sibling of ``test_cooperative_abort.py::TestEveryAcksLateTaskHandlesTheAbort``.

    A task that requeues a shutdown abort but lets a cancel fall into ``except Exception``
    marks the file ERROR and notifies the user that their transcription failed — for a stop
    they asked for. Every module that handles one must handle the other.
    """

    def _module_sources(self):
        root = Path(__file__).resolve().parents[2]
        return {rel: (root / rel).read_text(encoding="utf-8") for rel in _TASK_MODULES}

    def test_the_sources_actually_resolve(self):
        """Guard the guard: an unresolvable path would make every assertion below iterate
        nothing and pass while checking nothing."""
        sources = self._module_sources()
        assert len(sources) == len(_TASK_MODULES)
        for rel, src in sources.items():
            assert "TranscriptionAbortedError" in src, f"{rel} is not the module we think it is"

    def test_every_module_handling_the_abort_also_handles_the_cancel(self):
        offenders = []
        for rel, src in self._module_sources().items():
            caught = _handler_names(src)
            if (
                "TranscriptionAbortedError" in caught
                and "TranscriptionCancelledError" not in caught
            ):
                offenders.append(rel)
        assert not offenders, (
            f"these tasks requeue a shutdown abort but have no cancel branch: {offenders}. "
            "A TranscriptionCancelledError reaching `except Exception` marks the file ERROR "
            "and tells the user their file failed. Add an `except TranscriptionCancelledError` "
            "that returns cancellation.finish_cancelled(...), BEFORE the generic handler."
        )

    def test_no_task_routes_a_cancel_through_requeue_after_abort(self):
        """The specific wrong fix: reusing #809's outcome. It would ack-and-redeliver the
        cancelled job, so the file would go back to processing right after the UI said it
        stopped."""
        requeueing: list[str] = []
        failure_pathed: list[str] = []
        handlers_inspected = 0

        for rel, src in self._module_sources().items():
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if "TranscriptionCancelledError" not in _handler_names(
                    f"try:\n    pass\n{ast.unparse(node)}"
                ):
                    continue
                handlers_inspected += 1
                body = ast.unparse(node)
                if "requeue_after_abort" in body:
                    requeueing.append(rel)
                if "_handle_transcription_failure" in body:
                    failure_pathed.append(rel)

        # Outside the loop: an empty module set, or a rename that stopped the handler filter
        # matching, would otherwise leave every assertion above unexecuted and pass.
        assert handlers_inspected == len(_TASK_MODULES), (
            f"inspected {handlers_inspected} cancel handlers across {len(_TASK_MODULES)} task "
            "modules — expected exactly one each; this gate is checking nothing"
        )
        assert not requeueing, (
            f"{requeueing}: the cancel handler calls requeue_after_abort, which requeues the "
            "job the user just cancelled"
        )
        assert not failure_pathed, (
            f"{failure_pathed}: the cancel handler travels the failure path — a cancel is not "
            "a failure and must not mark the file ERROR"
        )


# ──────────────────────────────────────────────────────────────────────────────────────────
# 6. The two-phase status machine — DB-backed
# ──────────────────────────────────────────────────────────────────────────────────────────


@contextmanager
def _yield_session(db):
    """Stand-in for ``session_scope()`` handing out the test's savepoint session.

    ``finish_cancelled`` and ``reconcile_cancellation`` open their OWN sessions, which under
    the savepoint harness cannot see the uncommitted fixture rows.
    """
    yield db


def _make_processing_file(db_session, user, status=FileStatus.PROCESSING):
    media_file = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename="cancel_target.mp3",
        storage_path=f"user_{user.id}/cancel_target.mp3",
        file_size=1024,
        content_type="audio/mpeg",
        status=status,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    task_id = str(uuid_module.uuid4())
    db_session.add(
        Task(
            id=task_id,
            user_id=user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="in_progress",
            created_at=datetime.now(UTC),
        )
    )
    media_file.active_task_id = task_id
    media_file.task_started_at = datetime.now(UTC)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file, task_id


@pytest.fixture
def cancellation_seams(db_session):
    """Point the cancellation module's own session and notification seams at the test."""
    with (
        patch(f"{_CANCELLATION}.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.transcription.notifications.send_notification_via_redis") as notify,
    ):
        yield notify


class TestCancelActiveTask:
    """Phase one: the API arms the stop and says so honestly."""

    def test_the_file_moves_to_cancelling_not_cancelled(self, db_session, normal_user, fake_redis):
        """The status-honesty change. ``CANCELLED`` used to be written here, before anything
        had stopped — it is now written only by the worker that actually stood down.
        ``FileStatus.CANCELLING`` already existed in the enum, the API filter list,
        ``formatting_service`` and the frontend's status union; nothing had ever written it."""
        from app.utils.task_utils import cancel_active_task

        media_file, task_id = _make_processing_file(db_session, normal_user)

        assert cancel_active_task(db_session, media_file.id) is True

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLING
        assert media_file.cancellation_requested is True

    def test_it_arms_the_flag_the_running_task_polls(self, db_session, normal_user, fake_redis):
        from app.utils.task_utils import cancel_active_task

        media_file, task_id = _make_processing_file(db_session, normal_user)
        cancel_active_task(db_session, media_file.id)

        assert tcancel.CANCEL_KEY.format(task_id=task_id) in fake_redis.store, (
            "no cooperative flag was written, so no checkpoint will ever fire and the GPU "
            "keeps running — issue #823's original symptom"
        )

    def test_it_retains_the_active_task_id(self, db_session, normal_user, fake_redis):
        """``active_task_id`` used to be nulled here. It is the only handle the worker's
        confirmation and the reconciliation task have on this run; clearing it makes the two
        phases unable to find each other."""
        from app.utils.task_utils import cancel_active_task

        media_file, task_id = _make_processing_file(db_session, normal_user)
        cancel_active_task(db_session, media_file.id)

        db_session.refresh(media_file)
        assert media_file.active_task_id == task_id

    def test_when_the_flag_cannot_be_armed_the_file_is_finalized(self, db_session, normal_user):
        """Redis down: no checkpoint can ever fire, so leaving the file ``CANCELLING`` would
        wedge it waiting for a stop nobody was told about. Fall back to the pre-#823
        behaviour — and say so in the log rather than reporting a clean cancellation."""
        from app.utils.task_utils import cancel_active_task

        media_file, _task_id = _make_processing_file(db_session, normal_user)

        with patch(_GET_REDIS, return_value=_ExplodingRedis()):
            assert cancel_active_task(db_session, media_file.id) is True

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLED
        assert media_file.active_task_id is None

    def test_it_no_longer_calls_revoke(self, db_session, normal_user, fake_redis, monkeypatch):
        """``revoke(active_task_id, terminate=True)`` was dead code on EVERY pool type, not
        just ``--pool=threads``: ``dispatch.py`` calls ``pipeline.apply_async()`` with no
        ``task_id=``, so celery mints its own ids while ``active_task_id`` holds the separate
        application uuid4. The id being revoked had never belonged to a celery message."""
        from app.core.celery import celery_app
        from app.utils.task_utils import cancel_active_task

        calls = []
        monkeypatch.setattr(celery_app.control, "revoke", lambda *a, **k: calls.append((a, k)))

        media_file, _ = _make_processing_file(db_session, normal_user)
        cancel_active_task(db_session, media_file.id)

        assert calls == [], (
            "cancel_active_task still calls revoke(); it targets an id no worker has ever "
            "seen, so it can only mislead a reader into thinking termination was attempted"
        )

    def test_the_unarmed_fallback_says_the_work_may_still_be_running(
        self, db_session, normal_user, caplog
    ):
        """Log honesty, inherited from the characterization suite this file replaces.

        The pre-#823 message was "Successfully cancelled task for file {id}" — read by an
        operator as "the GPU work stopped", which was false. On the cooperative path the status
        itself now carries the distinction (``CANCELLING`` vs ``CANCELLED``), but the
        Redis-unreachable fallback still writes ``CANCELLED`` without any confirmation, so THAT
        path must still say so out loud.
        """
        import logging

        from app.utils.task_utils import cancel_active_task

        media_file, _ = _make_processing_file(db_session, normal_user)

        with caplog.at_level(logging.ERROR, logger="app.utils.task_utils"):
            with patch(_GET_REDIS, return_value=_ExplodingRedis()):
                cancel_active_task(db_session, media_file.id)

        messages = " ".join(r.message for r in caplog.records).lower()
        assert "may still be running" in messages, (
            f"the unconfirmed fallback does not warn that the work was never stopped -- "
            f"messages were: {messages!r}"
        )

    def test_a_file_with_no_active_task_is_not_cancellable(
        self, db_session, normal_user, fake_redis
    ):
        from app.utils.task_utils import cancel_active_task

        media_file, _ = _make_processing_file(db_session, normal_user)
        media_file.active_task_id = None
        db_session.commit()

        assert cancel_active_task(db_session, media_file.id) is False
        assert fake_redis.store == {}


class TestFinishCancelled:
    """Phase two: the worker confirms it actually stopped."""

    def test_it_marks_the_file_cancelled_and_returns_rather_than_requeueing(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLING)
        ctx = MagicMock(file_id=media_file.id, user_id=normal_user.id)

        result = finish_cancelled(
            ctx,
            task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        # Returning is what acks the message under acks_late — i.e. what stops the job coming
        # back. A Reject (issue #809's outcome) would have raised out of this call.
        assert result["status"] == "cancelled"
        assert result["task_id"] == task_id

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLED
        assert media_file.active_task_id is None
        assert media_file.cancellation_requested is False

    def test_it_records_when_the_stop_was_confirmed(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        """``Task.completed_at`` is the verifiable "it actually stopped at" signal — written
        only on this path, never by the API's request."""
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLING)
        task = db_session.query(Task).filter(Task.id == task_id).one()
        assert task.completed_at is None, "precondition: the request alone confirms nothing"

        finish_cancelled(
            MagicMock(file_id=media_file.id, user_id=normal_user.id),
            task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        db_session.refresh(task)
        assert task.completed_at is not None

    def test_it_does_not_mark_the_file_errored(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        """The contrast with ``_handle_transcription_failure``: a user's own cancel reported
        back to them as a failed transcription is the wrong answer."""
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLING)

        finish_cancelled(
            MagicMock(file_id=media_file.id, user_id=normal_user.id),
            task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        db_session.refresh(media_file)
        # The exact expected value, not `!= ERROR`: a negated status assertion passes against
        # any other wrong state too (the auditor's `negated-status` rule).
        assert media_file.status == FileStatus.CANCELLED
        assert not media_file.last_error_message, (
            "a cancel wrote last_error_message, which the UI renders as a failure reason"
        )
        assert not media_file.error_category

    def test_it_clears_the_flag_so_a_later_run_is_unaffected(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLING)
        request_cancel(task_id, str(media_file.uuid))

        finish_cancelled(
            MagicMock(file_id=media_file.id, user_id=normal_user.id),
            task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        assert tcancel.CANCEL_KEY.format(task_id=task_id) not in fake_redis.store

    def test_it_leaves_a_file_a_reprocess_has_reclaimed_alone(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        """``POST /files/{uuid}/reprocess`` cancels the active task and immediately dispatches
        a fresh pipeline. The old run's checkpoint can fire minutes later, mid-decode; without
        the ownership check it would mark the file CANCELLED and null out the NEW run's
        ``active_task_id``, killing a reprocess the user just asked for."""
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, old_task_id = _make_processing_file(db_session, normal_user)
        new_task_id = str(uuid_module.uuid4())
        media_file.active_task_id = new_task_id
        media_file.status = FileStatus.PROCESSING
        db_session.commit()

        finish_cancelled(
            MagicMock(file_id=media_file.id, user_id=normal_user.id),
            old_task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.PROCESSING
        assert media_file.active_task_id == new_task_id

    def test_it_does_not_overwrite_a_file_that_already_completed(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        """A run that finished before the cancel landed legitimately has a transcript.
        Overwriting COMPLETED with CANCELLED would discard it."""
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.COMPLETED)

        finish_cancelled(
            MagicMock(file_id=media_file.id, user_id=normal_user.id),
            task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.COMPLETED


class TestReconcileCancellation:
    """The bounded backstop that stops ``CANCELLING`` being a state a file can wedge in."""

    def test_it_is_a_noop_once_the_worker_has_confirmed(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        from app.tasks.transcription.cancellation import reconcile_cancellation

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLED)

        outcome = reconcile_cancellation(media_file.id, task_id)

        assert outcome == {"outcome": "confirmed"}
        assert cancellation_seams.call_count == 0, "a confirmed stop must not re-notify"

    def test_it_resolves_a_cancellation_no_worker_ever_answered(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        """The case it exists for: the worker died, or (on --lite) the run was published into
        a queue nothing drains, so no checkpoint can ever fire."""
        from app.tasks.transcription.cancellation import reconcile_cancellation

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLING)

        outcome = reconcile_cancellation(media_file.id, task_id)

        assert outcome == {"outcome": "forced"}
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLED
        assert media_file.active_task_id is None

    def test_it_leaves_the_flag_armed(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        """⚠️ Clearing the flag here would let a stage that IS still running sail past its next
        checkpoint, finish, save segments and mark the file COMPLETED — resurrecting the job
        this task has just reported as cancelled."""
        from app.tasks.transcription.cancellation import reconcile_cancellation

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLING)
        request_cancel(task_id, str(media_file.uuid))

        reconcile_cancellation(media_file.id, task_id)

        assert tcancel.CANCEL_KEY.format(task_id=task_id) in fake_redis.store

    def test_it_does_not_touch_a_file_a_reprocess_reclaimed(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        from app.tasks.transcription.cancellation import reconcile_cancellation

        media_file, old_task_id = _make_processing_file(
            db_session, normal_user, FileStatus.CANCELLING
        )
        media_file.active_task_id = str(uuid_module.uuid4())
        db_session.commit()

        assert reconcile_cancellation(media_file.id, old_task_id) == {"outcome": "superseded"}
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLING

    def test_a_file_that_moved_on_by_itself_is_left_alone(
        self, db_session, normal_user, fake_redis, cancellation_seams
    ):
        from app.tasks.transcription.cancellation import reconcile_cancellation

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.PROCESSING)

        assert reconcile_cancellation(media_file.id, task_id) == {"outcome": "not_cancelling"}
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.PROCESSING


class TestThePostprocessLinkIsANoop:
    """``finish_cancelled`` RETURNS, which acks the message — and therefore also dispatches the
    chain's successor. That successor must not mark the cancelled file COMPLETED."""

    def test_finalize_transcription_stops_on_a_cancelled_payload(self, db_session, normal_user):
        from app.tasks.transcription import postprocess

        media_file, task_id = _make_processing_file(db_session, normal_user, FileStatus.CANCELLED)
        payload = {
            "status": "cancelled",
            "file_uuid": str(media_file.uuid),
            "file_id": media_file.id,
            "task_id": task_id,
        }

        with patch.object(postprocess, "_cleanup_temp") as cleanup:
            result = postprocess.finalize_transcription(payload)

        assert result == payload
        cleanup.assert_called_once_with(str(media_file.uuid))
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLED
