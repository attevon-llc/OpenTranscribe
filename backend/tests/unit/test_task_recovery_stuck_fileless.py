"""A stuck task row with no media file (a ``privacy_purge``) is failed and reported recovered."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.models.media import Task
from app.services import task_recovery_stuck as mod


@pytest.mark.unit
def test_fileless_task_is_failed_and_recovery_succeeds_without_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = Task(id="t-1", task_type="privacy_purge", media_file_id=None)
    service = mod.TaskRecoveryStuckMixin()
    with (
        patch.object(mod, "update_task_status") as update,
        caplog.at_level(logging.ERROR, logger=mod.__name__),
    ):
        ok = service.recover_stuck_task(MagicMock(), task, requeue=False)
    assert ok is True
    assert update.call_args.kwargs["status"] == "failed"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
