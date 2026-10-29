"""A failed matching pass must be reported by ``process_speaker_update_background``.

Zero counts are also the real "nothing matched" answer, so the task has to say that
the pass failed rather than let an outage read as success (fail-open cleanup, FO-3).
Every seam but the labeling workflow is stubbed; no database is needed.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.exc import OperationalError

from app.api.endpoints import speakers as spk
from app.tasks import speaker_update_task as spt


@pytest.fixture
def task_env(monkeypatch):
    recorded: dict = {"ws": [], "cache": []}

    @contextmanager
    def _scope():
        yield object()

    monkeypatch.setattr(spt, "session_scope", _scope)
    monkeypatch.setattr(
        spt,
        "_load_update_plan",
        lambda *a, **k: {"display_name": "Ada", "profile_id": None, "profile_sync": None},
    )
    monkeypatch.setattr(
        spt,
        "_load_completion_notification",
        lambda _uuid: {"profile_uuid": None, "media_file_uuid": "file-uuid"},
    )
    monkeypatch.setattr(spk, "_handle_profile_embedding_updates", lambda *a, **k: None)
    monkeypatch.setattr(spk, "_update_opensearch_speaker_name", lambda *a, **k: None)
    monkeypatch.setattr(spk, "_push_speaker_profile_info", lambda *a, **k: None)
    monkeypatch.setattr(spk, "_clear_video_cache_for_speaker", recorded["cache"].append)
    monkeypatch.setattr(
        "app.utils.websocket_notify.send_ws_event_for_file",
        lambda user_id, event, data, file_uuid: recorded["ws"].append((event, data)),
    )
    return recorded


def _run():
    return spt.process_speaker_update_background.apply(
        kwargs={
            "speaker_uuid": "spk-uuid",
            "user_id": 1,
            "display_name": "Ada",
            "speaker_id": 7,
            "old_profile_id": None,
            "new_profile_id": None,
            "was_auto_labeled": False,
            "display_name_changed": True,
            "media_file_id": 42,
        }
    ).get()


def test_failed_matching_is_reported_but_later_steps_still_run(task_env, monkeypatch):
    def _failing_workflow(db, speaker_id, display_name, user_id=None):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(spk, "_handle_speaker_labeling_workflow", _failing_workflow)

    result = _run()

    assert result["status"] == "partial", result
    assert result["matching_failed"] is True
    assert "retroactive_matching" in result["cleanup_incomplete_stages"]
    # The label was saved, so the subtitle cache is still cleared and the user told.
    assert task_env["cache"] == [42]
    event, data = task_env["ws"][0]
    assert event == "speaker_processing_complete"
    assert data["matching_failed"] is True


def test_genuinely_empty_matching_is_still_success(task_env, monkeypatch):
    monkeypatch.setattr(
        spk,
        "_handle_speaker_labeling_workflow",
        lambda db, speaker_id, display_name, user_id=None: {
            "auto_applied_count": 0,
            "suggested_count": 0,
        },
    )

    result = _run()

    assert result["status"] == "success", result
    assert result["matching_failed"] is False
    assert result["cleanup_incomplete_stages"] == []
    assert task_env["ws"][0][1]["matching_failed"] is False
