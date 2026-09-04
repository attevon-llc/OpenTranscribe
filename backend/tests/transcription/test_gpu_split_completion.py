"""Regression tests for the gpu-split (--with-gpu-split) P0 fix.

Issue #703-followup. Before this fix, EVERY --with-gpu-split job failed visibly:
``transcribe_gpu_task`` returned a ``status: split_forwarded`` dict into the outer pipeline
chain's UNCONDITIONAL third link (``finalize_transcription``), which unpacked
``gpu_result["user_id"]`` with no matching guard and raised ``KeyError``. Separately,
``diarize_gpu_task`` — which actually finishes the split run — was dispatched standalone
(``apply_async``, not chained), so even without the KeyError its real result never reached
``finalize_transcription`` at all.

Two things are proven here, deliberately on REAL rows / real Celery signature objects rather
than mocked-away effects (the repo's own ``test_dispatch.py`` docstring records that patching
the very effects under test kept 13 of 15 tests green while the handler under test was broken):

1. ``finalize_transcription`` no longer raises on a ``split_forwarded`` payload (the literal
   reproduction from the brief).
2. The split path reaches a COMPLETED ``MediaFile``/``Task`` pair exactly once: the outer
   chain's finalize call is a true no-op (no premature ERROR), and the SEPARATE
   diarize_gpu_task -> finalize_transcription chain this fix dispatches is what actually
   completes the file.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task

_CORE = "app.tasks.transcription.core"
_POSTPROCESS = "app.tasks.transcription.postprocess"


class _FakeRedisNX:
    """Minimal SETNX-shaped stand-in — enough to exercise the once-only claim."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):  # noqa: ARG002 - ex unused, matches redis-py sig
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True


class TestClaimGpuSplitDiarizeDispatch:
    """``_claim_gpu_split_diarize_dispatch`` — the once-only guard against a redelivered
    ``transcribe_gpu_task`` (acks_late=True) dispatching a second diarize chain."""

    def test_first_claim_succeeds(self, monkeypatch):
        from app.tasks.transcription.core import _claim_gpu_split_diarize_dispatch

        monkeypatch.setattr("app.core.redis.get_redis", lambda: _FakeRedisNX())

        assert _claim_gpu_split_diarize_dispatch("task-first") is True

    def test_redelivery_of_the_same_task_id_is_refused(self, monkeypatch):
        """THE regression this guard exists for: a redelivered transcribe_gpu_task carries
        the SAME application task_id, so a second claim attempt for it must fail."""
        from app.tasks.transcription.core import _claim_gpu_split_diarize_dispatch

        fake_redis = _FakeRedisNX()
        monkeypatch.setattr("app.core.redis.get_redis", lambda: fake_redis)

        assert _claim_gpu_split_diarize_dispatch("task-dup") is True
        assert _claim_gpu_split_diarize_dispatch("task-dup") is False

    def test_different_task_ids_both_claim(self, monkeypatch):
        from app.tasks.transcription.core import _claim_gpu_split_diarize_dispatch

        fake_redis = _FakeRedisNX()
        monkeypatch.setattr("app.core.redis.get_redis", lambda: fake_redis)

        assert _claim_gpu_split_diarize_dispatch("task-a") is True
        assert _claim_gpu_split_diarize_dispatch("task-b") is True

    def test_redis_error_fails_open(self, monkeypatch):
        """A Redis outage must not silently drop diarization for every gpu-split file —
        failing open (dispatch anyway) is the documented, deliberate tradeoff."""
        from app.tasks.transcription.core import _claim_gpu_split_diarize_dispatch

        def _boom():
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr("app.core.redis.get_redis", _boom)

        assert _claim_gpu_split_diarize_dispatch("task-redis-down") is True


class TestGpuDiarizeConsumerPresent:
    """``_gpu_diarize_consumer_present`` / ``_resolve_gpu_diarize_queue`` — issue #705's
    consumer-liveness guard for the diarize leg, mirroring
    ``dispatch.py::_gpu_transcribe_consumer_present`` for the transcribe leg."""

    def _reset_cache(self):
        """The TTL cache is module-level; force a fresh probe for each test."""
        from app.tasks.transcription import core

        core._gpu_diarize_consumer_cache["checked_at"] = 0.0
        core._gpu_diarize_consumer_cache["present"] = False

    def test_consumer_present_routes_to_gpu_diarize(self, monkeypatch):
        self._reset_cache()
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.core import _resolve_gpu_diarize_queue

        class _FakeInspect:
            def active_queues(self):
                return {"worker1@host": [{"name": CeleryQueues.GPU_DIARIZE}]}

        monkeypatch.setattr(
            "app.tasks.transcription.core.celery_app.control.inspect",
            lambda timeout=2.0: _FakeInspect(),
        )

        assert _resolve_gpu_diarize_queue() == CeleryQueues.GPU_DIARIZE

    def test_consumer_absent_fails_closed_to_gpu_queue(self, monkeypatch):
        """No live consumer on gpu-diarize must NOT dispatch into a dead queue — the
        exact issue #705 hazard. Falls back to the always-staffed 'gpu' queue, the same
        fail-closed direction the transcribe leg uses (diarize_gpu_task is registered
        there too, since the main 'gpu' worker imports the full app)."""
        self._reset_cache()
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.core import _resolve_gpu_diarize_queue

        class _FakeInspect:
            def active_queues(self):
                return {"worker1@host": [{"name": "some-other-queue"}]}

        monkeypatch.setattr(
            "app.tasks.transcription.core.celery_app.control.inspect",
            lambda timeout=2.0: _FakeInspect(),
        )

        assert _resolve_gpu_diarize_queue() == CeleryQueues.GPU

    def test_broker_error_fails_closed_to_gpu_queue(self, monkeypatch):
        """A broker error probing consumer liveness must fail CLOSED (route to the
        always-staffed 'gpu' queue) — the opposite tradeoff from the Redis dedup claim
        above, and deliberately so: an error here risks silently dropping every
        gpu-split diarize job into a dead queue, whereas failing closed just costs the
        dedicated worker's isolation/affinity benefits for this one dispatch."""
        self._reset_cache()
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.core import _resolve_gpu_diarize_queue

        def _boom(timeout=2.0):
            raise ConnectionError("broker unreachable")

        monkeypatch.setattr(
            "app.tasks.transcription.core.celery_app.control.inspect",
            _boom,
        )

        assert _resolve_gpu_diarize_queue() == CeleryQueues.GPU

    def test_cache_short_circuits_a_second_probe_within_ttl(self, monkeypatch):
        """The TTL cache must not re-probe on every call — only once per window."""
        self._reset_cache()
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.core import _gpu_diarize_consumer_present

        call_count = {"n": 0}

        class _FakeInspect:
            def active_queues(self):
                call_count["n"] += 1
                return {"worker1@host": [{"name": CeleryQueues.GPU_DIARIZE}]}

        monkeypatch.setattr(
            "app.tasks.transcription.core.celery_app.control.inspect",
            lambda timeout=2.0: _FakeInspect(),
        )

        assert _gpu_diarize_consumer_present() is True
        assert _gpu_diarize_consumer_present() is True
        assert call_count["n"] == 1


class TestDispatchGpuSplitDiarizeChainQueueSelection:
    """``_dispatch_gpu_split_diarize_chain`` must route the diarize leg through the
    consumer-liveness guard rather than hardcoding ``CeleryQueues.GPU_DIARIZE`` —
    otherwise the whole point of #705's guard is dead code that nothing calls."""

    @patch(f"{_CORE}._claim_gpu_split_diarize_dispatch", return_value=True)
    def test_dispatch_uses_the_resolved_queue_not_a_hardcoded_one(self, mock_claim, monkeypatch):
        """Proves dispatch actually calls the guard rather than hardcoding
        ``CeleryQueues.GPU_DIARIZE`` — otherwise the guard is dead code nothing calls.
        Patches ``chain`` itself (not just ``apply_async``) so the first signature's
        ``.options['queue']`` — set via ``.set()`` before ``chain()`` is even called —
        can be inspected directly, rather than fighting unbound-method patching on
        ``celery.canvas._chain.apply_async``.
        """
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.core import _dispatch_gpu_split_diarize_chain

        monkeypatch.setattr(f"{_CORE}._resolve_gpu_diarize_queue", lambda: CeleryQueues.GPU)

        captured_signatures = {}

        def _fake_chain(*sigs):
            captured_signatures["sigs"] = sigs
            fake = type("FakeChain", (), {"apply_async": lambda self, **kw: None})()
            return fake

        monkeypatch.setattr(f"{_CORE}.chain", _fake_chain)

        result = _dispatch_gpu_split_diarize_chain(
            task_id="task-queue-check",
            file_uuid="file-uuid-1",
            transcript_data={"raw_segments": []},
            preprocess_context={"task_id": "task-queue-check", "file_id": 1, "user_id": 7},
        )

        assert result is True
        diarize_signature = captured_signatures["sigs"][0]
        assert diarize_signature.options["queue"] == CeleryQueues.GPU


class TestDispatchGpuSplitDiarizeChain:
    """``_dispatch_gpu_split_diarize_chain`` builds and dispatches the real completion
    chain: diarize_gpu_task -> finalize_transcription, with its own link_error."""

    @patch(f"{_CORE}._claim_gpu_split_diarize_dispatch", return_value=True)
    def test_dispatches_a_two_link_chain_with_link_error(self, mock_claim):
        from app.tasks.transcription.core import _dispatch_gpu_split_diarize_chain

        with patch("celery.canvas._chain.apply_async") as mock_apply_async:
            result = _dispatch_gpu_split_diarize_chain(
                task_id="task-1",
                file_uuid="file-uuid-1",
                transcript_data={"raw_segments": []},
                preprocess_context={"task_id": "task-1", "file_id": 1, "user_id": 7},
            )

        assert result is True
        mock_apply_async.assert_called_once()
        # link_error must be wired so a genuine failure anywhere in this second leg
        # still marks the file ERROR — not silently swallowed.
        assert mock_apply_async.call_args.kwargs["link_error"]

    @patch(f"{_CORE}._claim_gpu_split_diarize_dispatch", return_value=False)
    def test_skips_dispatch_when_claim_already_taken(self, mock_claim):
        """A redelivered transcribe_gpu_task must NOT build/dispatch a second chain —
        this is the double-diarize-dispatch hazard the brief calls out explicitly."""
        from app.tasks.transcription.core import _dispatch_gpu_split_diarize_chain

        with patch("celery.canvas._chain.apply_async") as mock_apply_async:
            result = _dispatch_gpu_split_diarize_chain(
                task_id="task-redelivered",
                file_uuid="file-uuid-1",
                transcript_data={"raw_segments": []},
                preprocess_context={"task_id": "task-redelivered", "file_id": 1, "user_id": 7},
            )

        assert result is False
        mock_apply_async.assert_not_called()


class TestFinalizeTranscriptionSplitForwarded:
    """``finalize_transcription`` must tolerate the exact ``split_forwarded`` payload
    ``transcribe_gpu_task`` returns — the literal reproduction from the bug report."""

    def test_does_not_raise_and_returns_the_payload(self):
        from app.tasks.transcription.postprocess import finalize_transcription

        gpu_result = {
            "status": "split_forwarded",
            "file_uuid": "file-uuid-1",
            "file_id": 1,
            "task_id": "task-1",
            "split_stage": "transcribe_only",
        }

        # Before the fix this line raised KeyError('user_id') from inside the try body,
        # after unpacking gpu_result["file_uuid"]/["file_id"]/["user_id"]/["task_id"].
        result = finalize_transcription.__wrapped__(gpu_result)

        assert result == gpu_result

    def test_does_not_touch_task_or_media_file_state(self, db_session, normal_user):
        """A split_forwarded no-op must not mark anything ERROR or COMPLETED — the real
        completion belongs to the diarize_gpu_task -> finalize_transcription chain."""
        from app.tasks.transcription.postprocess import finalize_transcription

        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="split_test.mp3",
            storage_path="split/split_test.mp3",
            file_size=1024,
            content_type="audio/mpeg",
            status=FileStatus.PROCESSING,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        task = Task(
            id=str(uuid_pkg.uuid4()),
            user_id=normal_user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="in_progress",
            progress=0.25,
        )
        db_session.add(task)
        db_session.commit()

        finalize_transcription.__wrapped__(
            {
                "status": "split_forwarded",
                "file_uuid": str(media_file.uuid),
                "file_id": media_file.id,
                "task_id": task.id,
                "split_stage": "transcribe_only",
            }
        )

        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.PROCESSING
        assert task.status == "in_progress"


@contextmanager
def _session_scope_over(db_session):
    yield db_session


class TestSplitPathReachesCompletedExactlyOnce:
    """End-to-end (within the process): the outer chain's finalize call is a genuine no-op,
    and the SEPARATE chain this fix dispatches — diarize_gpu_task's real result fed into a
    SECOND finalize_transcription call — is what completes the file. Never both, never
    neither, never ERROR-then-COMPLETED.
    """

    @patch(f"{_POSTPROCESS}.enrich_and_dispatch")
    @patch(f"{_POSTPROCESS}.send_ws_event")
    @patch(f"{_POSTPROCESS}.send_completion_notification")
    @patch(f"{_POSTPROCESS}.send_progress_notification")
    def test_outer_noop_then_real_finalize_completes_exactly_once(
        self,
        mock_progress,
        mock_completion,
        mock_ws,
        mock_enrich,
        db_session,
        normal_user,
    ):
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="split_complete.mp3",
            storage_path="split/split_complete.mp3",
            file_size=4096,
            content_type="audio/mpeg",
            status=FileStatus.PROCESSING,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        task = Task(
            id=str(uuid_pkg.uuid4()),
            user_id=normal_user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="in_progress",
            progress=0.5,
        )
        db_session.add(task)
        db_session.commit()

        from app.tasks.transcription.postprocess import finalize_transcription

        # Step 1: the OUTER chain's finalize stage runs first (transcribe_gpu_task's
        # return value), against the split_forwarded dict. Must be a no-op.
        finalize_transcription.__wrapped__(
            {
                "status": "split_forwarded",
                "file_uuid": str(media_file.uuid),
                "file_id": media_file.id,
                "task_id": task.id,
                "split_stage": "transcribe_only",
            }
        )
        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.PROCESSING, (
            "the outer chain's finalize call must not touch file state"
        )
        assert task.status == "in_progress"

        # Step 2: the SEPARATE chain this fix dispatches — diarize_gpu_task's real
        # result — reaches finalize_transcription and completes the file.
        real_gpu_result = {
            "status": "success",
            "file_uuid": str(media_file.uuid),
            "file_id": media_file.id,
            "user_id": normal_user.id,
            "task_id": task.id,
            "speaker_mapping": {},
            "native_embeddings": None,
            "use_native_embeddings": False,
            "asr_provider": "local",
            "downstream_tasks": None,
            "diarization_disabled": False,
            "diarization_source": "provider",
            "segment_count": 12,
        }

        with (
            patch(f"{_POSTPROCESS}.session_scope", new=lambda: _session_scope_over(db_session)),
            patch(f"{_POSTPROCESS}._cleanup_temp"),
        ):
            outcome = finalize_transcription.__wrapped__(real_gpu_result)

        assert outcome["status"] == "success"

        db_session.refresh(media_file)
        db_session.refresh(task)
        # Reached COMPLETED via exactly one real completion call — never having passed
        # through ERROR on the way (the KeyError-driven regression this fix closes).
        assert task.status == "completed"
        assert task.completed_at is not None
        mock_completion.assert_called_once_with(normal_user.id, media_file.id)
