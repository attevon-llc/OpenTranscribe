"""Tests for the v4 embedding migration tasks and helpers."""

from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

# Patch path for get_opensearch_client -- it's imported inside function bodies
# from app.services.opensearch_service, so we patch at the source.
_OS_CLIENT_PATCH = "app.services.opensearch_service.get_opensearch_client"


class TestGetAlreadyMigratedFileIds:
    """Tests for _get_already_migrated_file_ids skip logic."""

    @patch(_OS_CLIENT_PATCH)
    def test_returns_empty_set_when_no_client(self, mock_get_client):
        from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

        mock_get_client.return_value = None
        assert _get_already_migrated_file_ids() == set()

    @patch(_OS_CLIENT_PATCH)
    def test_returns_empty_set_when_v4_index_missing(self, mock_get_client):
        from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False
        mock_get_client.return_value = mock_client
        assert _get_already_migrated_file_ids() == set()

    @patch("app.tasks.embedding_migration_v4._count_embeddable_speakers_per_file")
    @patch(_OS_CLIENT_PATCH)
    def test_returns_file_ids_from_aggregation(self, mock_get_client, mock_count):
        from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        # Composite aggregation shape (issue #657, defect 6: replaced the
        # 50,000-bucket-capped `terms` agg with a paginated `composite` one).
        mock_client.search.return_value = {
            "aggregations": {
                "file_ids": {
                    "buckets": [
                        {"key": {"media_file_id": 10}, "doc_count": 3},
                        {"key": {"media_file_id": 25}, "doc_count": 1},
                        {"key": {"media_file_id": 42}, "doc_count": 5},
                    ],
                    "after_key": {"media_file_id": 42},
                }
            }
        }
        mock_get_client.return_value = mock_client
        # Embeddable counts match or are below v4 doc_count so all are "fully migrated"
        mock_count.return_value = {10: 3, 25: 1, 42: 5}

        result = _get_already_migrated_file_ids()
        assert result == {10, 25, 42}

    @patch("app.tasks.embedding_migration_v4._count_embeddable_speakers_per_file")
    @patch(_OS_CLIENT_PATCH)
    def test_excludes_partially_migrated_files(self, mock_get_client, mock_count):
        from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.search.return_value = {
            "aggregations": {
                "file_ids": {
                    "buckets": [
                        {"key": {"media_file_id": 10}, "doc_count": 2},  # Only 2 of 5 migrated
                        {"key": {"media_file_id": 25}, "doc_count": 3},  # All 3 migrated
                    ],
                    "after_key": {"media_file_id": 25},
                }
            }
        }
        mock_get_client.return_value = mock_client
        mock_count.return_value = {10: 5, 25: 3}

        result = _get_already_migrated_file_ids()
        # File 10 is partially migrated (2 < 5), file 25 is fully migrated
        assert result == {25}

    @patch(_OS_CLIENT_PATCH)
    def test_returns_empty_on_search_error(self, mock_get_client):
        from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.search.side_effect = Exception("search failed")
        mock_get_client.return_value = mock_client

        result = _get_already_migrated_file_ids()
        assert result == set()


class TestBulkWriteV4Embeddings:
    """Tests for _bulk_write_v4_embeddings."""

    @patch(_OS_CLIENT_PATCH)
    def test_empty_docs_returns_zero(self, mock_get_client):
        from app.tasks.embedding_migration_v4 import _bulk_write_v4_embeddings

        assert _bulk_write_v4_embeddings([]) == 0
        mock_get_client.assert_not_called()

    @patch(_OS_CLIENT_PATCH)
    def test_returns_zero_when_no_client(self, mock_get_client):
        from app.tasks.embedding_migration_v4 import _bulk_write_v4_embeddings

        mock_get_client.return_value = None
        result = _bulk_write_v4_embeddings([{"_id": "abc", "name": "test"}])
        assert result == 0

    @patch("app.tasks.embedding_migration_v4.get_speaker_index_v4", return_value="speakers_v4")
    @patch(_OS_CLIENT_PATCH)
    def test_bulk_writes_documents(self, mock_get_client, mock_get_v4_index):
        from app.tasks.embedding_migration_v4 import _bulk_write_v4_embeddings

        mock_client = MagicMock()
        mock_client.bulk.return_value = {"errors": False, "items": []}
        mock_get_client.return_value = mock_client

        docs = [
            {"_id": "uuid-1", "speaker_id": 1, "name": "Alice", "embedding": [0.1] * 256},
            {"_id": "uuid-2", "speaker_id": 2, "name": "Bob", "embedding": [0.2] * 256},
        ]

        result = _bulk_write_v4_embeddings(docs)
        assert result == 2
        mock_client.bulk.assert_called_once()

        # Verify bulk body structure
        bulk_body = mock_client.bulk.call_args.kwargs["body"]
        assert len(bulk_body) == 4  # 2 action + 2 doc
        assert bulk_body[0] == {"index": {"_index": "speakers_v4", "_id": "uuid-1"}}
        assert bulk_body[2] == {"index": {"_index": "speakers_v4", "_id": "uuid-2"}}

    @patch("app.tasks.embedding_migration_v4.get_speaker_index_v4", return_value="speakers_v4")
    @patch(_OS_CLIENT_PATCH)
    def test_logs_bulk_errors(self, mock_get_client, mock_get_v4_index):
        from app.tasks.embedding_migration_v4 import _bulk_write_v4_embeddings

        mock_client = MagicMock()
        mock_client.bulk.return_value = {
            "errors": True,
            "items": [
                {"index": {"error": {"type": "mapper_parsing_exception", "reason": "bad data"}}}
            ],
        }
        mock_get_client.return_value = mock_client

        # 1 doc sent, 1 failed => returns 0 (len(docs) - failed_count)
        result = _bulk_write_v4_embeddings([{"_id": "x", "data": "bad"}])
        assert result == 0


class TestBuildSpeakerSegments:
    """Tests for _build_speaker_segments."""

    def test_builds_mapping_from_segments(self):
        from app.tasks.embedding_migration_v4 import _build_speaker_segments

        seg1 = MagicMock(speaker_id=1, start_time=0.0, end_time=5.0)
        seg2 = MagicMock(speaker_id=1, start_time=5.0, end_time=10.0)
        seg3 = MagicMock(speaker_id=2, start_time=2.0, end_time=8.0)

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [seg1, seg2, seg3]

        result = _build_speaker_segments(mock_db, media_file_id=42)

        assert 1 in result
        assert 2 in result
        assert len(result[1]) == 2
        assert len(result[2]) == 1
        assert result[1][0] == {"start": 0.0, "end": 5.0}

    def test_returns_empty_for_no_segments(self):
        from app.tasks.embedding_migration_v4 import _build_speaker_segments

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        result = _build_speaker_segments(mock_db, media_file_id=999)
        assert result == {}


class TestPrepareFile:
    """Tests for prepare_file from migration_pipeline.

    This function queries the DB, builds speaker snapshots, generates
    a presigned URL, and returns a PreparedFile (or None/raises).

    Note: get_file_by_uuid and minio_client are lazy-imported inside
    prepare_file(), so we patch them at their source modules.
    """

    @patch("app.services.minio_service.minio_client")
    @patch("app.utils.uuid_helpers.get_file_by_uuid")
    @patch("app.tasks.migration_pipeline.session_scope")
    def test_returns_none_when_no_speakers(self, mock_scope, mock_get_file, mock_minio):
        from app.tasks.migration_pipeline import prepare_file

        mock_db = MagicMock()
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)

        mock_media_file = MagicMock(id=1, user_id=1, storage_path="test.wav")
        mock_get_file.return_value = mock_media_file
        # No speakers found
        mock_db.query.return_value.filter.return_value.all.return_value = []

        result = prepare_file("test-uuid")
        assert result is None

    @patch("app.services.minio_service.minio_client")
    @patch("app.utils.uuid_helpers.get_file_by_uuid")
    @patch("app.tasks.migration_pipeline.session_scope")
    def test_raises_when_file_not_found(self, mock_scope, mock_get_file, mock_minio):
        from app.tasks.migration_pipeline import prepare_file

        mock_db = MagicMock()
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)

        mock_get_file.return_value = None

        with pytest.raises(ValueError, match="not found"):
            prepare_file("missing-uuid")


class TestEmbeddingResultWriter:
    """Tests for _embedding_result_writer.

    This callback receives a PreparedFile and results_by_model dict
    (from the pipeline), aggregates embeddings per speaker, and bulk-writes
    to OpenSearch.
    """

    @staticmethod
    def _make_prepared(speakers, speaker_segments=None, speaker_profiles=None):
        from app.tasks.migration_pipeline import PreparedFile

        return PreparedFile(
            file_uuid="test-uuid",
            audio_source="http://minio:9000/test/audio.mp4?presigned=1",
            speakers=speakers,
            speaker_segments=speaker_segments or {},
            media_file_id=1,
            user_id=1,
            extra={"speaker_profiles": speaker_profiles or {}},
        )

    @staticmethod
    def _make_speaker(speaker_id, uuid, name, profile_id=None):
        from app.tasks.migration_pipeline import SpeakerSnapshot

        return SpeakerSnapshot(id=speaker_id, uuid=uuid, name=name, profile_id=profile_id)

    def test_returns_zero_when_no_embedding_results(self):
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        speaker = self._make_speaker(1, "sp-1", "Alice")
        prepared = self._make_prepared([speaker])

        # No "embedding" key in results
        result = _embedding_result_writer(prepared, {"other_model": []})
        assert result == 0

    def test_returns_zero_when_embedding_results_empty(self):
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        speaker = self._make_speaker(1, "sp-1", "Alice")
        prepared = self._make_prepared([speaker])

        result = _embedding_result_writer(prepared, {"embedding": []})
        assert result == 0

    @patch("app.tasks.embedding_migration_v4._bulk_write_v4_embeddings")
    def test_aggregates_and_writes_single_speaker(self, mock_bulk):
        from app.services.speaker_analysis_models import SegmentResult
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        mock_bulk.return_value = 1  # 1 doc written == 1 doc submitted (full success)
        speaker = self._make_speaker(1, "sp-uuid", "Alice")
        prepared = self._make_prepared(
            [speaker],
            speaker_profiles={1: None},
        )

        emb1 = np.random.randn(256).astype(np.float32)
        emb2 = np.random.randn(256).astype(np.float32)
        results_by_model = {
            "embedding": [
                SegmentResult(model_name="embedding", speaker_id=1, value=emb1),
                SegmentResult(model_name="embedding", speaker_id=1, value=emb2),
            ]
        }

        result = _embedding_result_writer(prepared, results_by_model)

        assert result == 1
        mock_bulk.assert_called_once()
        docs = mock_bulk.call_args[0][0]
        assert len(docs) == 1
        assert docs[0]["_id"] == "sp-uuid"
        assert docs[0]["speaker_id"] == 1
        assert docs[0]["name"] == "Alice"
        assert docs[0]["media_file_id"] == 1
        assert docs[0]["segment_count"] == 2
        assert len(docs[0]["embedding"]) == 256

    @patch("app.tasks.embedding_migration_v4._bulk_write_v4_embeddings")
    def test_aggregates_multiple_speakers(self, mock_bulk):
        from app.services.speaker_analysis_models import SegmentResult
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        mock_bulk.return_value = 2  # both docs written (full success)
        alice = self._make_speaker(1, "sp-alice", "Alice")
        bob = self._make_speaker(2, "sp-bob", "Bob")
        prepared = self._make_prepared(
            [alice, bob],
            speaker_profiles={1: None, 2: None},
        )

        results_by_model = {
            "embedding": [
                SegmentResult(model_name="embedding", speaker_id=1, value=np.ones(256)),
                SegmentResult(model_name="embedding", speaker_id=2, value=np.ones(256) * 2),
            ]
        }

        result = _embedding_result_writer(prepared, results_by_model)

        assert result == 2
        mock_bulk.assert_called_once()
        docs = mock_bulk.call_args[0][0]
        assert len(docs) == 2
        speaker_ids = {d["speaker_id"] for d in docs}
        assert speaker_ids == {1, 2}

    @patch("app.tasks.embedding_migration_v4._bulk_write_v4_embeddings")
    def test_skips_unknown_speaker_id(self, mock_bulk):
        from app.services.speaker_analysis_models import SegmentResult
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        alice = self._make_speaker(1, "sp-alice", "Alice")
        prepared = self._make_prepared([alice])

        # speaker_id=99 is not in prepared.speakers
        results_by_model = {
            "embedding": [
                SegmentResult(model_name="embedding", speaker_id=99, value=np.ones(256)),
            ]
        }

        result = _embedding_result_writer(prepared, results_by_model)
        assert result == 0
        mock_bulk.assert_not_called()

    @patch("app.tasks.embedding_migration_v4._bulk_write_v4_embeddings")
    def test_normalizes_aggregated_embedding(self, mock_bulk):
        from app.services.speaker_analysis_models import SegmentResult
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        mock_bulk.return_value = 1  # full success
        speaker = self._make_speaker(1, "sp-1", "Alice")
        prepared = self._make_prepared([speaker], speaker_profiles={1: None})

        # Two embeddings that should be averaged and L2-normalized
        emb1 = np.array([3.0, 0.0] + [0.0] * 254)
        emb2 = np.array([1.0, 0.0] + [0.0] * 254)
        results_by_model = {
            "embedding": [
                SegmentResult(model_name="embedding", speaker_id=1, value=emb1),
                SegmentResult(model_name="embedding", speaker_id=1, value=emb2),
            ]
        }

        _embedding_result_writer(prepared, results_by_model)

        docs = mock_bulk.call_args[0][0]
        embedding = np.array(docs[0]["embedding"])
        # Mean of [3,0,...] and [1,0,...] is [2,0,...], normalized to [1,0,...]
        assert abs(np.linalg.norm(embedding) - 1.0) < 1e-6


class TestGetMigrationStatus:
    """Tests for get_migration_status."""

    @patch("app.tasks.embedding_migration_v4.EmbeddingModeService")
    @patch(_OS_CLIENT_PATCH)
    def test_returns_error_when_no_client(self, mock_get_client, mock_mode):
        from app.tasks.embedding_migration_v4 import get_migration_status

        mock_get_client.return_value = None
        result = get_migration_status()
        assert result["status"] == "error"

    @patch("app.tasks.embedding_migration_v4.EmbeddingModeService")
    @patch(_OS_CLIENT_PATCH)
    def test_includes_transcription_paused_flag(self, mock_get_client, mock_mode):
        from app.tasks.embedding_migration_v4 import get_migration_status

        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False
        mock_client.count.return_value = {"count": 0}
        mock_get_client.return_value = mock_client
        mock_mode.detect_mode.return_value = "v3"

        result = get_migration_status()
        assert result["transcription_paused"] is False
        assert result["migration_needed"] is True


class TestPreparedFileDataclass:
    """Tests for the PreparedFile and SpeakerSnapshot dataclasses from migration_pipeline."""

    def test_prepared_file_construction(self):
        from app.tasks.migration_pipeline import PreparedFile
        from app.tasks.migration_pipeline import SpeakerSnapshot

        speaker = SpeakerSnapshot(id=1, uuid="sp-1", name="Alice", profile_id=None)
        pf = PreparedFile(
            file_uuid="test-uuid",
            audio_source="http://example.com/audio.mp4",
            speakers=[speaker],
            speaker_segments={1: [{"start": 0.0, "end": 5.0}]},
            media_file_id=42,
            user_id=1,
        )

        assert pf.file_uuid == "test-uuid"
        assert len(pf.speakers) == 1
        assert pf.speakers[0].name == "Alice"
        assert pf.media_file_id == 42
        assert pf.user_id == 1
        assert pf.extra == {}

    def test_prepared_file_extra_field(self):
        from app.tasks.migration_pipeline import PreparedFile

        pf = PreparedFile(
            file_uuid="uuid",
            audio_source="url",
            speakers=[],
            speaker_segments={},
            media_file_id=1,
            user_id=1,
            extra={"speaker_profiles": {1: "profile-uuid"}},
        )

        assert pf.extra["speaker_profiles"][1] == "profile-uuid"

    def test_speaker_snapshot_defaults(self):
        from app.tasks.migration_pipeline import SpeakerSnapshot

        sp = SpeakerSnapshot(id=1, uuid="sp-1", name="Alice")
        assert sp.profile_id is None


class TestCountProfileDocs:
    """Tests for _count_profile_docs — must never return 0 on ambiguity."""

    def test_returns_none_when_index_missing(self):
        from app.tasks.embedding_migration_v4 import _count_profile_docs

        client = MagicMock()
        client.indices.exists.return_value = False
        assert _count_profile_docs(client, "speakers_v3") is None
        client.count.assert_not_called()

    def test_returns_none_on_count_error(self):
        from app.tasks.embedding_migration_v4 import _count_profile_docs

        client = MagicMock()
        client.indices.exists.return_value = True
        client.count.side_effect = RuntimeError("opensearch unavailable")
        assert _count_profile_docs(client, "speakers_v3") is None

    def test_returns_count_and_filters_by_document_type(self):
        from app.tasks.embedding_migration_v4 import _count_profile_docs

        client = MagicMock()
        client.indices.exists.return_value = True
        client.count.return_value = {"count": 7}
        assert _count_profile_docs(client, "speakers_v3") == 7
        _, kwargs = client.count.call_args
        assert kwargs["index"] == "speakers_v3"
        assert kwargs["body"]["query"]["term"]["document_type"] == "profile"


class TestFinalizeV4MigrationProfileGuard:
    """Fail-closed profile-voiceprint guard on finalize_v4_migration_task (issue #657).

    OpenSearch is the ONLY copy of profile voiceprints (Postgres stores no
    embedding vectors). The total-doc-count ratio check cannot detect a
    profile-only shortfall, so this guard is separate and must refuse the
    alias swap on any shortfall or any ambiguity in the counts.
    """

    @patch("app.services.opensearch_service.swap_speaker_alias")
    @patch("app.services.opensearch_service.get_active_versioned_index")
    @patch(_OS_CLIENT_PATCH)
    def test_refuses_swap_on_profile_shortfall(self, mock_get_client, mock_active_index, mock_swap):
        """v3 has profile docs v4 lacks -> finalize must refuse, alias must not move."""
        from app.tasks.embedding_migration_v4 import finalize_v4_migration_task

        client = MagicMock()
        client.indices.exists.return_value = True
        client.indices.flush.return_value = {}
        # No v4 speaker docs to backfill profiles from -- migrate_profile_documents_to_v4 returns immediately.
        client.search.return_value = {"hits": {"hits": []}}
        mock_active_index.return_value = "speakers_v3"

        def count_side_effect(index, body=None):
            is_profile_query = (
                body is not None
                and body.get("query", {}).get("term", {}).get("document_type") == "profile"
            )
            if is_profile_query:
                # v3 holds 3 profile docs, v4 holds 0 -> shortfall.
                return {"count": 3 if index == "speakers_v3" else 0}
            # Total-doc-count check: keep it comfortably passing.
            return {"count": 100}

        client.count.side_effect = count_side_effect
        mock_get_client.return_value = client

        result = finalize_v4_migration_task.run(user_id=1)

        assert result["status"] == "error"
        assert "profile" in result["message"].lower()
        mock_swap.assert_not_called()

    @patch("app.services.opensearch_service.swap_speaker_alias")
    @patch("app.services.opensearch_service.get_active_versioned_index")
    @patch(_OS_CLIENT_PATCH)
    def test_refuses_swap_when_profile_count_unavailable(
        self, mock_get_client, mock_active_index, mock_swap
    ):
        """Profile count query errors -> ambiguity must refuse, never proceed."""
        from app.tasks.embedding_migration_v4 import finalize_v4_migration_task

        client = MagicMock()
        client.indices.exists.return_value = True
        client.indices.flush.return_value = {}
        # No v4 speaker docs to backfill profiles from -- migrate_profile_documents_to_v4 returns immediately.
        client.search.return_value = {"hits": {"hits": []}}
        mock_active_index.return_value = "speakers_v3"

        def count_side_effect(index, body=None):
            is_profile_query = (
                body is not None
                and body.get("query", {}).get("term", {}).get("document_type") == "profile"
            )
            if is_profile_query:
                raise RuntimeError("opensearch timeout")
            return {"count": 100}

        client.count.side_effect = count_side_effect
        mock_get_client.return_value = client

        result = finalize_v4_migration_task.run(user_id=1)

        assert result["status"] == "error"
        assert "cannot verify" in result["message"].lower()
        mock_swap.assert_not_called()

    @patch("app.services.opensearch_service.swap_speaker_alias")
    @patch("app.services.opensearch_service.get_active_versioned_index")
    @patch(_OS_CLIENT_PATCH)
    def test_allows_swap_when_profile_counts_are_not_short(
        self, mock_get_client, mock_active_index, mock_swap
    ):
        """v4 profile count >= v3 profile count -> proceeds to swap."""
        from app.tasks.embedding_migration_v4 import finalize_v4_migration_task

        client = MagicMock()
        client.indices.exists.return_value = True
        client.indices.flush.return_value = {}
        # No v4 speaker docs to backfill profiles from -- migrate_profile_documents_to_v4 returns immediately.
        client.search.return_value = {"hits": {"hits": []}}
        mock_active_index.return_value = "speakers_v3"
        mock_swap.return_value = {"status": "success"}

        def count_side_effect(index, body=None):
            is_profile_query = (
                body is not None
                and body.get("query", {}).get("term", {}).get("document_type") == "profile"
            )
            if is_profile_query:
                return {"count": 3}
            return {"count": 100}

        client.count.side_effect = count_side_effect
        mock_get_client.return_value = client

        result = finalize_v4_migration_task.run(user_id=1)

        assert result["status"] == "success"
        mock_swap.assert_called_once()
