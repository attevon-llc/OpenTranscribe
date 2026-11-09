"""Task-module failure handlers: the failure is counted/recorded AND carries a traceback.

Covers the handlers that used to log ``str(exc)`` only, plus the one whose failure was
invisible to the run it belonged to (a crashed repair phase reported zero repairs and
never counted its files).
"""

import logging
import uuid
from unittest.mock import MagicMock

import pytest
from kombu.exceptions import OperationalError as BrokerDown

from app.models.media import MediaFile
from app.tasks import combined_speaker_analysis_task
from app.tasks import embedding_consistency_repair
from app.tasks import erasure_reconciliation
from app.tasks import ingest_artifacts_task
from app.tasks import migration_pipeline
from app.tasks.migration_pipeline import PreparedFile


def _traceback_logged(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def test_a_crashed_repair_phase_marks_every_file_failed(monkeypatch, caplog):
    from app.services import speaker_embedding_service

    def _boom(mode):
        raise OSError("model weights unreadable")

    monkeypatch.setattr(speaker_embedding_service, "get_cached_embedding_service", _boom)
    failed: list[str] = []

    with caplog.at_level(logging.ERROR, logger=embedding_consistency_repair.logger.name):
        repaired = embedding_consistency_repair._run_repair_phase(
            "v3",
            set(),
            lambda *a: 0,
            [("f-1", MagicMock()), ("f-2", MagicMock())],
            {},
            lambda: True,
            0,
            on_file_done=None,
            on_file_fail=failed.append,
        )

    assert repaired == 0
    assert failed == ["f-1", "f-2"]
    assert _traceback_logged(caplog, "repair failed for batch 0")


def test_file_facts_dispatch_swallows_only_broker_outages(monkeypatch, caplog):
    task = MagicMock()
    monkeypatch.setattr(ingest_artifacts_task, "generate_file_facts_task", task)

    task.delay.side_effect = BrokerDown("broker unreachable")
    with caplog.at_level(logging.WARNING, logger=ingest_artifacts_task.logger.name):
        assert ingest_artifacts_task.dispatch_file_facts(7) is False
    assert _traceback_logged(caplog, "Failed to dispatch file_facts")

    task.delay.side_effect = TypeError("wrong kwargs: a bug, not an outage")
    with pytest.raises(TypeError):
        ingest_artifacts_task.dispatch_file_facts(7)


def test_hold_release_dispatch_swallows_only_broker_outages(monkeypatch, caplog):
    sweep = MagicMock()
    monkeypatch.setattr(erasure_reconciliation, "erasure_reconciliation_sweep", sweep)
    file = MediaFile(user_id=1, uuid=uuid.uuid4())

    sweep.delay.side_effect = BrokerDown("broker unreachable")
    with caplog.at_level(logging.WARNING, logger=erasure_reconciliation.logger.name):
        erasure_reconciliation.notify_hold_released(file)
    assert _traceback_logged(caplog, "Could not dispatch erasure reconciliation")

    sweep.delay.side_effect = AttributeError("a bug, not an outage")
    with pytest.raises(AttributeError):
        erasure_reconciliation.notify_hold_released(file)


def test_pipelined_batch_counts_a_failed_file_and_logs_traceback(monkeypatch, caplog):
    monkeypatch.setattr(migration_pipeline, "submit_segment_fetches", lambda *a, **k: [])
    monkeypatch.setattr(migration_pipeline, "_process_file_segments", lambda *a: {})

    def _writer(prepared, results):
        raise ValueError("opensearch rejected the write")

    failures: list[str] = []
    with caplog.at_level(logging.ERROR, logger=migration_pipeline.logger.name):
        success, failed = migration_pipeline.process_batch_pipelined(
            prepared_files=[("file-uuid-0001-xxxx", MagicMock())],
            runner=MagicMock(),
            result_writer=_writer,
            is_running_check=lambda: True,
            on_file_success=lambda f: None,
            on_file_failure=lambda f, e: failures.append(f),
        )
    assert (success, failed) == (0, 1)
    assert failures == ["file-uuid-0001-xxxx"]
    assert _traceback_logged(caplog, "failed:")


def test_combined_writer_embedding_failure_logs_traceback_and_gender_still_runs(
    monkeypatch, caplog
):
    from app.tasks import embedding_migration_v4_writer
    from app.tasks import speaker_attribute_migration_task

    def _boom(prepared, results):
        raise OSError("opensearch down")

    monkeypatch.setattr(embedding_migration_v4_writer, "_embedding_result_writer", _boom)
    monkeypatch.setattr(speaker_attribute_migration_task, "_gender_result_writer", lambda p, r: 3)
    prepared = PreparedFile(
        file_uuid="abcdef0123456789",
        audio_source="audio-not-read-by-this-test.wav",
        speakers=[],
        speaker_segments={},
        media_file_id=1,
        user_id=1,
    )

    with caplog.at_level(logging.ERROR, logger=combined_speaker_analysis_task.logger.name):
        count = combined_speaker_analysis_task._combined_result_writer(prepared, {})

    assert count == 3
    assert _traceback_logged(caplog, "Embedding write failed")
