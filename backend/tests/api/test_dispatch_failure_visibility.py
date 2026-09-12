"""Three routes in ``endpoints/tasks.py`` used to schedule the real transcription
re-dispatch via ``background_tasks.add_task`` and then return immediately. A FastAPI
response is sent BEFORE a background task runs, so a response built that way can
never reflect the background task's actual outcome — a broker-publish failure was
invisible at the API layer no matter what happened underneath. All three
(``POST /tasks/recover-stuck-tasks``, ``POST /tasks/system/recover-task/{id}``,
``POST /tasks/retry/{file_uuid}``) now dispatch INLINE instead (issue #906), so this
file drives a real (unmocked) ``dispatch_transcription_pipeline`` through a stubbed
Celery ``chain`` and asserts the failure actually reaches the HTTP response and the
DB row — not just the log.

As amended by #914: what must reach the response is that the dispatch FAILED, plus the
class of failure — never the caught exception's raw text, which on the broker path can
name the transport URL and its credentials. The raw text is asserted to be present in
the log and on the ``MediaFile`` row instead, so these tests still prove the failure is
diagnosable; they just pin where each half of it is allowed to appear.
"""

from __future__ import annotations

import contextlib
import inspect
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import status
from kombu.exceptions import OperationalError as BrokerOperationalError

import app.api.endpoints.tasks as tasks_endpoints
import app.tasks.transcription.dispatch as dispatch_module
from app.core.config import settings
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel

RECOVER_ALL = "/api/tasks/recover-stuck-tasks"
RECOVER_TASK = "/api/tasks/system/recover-task"
RETRY = "/api/tasks/retry"


def _make_media_file(db, owner, *, file_status: str = "error", **overrides) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "dispatch_visibility_test.wav",
        "title": "dispatch_visibility_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _stuck_transcription_task(db, user, media_file) -> TaskModel:
    """Stale AND past its duration ceiling — what ``identify_stuck_tasks`` selects."""
    now = datetime.now(UTC)
    task = TaskModel(
        id=f"pytest-dispatch-vis-{uuid_pkg.uuid4().hex[:10]}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        progress=0.4,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
    )
    db.add(task)
    db.commit()
    return task


@contextlib.contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def dispatch_uses_test_session(monkeypatch, db_session):
    """Point ``dispatch.py``'s OWN ``session_scope`` at the test's savepointed session.

    ``dispatch_transcription_pipeline`` runs unmocked in every test in this file — it
    opens its own session via ``session_scope()``, which by default is a brand new
    connection outside the test's savepoint and would not see rows the test just
    created. Same seam ``tests/unit/test_dispatch.py`` patches, for the same reason.
    """
    monkeypatch.setattr(dispatch_module, "session_scope", lambda: _yield_session(db_session))
    return db_session


@pytest.fixture
def failing_dispatch(dispatch_uses_test_session, monkeypatch):
    """The Celery publish itself fails — the shape #906 is about."""

    def _raise(**kwargs):
        raise BrokerOperationalError("broker down")

    chain_stub = SimpleNamespace(apply_async=_raise)
    monkeypatch.setattr(dispatch_module, "chain", lambda *a, **k: chain_stub)
    return chain_stub


@pytest.fixture
def working_dispatch(dispatch_uses_test_session, monkeypatch):
    """CONTROL sibling of ``failing_dispatch``: the publish succeeds."""
    chain_stub = SimpleNamespace(apply_async=lambda **kwargs: SimpleNamespace(id="ok"))
    monkeypatch.setattr(dispatch_module, "chain", lambda *a, **k: chain_stub)
    return chain_stub


@pytest.fixture
def lite_mode_no_asr_provider(monkeypatch):
    """A deployment structurally refusing local ASR (#865) — an ASRConfigurationError,
    not a broker failure, so no ``chain`` stub is needed: dispatch never reaches it."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    monkeypatch.delenv("ASR_PROVIDER", raising=False)


# ---------------------------------------------------------------------------
# POST /tasks/recover-stuck-tasks
# ---------------------------------------------------------------------------
def test_recover_stuck_tasks_reports_a_failed_requeue(
    client, db_session, admin_token_headers, normal_user, failing_dispatch
):
    """Not an exact-count assertion: ``identify_stuck_tasks`` scans deployment-wide, so
    a busy dev stack may hold other genuinely stuck tasks. What must hold regardless is
    that OUR file's failure is reported and recorded — assert presence, not totals."""
    media_file = _make_media_file(db_session, normal_user, file_status="processing")
    _stuck_transcription_task(db_session, normal_user, media_file)

    response = client.post(RECOVER_ALL, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is False
    assert body["retried"] == 0  # every dispatch in this test uses the failing stub
    failures_by_uuid = {f["file_uuid"]: f["error"] for f in body["retry_failures"]}
    # The FAILURE is reported per-file (that is what #906 fixed) -- but as the class of
    # failure, not the broker's raw text (#914). See the sibling recover-task test.
    assert str(media_file.uuid) in failures_by_uuid
    assert failures_by_uuid[str(media_file.uuid)] == "Re-dispatch failed (OperationalError)"
    assert "broker down" not in response.text

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR
    # The DB row is a different plane from the HTTP response and still holds the cause.
    assert "broker down" in (media_file.last_error_message or "")


def test_recover_stuck_tasks_still_reports_success_when_the_requeue_works(
    client, db_session, admin_token_headers, normal_user, working_dispatch
):
    """CONTROL: same fixture shape, the requeue succeeds."""
    media_file = _make_media_file(db_session, normal_user, file_status="processing")
    _stuck_transcription_task(db_session, normal_user, media_file)

    response = client.post(RECOVER_ALL, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is True
    assert body["retry_failures"] == []
    assert body["retried"] >= 1

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.PROCESSING


# ---------------------------------------------------------------------------
# POST /tasks/system/recover-task/{task_id}
# ---------------------------------------------------------------------------
def test_recover_task_reports_that_dispatch_failed_without_echoing_the_error(
    client, db_session, admin_token_headers, normal_user, failing_dispatch, caplog
):
    """Renamed from ``..._reports_the_dispatch_error``: the endpoint deliberately no
    longer reports the dispatch *error text* (#914), and a name asserting the old
    contract is misleading even with an updated body.

    What #906 bought is still pinned here and is the point of the test: the failure is
    visible AT THE API LAYER at all — ``retry_scheduled`` False and a non-null
    ``dispatch_error`` — rather than a 200 that says nothing because the dispatch ran in
    a background task after the response was sent. What #914 changed is only that the
    reported string is the class of failure; the broker's raw text stays in the log and
    on the DB row.
    """
    media_file = _make_media_file(db_session, normal_user, file_status="processing")
    task = _stuck_transcription_task(db_session, normal_user, media_file)

    with caplog.at_level("ERROR", logger="app.api.endpoints.tasks"):
        response = client.post(f"{RECOVER_TASK}/{task.id}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is False
    assert body["retry_scheduled"] is False
    assert body["dispatch_error"] == "Re-dispatch failed (OperationalError)"
    # Nowhere in the response body, not just not in dispatch_error.
    assert "broker down" not in response.text
    # ...but the admin can still diagnose it: logger.exception leaves a traceback.
    assert "broker down" in caplog.text

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR
    assert "broker down" in (media_file.last_error_message or "")


def test_recover_task_returns_503_for_a_deliberate_asr_refusal(
    client,
    db_session,
    admin_token_headers,
    normal_user,
    dispatch_uses_test_session,
    lite_mode_no_asr_provider,
):
    media_file = _make_media_file(db_session, normal_user, file_status="processing")
    task = _stuck_transcription_task(db_session, normal_user, media_file)

    response = client.post(f"{RECOVER_TASK}/{task.id}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR
    assert "lite" in (media_file.last_error_message or "").lower()


# ---------------------------------------------------------------------------
# POST /tasks/retry/{file_uuid}
# ---------------------------------------------------------------------------
def test_tasks_retry_surfaces_a_dispatch_failure_instead_of_200(
    client, db_session, user_token_headers, normal_user, failing_dispatch, monkeypatch
):
    monkeypatch.setenv("SKIP_CELERY", "False")
    media_file = _make_media_file(db_session, normal_user, file_status="error", retry_count=0)

    response = client.post(f"{RETRY}/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR
    assert "broker down" in (media_file.last_error_message or "")


def test_tasks_retry_returns_503_for_a_deliberate_asr_refusal(
    client,
    db_session,
    user_token_headers,
    normal_user,
    dispatch_uses_test_session,
    lite_mode_no_asr_provider,
    monkeypatch,
):
    monkeypatch.setenv("SKIP_CELERY", "False")
    media_file = _make_media_file(db_session, normal_user, file_status="error", retry_count=0)

    response = client.post(f"{RETRY}/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR


# ---------------------------------------------------------------------------
# Structural: none of the three routes take a BackgroundTasks parameter any more.
# ---------------------------------------------------------------------------
def test_none_of_the_three_routes_schedule_a_background_task_any_more():
    from fastapi import BackgroundTasks

    handlers = [
        tasks_endpoints.recover_all_stuck_tasks,
        tasks_endpoints.recover_task,
        tasks_endpoints.retry_file_processing,
    ]
    for handler in handlers:
        annotations = [p.annotation for p in inspect.signature(handler).parameters.values()]
        assert BackgroundTasks not in annotations, (
            f"{handler.__name__} still declares a BackgroundTasks parameter"
        )
