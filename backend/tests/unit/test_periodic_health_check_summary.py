"""``system.health_check`` wires every recovery step into its summary, in order."""

from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

from app.tasks import recovery
from app.utils import task_lock


@contextmanager
def _scope():
    yield MagicMock(name="db")


@contextmanager
def _lock_held(*_args, **_kwargs):
    yield True


def _services(*, found: int = 2):
    det = MagicMock(name="detection")
    rec = MagicMock(name="recovery")
    items = ["x"] * found
    det.identify_stuck_tasks.return_value = items
    rec.recover_stuck_task.side_effect = [True, False][:found]
    det.identify_inconsistent_media_files.return_value = items
    rec.fix_inconsistent_media_file.return_value = True
    det.identify_stuck_files_without_active_celery_tasks.return_value = items
    rec.recover_stuck_files_without_celery_tasks.return_value = {"files_recovered": 1}
    det.identify_stuck_downloading_files.return_value = items
    rec.recover_stuck_downloading_files.return_value = {"files_recovered": 2, "tasks_retried": 0}
    det.identify_oom_error_files.return_value = items
    rec.recover_oom_error_files.return_value = {"files_retried": 3, "files_exhausted": 1}
    det.identify_retriable_error_files_split.return_value = (["y"], ["z", "w"])
    rec.recover_retriable_error_files.return_value = {"files_retried": 2, "files_failed": 1}
    det.identify_stuck_pending_download_files.return_value = items
    rec.recover_stuck_pending_download_files.return_value = {"files_marked_error": 2}
    det.identify_stuck_llm_tasks.return_value = items
    rec.recover_stuck_llm_tasks.return_value = {"tasks_marked_failed": 1}
    det.identify_false_positive_failed_tasks.return_value = items
    rec.recover_false_positive_failed_tasks.return_value = {"tasks_reset": 2}
    det.identify_incomplete_post_transcription_files.return_value = items
    rec.recover_incomplete_post_transcription_files.return_value = {"total_tasks_dispatched": 4}
    return det, rec


def _run(det, rec, *, oom_enabled=True):
    with (
        patch.object(recovery, "session_scope", _scope),
        patch.object(
            recovery, "resolve_superseded_task_rows", lambda db: {"found": 3, "resolved": 2}
        ),
        patch.object(recovery, "task_detection_service", det),
        patch.object(recovery, "task_recovery_service", rec),
        patch.object(recovery, "_check_opensearch_health", lambda summary: summary.update(os=1)),
        patch.object(recovery.task_recovery_config, "OOM_RETRY_ENABLED", oom_enabled),
        patch(
            "app.services.youtube_policy.get_youtube_policy",
            return_value=MagicMock(recovery_batch_size=3),
        ),
        patch.object(task_lock.task_lock_manager, "acquire_lock", _lock_held),
    ):
        return recovery.periodic_health_check_task()


def test_every_step_lands_in_the_summary():
    det, rec = _services()
    summary = _run(det, rec)

    assert summary["superseded_task_rows_found"] == 3
    assert summary["superseded_task_rows_resolved"] == 2
    assert (summary["stuck_tasks_found"], summary["stuck_tasks_recovered"]) == (2, 1)
    assert (summary["inconsistent_files_found"], summary["inconsistent_files_fixed"]) == (2, 2)
    assert summary["stuck_files_without_celery_recovered"] == 1
    assert summary["stuck_downloading_recovered"] == 2
    assert (summary["oom_files_found"], summary["oom_files_retried"]) == (2, 3)
    assert summary["oom_files_exhausted"] == 1
    assert summary["retriable_errors_found"] == 3
    assert summary["retriable_youtube_found"] == 1
    assert summary["retriable_errors_retried"] == 2
    assert summary["stuck_pending_downloads_marked_error"] == 2
    assert summary["stuck_llm_tasks_marked_failed"] == 1
    assert summary["false_positive_tasks_reset"] == 2
    assert summary["post_transcription_tasks_dispatched"] == 4
    assert summary["os"] == 1  # the OpenSearch check always runs last
    assert "error" not in summary


def test_oom_step_is_skipped_when_retry_is_disabled():
    det, rec = _services()
    summary = _run(det, rec, oom_enabled=False)

    det.identify_oom_error_files.assert_not_called()
    assert summary["oom_files_found"] == 0


def test_empty_detections_call_no_recovery_and_retry_count_stays_zero():
    det, rec = _services(found=0)
    det.identify_retriable_error_files_split.return_value = ([], [])
    summary = _run(det, rec)

    rec.recover_stuck_files_without_celery_tasks.assert_not_called()
    rec.recover_retriable_error_files.assert_not_called()
    rec.recover_incomplete_post_transcription_files.assert_not_called()
    assert summary["retriable_errors_retried"] == 0


def test_a_failing_step_is_recorded_and_the_opensearch_check_still_runs():
    det, rec = _services()
    det.identify_stuck_llm_tasks.side_effect = RuntimeError("detector down")
    summary = _run(det, rec)

    assert summary["error"] == "detector down"
    assert summary["os"] == 1
    assert summary["stuck_llm_tasks_found"] == 0
    assert summary["post_transcription_tasks_dispatched"] == 0
