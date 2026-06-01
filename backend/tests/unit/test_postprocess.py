"""Tests for transcription postprocess: enrichment task list and background dispatch."""

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
