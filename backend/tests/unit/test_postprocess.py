"""Tests for transcription postprocess: enrichment task list and background dispatch."""

from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

# Patch paths — these are imported at the top of postprocess.py
_POSTPROCESS = "app.tasks.transcription.postprocess"
_INDEX_TRANSCRIPT = f"{_POSTPROCESS}._index_transcript"
_SEND_WS = f"{_POSTPROCESS}.send_ws_event"
_DISPATCH_ATTRS = f"{_POSTPROCESS}._dispatch_speaker_attributes"
_DISPATCH_CLUSTERING = f"{_POSTPROCESS}._dispatch_speaker_clustering"

# Lazy import inside enrich_and_dispatch
_TRIGGER_SUMMARIZATION = "app.tasks.transcription.core.trigger_automatic_summarization"

# Full enrichment task list when nothing is excluded. Summarization is NOT
# included here — it has its own progressive notification bar. Only
# speaker_clustering is conditionally excluded (when already in downstream_tasks).
_FULL_TASK_LIST = [
    "search_indexing",
    "analytics",
    "speaker_attributes",
    "speaker_identification",
    "speaker_clustering",
]


class TestBuildEnrichmentTaskList:
    """Tests for _build_enrichment_task_list()."""

    def test_none_returns_all_tasks(self):
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list(None)
        assert result == _FULL_TASK_LIST

    def test_empty_list_returns_all_tasks(self):
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list([])
        assert result == _FULL_TASK_LIST

    def test_speaker_llm_no_longer_changes_list(self):
        """speaker_llm in downstream_tasks no longer excludes speaker_attributes.

        That exclusion was removed — speaker_attributes (gender detection) always
        runs because LLM speaker ID chains from it. speaker_llm now has no effect
        on the enrichment task list.
        """
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list(["speaker_llm"])
        assert result == _FULL_TASK_LIST
        assert "speaker_attributes" in result
        assert "speaker_clustering" in result
        assert "summarization" not in result

    def test_speaker_clustering_excludes_clustering(self):
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list(["speaker_clustering"])
        assert "speaker_clustering" not in result
        assert "speaker_attributes" in result
        assert "summarization" not in result
        # Everything except speaker_clustering remains, in order.
        assert result == [t for t in _FULL_TASK_LIST if t != "speaker_clustering"]

    def test_summarization_in_downstream_has_no_effect(self):
        """summarization in downstream_tasks does not change the enrichment list.

        Summarization is never listed in the enrichment chips (it has its own
        progress bar), and it does not exclude any other task.
        """
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list(["summarization"])
        assert result == _FULL_TASK_LIST
        assert "summarization" not in result
        assert "speaker_attributes" in result
        assert "speaker_clustering" in result

    def test_only_speaker_clustering_is_conditionally_excluded(self):
        """Of the historical exclusions, only speaker_clustering still applies.

        speaker_llm and summarization no longer exclude anything; passing all
        three only drops speaker_clustering.
        """
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list(["speaker_llm", "speaker_clustering", "summarization"])
        assert result == [t for t in _FULL_TASK_LIST if t != "speaker_clustering"]

    def test_search_indexing_always_present(self):
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        # search_indexing cannot be excluded and is always first.
        for input_val in [None, [], ["speaker_llm"], ["speaker_clustering", "summarization"]]:
            result = _build_enrichment_task_list(input_val)
            assert result[0] == "search_indexing"

    def test_unrelated_tasks_no_effect(self):
        from app.tasks.transcription.postprocess import _build_enrichment_task_list

        result = _build_enrichment_task_list(["topic_extraction", "something_else"])
        assert result == _FULL_TASK_LIST


class TestEnrichAndDispatch:
    """Tests for enrich_and_dispatch() Celery task."""

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_calls_all_downstream_tasks(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=1,
            downstream_tasks=None,
        )

        mock_index.assert_called_once_with(1, "uuid-1", 1, pipeline_task_id=None)
        mock_summarize.assert_called_once_with(1, "uuid-1", tasks_to_run=None)
        mock_attrs.assert_called_once_with("uuid-1", 1, None)
        mock_cluster.assert_called_once_with("uuid-1", 1, None)

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_sends_search_indexing_ws_event(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=7,
            downstream_tasks=None,
        )

        mock_ws.assert_called_once_with(
            7,
            "enrichment_task_complete",
            {"file_id": "uuid-1", "task": "search_indexing"},
        )

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_indexing_failure_doesnt_block_others(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        mock_index.side_effect = RuntimeError("OpenSearch down")

        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=1,
            downstream_tasks=None,
        )

        # WebSocket event NOT sent (indexing failed)
        mock_ws.assert_not_called()
        # But all other tasks still dispatched
        mock_summarize.assert_called_once()
        mock_attrs.assert_called_once()
        mock_cluster.assert_called_once()

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_summarization_failure_doesnt_block_others(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        mock_summarize.side_effect = RuntimeError("LLM unavailable")

        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=1,
            downstream_tasks=None,
        )

        mock_index.assert_called_once()
        mock_attrs.assert_called_once()
        mock_cluster.assert_called_once()

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_speaker_attr_failure_doesnt_block_clustering(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        mock_attrs.side_effect = RuntimeError("Speaker service down")

        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=1,
            downstream_tasks=None,
        )

        mock_cluster.assert_called_once()

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_passes_downstream_tasks_through(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        downstream = ["summarization"]
        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=1,
            downstream_tasks=downstream,
        )

        mock_summarize.assert_called_once_with(1, "uuid-1", tasks_to_run=downstream)
        mock_attrs.assert_called_once_with("uuid-1", 1, downstream)
        mock_cluster.assert_called_once_with("uuid-1", 1, downstream)

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_passes_file_params_correctly(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        enrich_and_dispatch(
            file_id=42,
            file_uuid="abc-def-123",
            user_id=7,
            downstream_tasks=None,
        )

        mock_index.assert_called_once_with(42, "abc-def-123", 7, pipeline_task_id=None)
        mock_summarize.assert_called_once_with(42, "abc-def-123", tasks_to_run=None)
        mock_attrs.assert_called_once_with("abc-def-123", 7, None)
        mock_cluster.assert_called_once_with("abc-def-123", 7, None)

    @patch(_DISPATCH_CLUSTERING)
    @patch(_DISPATCH_ATTRS)
    @patch(_TRIGGER_SUMMARIZATION)
    @patch(_SEND_WS)
    @patch(_INDEX_TRANSCRIPT)
    def test_propagates_pipeline_task_id_to_indexing(
        self,
        mock_index,
        mock_ws,
        mock_summarize,
        mock_attrs,
        mock_cluster,
    ):
        """pipeline_task_id is forwarded into _index_transcript for benchmark markers."""
        from app.tasks.transcription.postprocess import enrich_and_dispatch

        enrich_and_dispatch(
            file_id=1,
            file_uuid="uuid-1",
            user_id=1,
            downstream_tasks=None,
            pipeline_task_id="task-abc",
        )

        mock_index.assert_called_once_with(1, "uuid-1", 1, pipeline_task_id="task-abc")


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
            "diarization_source": "provider",
        }
        base.update(overrides)
        return base

    @patch(f"{_POSTPROCESS}.enrich_and_dispatch")
    @patch(f"{_POSTPROCESS}.send_ws_event")
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
        mock_ws,
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
    @patch(f"{_POSTPROCESS}.send_ws_event")
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
        mock_ws,
        mock_enrich,
    ):
        """Cloud ASR + LOCAL diarization: rediarize_task genuinely runs PyAnnote
        clustering (real diarization, not just embedding extraction), so it must
        stay on 'gpu' — this path is deliberately untouched by the #584 fix.
        """
        from app.core.constants import CeleryQueues
        from app.tasks.transcription.postprocess import finalize_transcription

        result = finalize_transcription.__wrapped__(self._gpu_result(diarization_source="local"))

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
