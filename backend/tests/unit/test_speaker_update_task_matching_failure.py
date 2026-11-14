"""A failed matching pass must be reported by ``process_speaker_update_background``.

Zero counts are also the real "nothing matched" answer, so the task has to say that
the pass failed rather than let an outage read as success (fail-open cleanup, FO-3).
A speaker merged away while the task ran is the one failure that is not a failure: the
row is gone, so there is nothing to match, and the task says so at INFO.
Every seam but the labeling workflow is stubbed; no database is needed.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import cast

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.endpoints import speakers_profile_workflow as spk
from app.models.speaker import Speaker
from app.tasks import speaker_update_task as spt


@pytest.fixture
def task_env(monkeypatch):
    recorded: dict = {"ws": [], "cache": []}

    @contextmanager
    def _scope():
        yield object()

    monkeypatch.setattr(spt, "session_scope", _scope)
    recorded["removed"] = False
    monkeypatch.setattr(spt, "_speaker_was_removed", lambda speaker_id: recorded["removed"])
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


def test_incomplete_suggestion_sync_reports_the_real_counts(task_env, monkeypatch):
    from app.services.speaker_match_errors import SuggestionSyncIncompleteError

    def _partial_workflow(db, speaker_id, display_name, user_id=None):
        raise SuggestionSyncIncompleteError(3, 2)

    monkeypatch.setattr(spk, "_handle_speaker_labeling_workflow", _partial_workflow)

    result = _run()

    assert result["status"] == "partial", result
    assert result["matching_failed"] is True
    assert (result["auto_applied_count"], result["suggested_count"]) == (3, 2)
    data = task_env["ws"][0][1]
    assert (data["auto_applied_count"], data["suggested_count"]) == (3, 2)
    assert data["matching_failed"] is True


def test_a_speaker_merged_away_meanwhile_is_not_a_matching_failure(task_env, monkeypatch, caplog):
    """The load test's speaker curator renames a temporary speaker, then merges it at once."""
    from sqlalchemy.orm.exc import StaleDataError

    def _speaker_gone(db, speaker_id, display_name, user_id=None):
        raise StaleDataError("UPDATE on table 'speaker' expected to update 1 row(s); 0 matched")

    monkeypatch.setattr(spk, "_handle_speaker_labeling_workflow", _speaker_gone)
    task_env["removed"] = True

    with caplog.at_level("INFO", logger=spt.logger.name):
        result = _run()

    assert result["status"] == "success", result
    assert result["matching_failed"] is False
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("merged or deleted while its label" in r.getMessage() for r in caplog.records)


def test_the_same_error_on_a_speaker_that_still_exists_is_a_failure(task_env, monkeypatch):
    """CONTROL: the error type alone never excuses a failure; the row must really be gone."""
    from sqlalchemy.orm.exc import StaleDataError

    def _speaker_gone(db, speaker_id, display_name, user_id=None):
        raise StaleDataError("UPDATE on table 'speaker' expected to update 1 row(s); 0 matched")

    monkeypatch.setattr(spk, "_handle_speaker_labeling_workflow", _speaker_gone)

    result = _run()

    assert result["status"] == "partial", result
    assert result["matching_failed"] is True


def test_the_profile_step_leaves_reporting_to_the_task(monkeypatch, caplog):
    """One report per failure: the step raises, the task (above) decides and logs once."""
    from types import SimpleNamespace

    from sqlalchemy.orm.exc import StaleDataError

    def _gone(db, media_file_id):
        raise StaleDataError("UPDATE on table 'speaker' expected to update 1 row(s); 0 matched")

    monkeypatch.setattr(spk.speaker_lookups, "file_organization_id", _gone)
    speaker = cast(Speaker, SimpleNamespace(id=7, media_file_id=42, user_id=1, profile_id=None))

    with caplog.at_level("DEBUG", logger=spk.logger.name), pytest.raises(StaleDataError):
        spk.auto_create_or_assign_profile(speaker, "Ada", db=cast(Session, None))

    assert [r for r in caplog.records if r.name == spk.logger.name] == []
