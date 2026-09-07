"""Failures counted as successes in the v4 migration (issue #657, defect 5).

Two distinct swallow sites, both fixed here:

1. ``prepare_file`` returned ``None`` for BOTH a legitimate skip (no
   speakers) and a real permanent failure (missing storage_path, missing
   MinIO object). The caller treated every ``None`` as
   ``increment_processed(success=True)``, so a permanently-missing file was
   marked done, never entered ``failed_files``, and was retried forever by
   ``/retry-failed`` while its 512-d voiceprint was silently orphaned at
   finalize. ``PermanentFileError`` now separates the two cases.
2. ``_embedding_result_writer`` ignored ``_bulk_write_v4_embeddings``'s
   return value, so a total bulk-write failure (which returns 0) still
   reported ``len(docs)`` written, and ``process_batch_pipelined`` called
   ``on_file_success`` regardless — the file was marked done with nothing
   actually indexed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.tasks import migration_pipeline as pipeline_mod


def _make_query_db(speakers, segments):
    """A MagicMock db whose .query(Model) returns the right rows per model.

    prepare_file queries Speaker (no order_by) then TranscriptSegment (with
    an order_by chain) — a flat ``.filter().all()`` side_effect list can't
    tell those two calls apart because order_by() returns a distinct mock.
    """
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    db = MagicMock()

    def _query(model, *args, **kwargs):
        result = MagicMock()
        if model is Speaker:
            result.filter.return_value.all.return_value = speakers
        elif model is TranscriptSegment:
            result.filter.return_value.order_by.return_value.all.return_value = segments
        return result

    db.query.side_effect = _query
    return db


@pytest.mark.unit
class TestPrepareFileDistinguishesSkipFromFailure:
    def test_missing_storage_path_raises_permanent_file_error(self, monkeypatch):
        media_file = MagicMock(storage_path=None, id=1, user_id=1)
        speaker = MagicMock(id=10, uuid="spk-uuid", name="Speaker 1", profile_id=None)
        segment = MagicMock(speaker_id=10, start_time=0.0, end_time=1.0)

        db = _make_query_db(speakers=[speaker], segments=[segment])

        monkeypatch.setattr(pipeline_mod, "session_scope", lambda: _FakeSessionCtx(db))
        monkeypatch.setattr(
            "app.utils.uuid_helpers.get_file_by_uuid", lambda db_arg, uuid: media_file
        )

        with pytest.raises(pipeline_mod.PermanentFileError):
            pipeline_mod.prepare_file("file-uuid-1")

    def test_missing_minio_object_raises_permanent_file_error(self, monkeypatch):
        media_file = MagicMock(storage_path="path/to/object", id=1, user_id=1)
        speaker = MagicMock(id=10, uuid="spk-uuid", name="Speaker 1", profile_id=None)
        segment = MagicMock(speaker_id=10, start_time=0.0, end_time=1.0)

        db = _make_query_db(speakers=[speaker], segments=[segment])

        monkeypatch.setattr(pipeline_mod, "session_scope", lambda: _FakeSessionCtx(db))
        monkeypatch.setattr(
            "app.utils.uuid_helpers.get_file_by_uuid", lambda db_arg, uuid: media_file
        )

        fake_minio = MagicMock()
        fake_minio.stat_object.side_effect = Exception("NoSuchKey")
        monkeypatch.setattr("app.services.minio_service.minio_client", fake_minio)

        with pytest.raises(pipeline_mod.PermanentFileError):
            pipeline_mod.prepare_file("file-uuid-1")

    def test_no_speakers_is_a_legitimate_skip_returns_none(self, monkeypatch):
        media_file = MagicMock(storage_path="path/to/object", id=1, user_id=1)
        db = _make_query_db(speakers=[], segments=[])

        monkeypatch.setattr(pipeline_mod, "session_scope", lambda: _FakeSessionCtx(db))
        monkeypatch.setattr(
            "app.utils.uuid_helpers.get_file_by_uuid", lambda db_arg, uuid: media_file
        )

        assert pipeline_mod.prepare_file("file-uuid-1") is None


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False


@pytest.mark.unit
class TestEmbeddingResultWriterRaisesOnBulkWriteFailure:
    def test_total_bulk_write_failure_raises_instead_of_reporting_success(self, monkeypatch):
        from app.tasks import embedding_migration_v4 as mig

        prepared = MagicMock()
        prepared.extra = {"speaker_profiles": {}}
        prepared.media_file_id = 42
        speaker = MagicMock(id=1, uuid="spk-1", name="Speaker 1", profile_id=None)
        prepared.speakers = [speaker]

        segment_result = MagicMock(speaker_id=1)
        segment_result.value = __import__("numpy").array([1.0, 0.0])

        monkeypatch.setattr(mig, "_bulk_write_v4_embeddings", lambda docs: 0)

        with pytest.raises(RuntimeError):
            mig._embedding_result_writer(prepared, {"embedding": [segment_result]})

    def test_full_bulk_write_success_does_not_raise(self, monkeypatch):
        from app.tasks import embedding_migration_v4 as mig

        prepared = MagicMock()
        prepared.extra = {"speaker_profiles": {}}
        prepared.media_file_id = 42
        speaker = MagicMock(id=1, uuid="spk-1", name="Speaker 1", profile_id=None)
        prepared.speakers = [speaker]

        segment_result = MagicMock(speaker_id=1)
        segment_result.value = __import__("numpy").array([1.0, 0.0])

        monkeypatch.setattr(mig, "_bulk_write_v4_embeddings", lambda docs: len(docs))

        count = mig._embedding_result_writer(prepared, {"embedding": [segment_result]})
        assert count == 1
