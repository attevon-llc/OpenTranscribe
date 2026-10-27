"""Tests for transcription postprocess: the background enrichment fan-out."""

from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from tests.unit._plan_contexts import plan_context

_POSTPROCESS = "app.tasks.transcription.postprocess"


@pytest.fixture
def fanout(monkeypatch):
    """enrich_and_dispatch with every dispatcher recorded and the plan captured."""
    from app.services import post_processing_status as pps
    from app.tasks.transcription import postprocess

    calls: dict[str, tuple] = {}
    plans: list[dict] = []

    def record(name, result):
        def _fn(*a, **k):
            calls[name] = (a, k)
            return result

        return _fn

    monkeypatch.setattr(
        postprocess, "_index_transcript", record("index", PostProcessingState.QUEUED)
    )
    monkeypatch.setattr(
        postprocess, "_dispatch_redaction", record("redaction", PostProcessingState.SKIPPED)
    )
    monkeypatch.setattr(
        postprocess,
        "_dispatch_speaker_attributes",
        record("attrs", {PostProcessingTask.SPEAKER_ATTRIBUTES: PostProcessingState.QUEUED}),
    )
    monkeypatch.setattr(
        postprocess,
        "_dispatch_speaker_clustering",
        record("cluster", PostProcessingState.QUEUED),
    )
    monkeypatch.setattr(
        "app.tasks.transcription.core.trigger_automatic_summarization",
        record("summarize", {PostProcessingTask.ANALYTICS: PostProcessingState.QUEUED}),
    )
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts", record("facts", True)
    )
    monkeypatch.setattr(
        postprocess.post_processing_status,
        "resolve_file",
        lambda file_id: pps.FileRef(id=file_id, uuid="u", owner_id=1),
    )
    monkeypatch.setattr(
        postprocess.post_processing_status,
        "report_plan",
        lambda ref, decisions, run_at: plans.append(dict(decisions)),
    )
    return postprocess, calls, plans


class TestEnrichAndDispatch:
    """Tests for enrich_and_dispatch() Celery task."""

    def test_calls_all_downstream_tasks_with_the_file_params(self, fanout):
        postprocess, calls, plans = fanout

        postprocess.enrich_and_dispatch(file_id=42, file_uuid="abc-def-123", user_id=7)

        assert calls["index"] == ((42, "abc-def-123", 7), {"pipeline_task_id": None})
        assert calls["summarize"] == ((42, "abc-def-123"), {"tasks_to_run": None})
        assert calls["attrs"] == (("abc-def-123", 7, None), {})
        assert calls["cluster"] == (("abc-def-123", 7, None), {"file_id": 42})
        assert calls["facts"] == ((42,), {"pipeline_task_id": None})
        assert len(plans) == 1

    def test_passes_downstream_tasks_through(self, fanout):
        postprocess, calls, _ = fanout
        downstream = ["summarization"]

        postprocess.enrich_and_dispatch(
            file_id=1, file_uuid="uuid-1", user_id=1, downstream_tasks=downstream
        )

        assert calls["summarize"] == ((1, "uuid-1"), {"tasks_to_run": downstream})
        assert calls["attrs"] == (("uuid-1", 1, downstream), {})
        assert calls["cluster"] == (("uuid-1", 1, downstream), {"file_id": 1})

    def test_propagates_pipeline_task_id_to_indexing(self, fanout):
        """pipeline_task_id is forwarded into _index_transcript for benchmark markers."""
        postprocess, calls, _ = fanout

        postprocess.enrich_and_dispatch(
            file_id=1, file_uuid="uuid-1", user_id=1, pipeline_task_id="task-abc"
        )

        assert calls["index"] == ((1, "uuid-1", 1), {"pipeline_task_id": "task-abc"})

    def test_summarization_failure_doesnt_block_others(self, fanout, monkeypatch):
        postprocess, calls, plans = fanout

        def broken(*a, **k):
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr("app.tasks.transcription.core.trigger_automatic_summarization", broken)

        postprocess.enrich_and_dispatch(file_id=1, file_uuid="uuid-1", user_id=1)

        assert {"index", "attrs", "cluster", "facts"} <= set(calls)
        # The summary step decided nothing, so nothing waits for it.
        assert PostProcessingTask.ANALYTICS not in plans[0]
        assert plans[0][PostProcessingTask.SEARCH_INDEXING] is PostProcessingState.QUEUED

    def test_an_indexing_dispatch_failure_is_planned_failed_and_does_not_block(self, monkeypatch):
        """The REAL _index_transcript: a broker error becomes FAILED, never an exception."""
        from app.services import post_processing_status as pps
        from app.tasks.transcription import postprocess

        def broken(*a, **k):
            raise ConnectionError("broker down")

        monkeypatch.setattr(
            "app.tasks.search_indexing_task.index_transcript_search_task.delay", broken
        )
        assert postprocess._index_transcript(1, "uuid-1", 1) is PostProcessingState.FAILED
        assert pps.outcome_of({"status": "failed"}) is PostProcessingState.FAILED


@contextmanager
def _fake_session_scope():
    """A no-op DB session for tests that never touch real state."""
    yield MagicMock()


class TestSpeakerEmbeddingQueueRouting:
    """Issue #584: lite mode runs zero workers on the ``gpu`` queue.

    A cloud-ASR file whose provider already supplied diarization only needs
    ``extract_speaker_embeddings_task`` — pure embedding extraction from known
    segments, no GPU diarization model — so it must not be pinned to ``gpu``.
    A cloud-ASR file needing LOCAL diarization dispatches ``rediarize_task``
    instead, which genuinely runs the PyAnnote clustering pass and must stay
    on ``gpu``.
    """

    def _gpu_result(self, **overrides) -> dict:
        base = {
            "status": "success",
            "file_uuid": "file-uuid-1",
            "file_id": 1,
            "user_id": 7,
            "task_id": "task-1",
            "speaker_mapping": {"SPEAKER_00": 1},
            "native_embeddings": None,
            "use_native_embeddings": False,
            "asr_provider": "deepgram",
            "downstream_tasks": None,
            "diarization_disabled": False,
            "pipeline_plan": plan_context("cloud_speakers", "provider"),
        }
        base.update(overrides)
        return base

    @pytest.mark.usefixtures("privacy_allows_all")
    @patch(f"{_POSTPROCESS}.enrich_and_dispatch")
    @patch(f"{_POSTPROCESS}.send_completion_notification")
    @patch(f"{_POSTPROCESS}.send_progress_notification")
    @patch(f"{_POSTPROCESS}.update_task_status")
    @patch(f"{_POSTPROCESS}.session_scope", new=_fake_session_scope)
    @patch("app.tasks.speaker_embedding_task.extract_speaker_embeddings_task.apply_async")
    def test_cloud_asr_provider_diarization_routes_embedding_task_off_gpu(
        self,
        mock_apply_async,
        mock_update_task_status,
        mock_progress,
        mock_completion,
        mock_enrich,
    ):
        """Cloud ASR + provider diarization: embedding extraction must NOT land on 'gpu'.

        Lite mode (docker-compose.lite.yml) scales celery-worker (the sole 'gpu'
        consumer) to replicas: 0, so a task pinned to that queue never runs —
        the file's voiceprints silently never materialize. The CPU worker
        (-Q cpu,utility,cpu-transcribe) is what stays up in lite mode.
        """
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.postprocess import finalize_transcription

        result = finalize_transcription.__wrapped__(self._gpu_result())

        # Real outcome, not mock bookkeeping: the pipeline must still report
        # success — the dispatch fix must not change the task's own result.
        assert result == {"status": "success", "file_id": 1, "segment_count": 0}

        mock_apply_async.assert_called_once()
        assert mock_apply_async.call_args.kwargs["queue"] != CeleryQueues.GPU, (
            "extract_speaker_embeddings_task only reads known segments and runs "
            "SpeakerEmbeddingService.extract_embedding_from_segment (already "
            "device-agnostic per hardware_detection.py) — it must route to a "
            "queue lite mode actually has a worker on"
        )
        assert mock_apply_async.call_args.kwargs["queue"] == CeleryQueues.CPU

    @patch(f"{_POSTPROCESS}.enrich_and_dispatch")
    @patch(f"{_POSTPROCESS}.send_completion_notification")
    @patch(f"{_POSTPROCESS}.send_progress_notification")
    @patch(f"{_POSTPROCESS}.update_task_status")
    @patch(f"{_POSTPROCESS}.session_scope", new=_fake_session_scope)
    @patch("app.tasks.rediarize_task.rediarize_task.apply_async")
    def test_cloud_asr_local_diarization_still_routes_rediarize_to_gpu(
        self,
        mock_apply_async,
        mock_update_task_status,
        mock_progress,
        mock_completion,
        mock_enrich,
    ):
        """Cloud ASR + LOCAL diarization: rediarize_task genuinely runs PyAnnote
        clustering (real diarization, not just embedding extraction), so it must
        stay on 'gpu' — this path is deliberately untouched by the #584 fix.
        """
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.postprocess import finalize_transcription

        result = finalize_transcription.__wrapped__(
            self._gpu_result(pipeline_plan=plan_context("cloud_speakers", "local"))
        )

        # Real outcome, not mock bookkeeping: still reports success even though
        # completion is deferred to rediarize_task.
        assert result == {"status": "success", "file_id": 1, "segment_count": 0}

        mock_apply_async.assert_called_once()
        assert mock_apply_async.call_args.kwargs["queue"] == CeleryQueues.GPU


class TestTaskRoutesQueueAssignment:
    """The static ``task_routes`` fallback must agree with the call-site fix.

    Any dispatch that omits an explicit ``queue=`` kwarg (e.g. a future call
    site, or Celery's own routing when none is passed) falls back to this
    table — it must not silently re-pin the task to 'gpu'.
    """

    def test_extract_speaker_embeddings_route_is_not_gpu(self):
        from app.core.celery import celery_app
        from app.core.constants import CeleryQueues

        assert celery_app.conf.task_routes["extract_speaker_embeddings"] == {
            "queue": CeleryQueues.CPU
        }

    def test_rediarize_route_is_still_gpu(self):
        """rediarize genuinely needs the GPU diarization model — untouched."""
        from app.core.celery import celery_app
        from app.core.constants import CeleryQueues

        assert celery_app.conf.task_routes["rediarize"] == {"queue": CeleryQueues.GPU}
