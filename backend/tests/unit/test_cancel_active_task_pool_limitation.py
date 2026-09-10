"""``cancel_active_task``'s ``revoke(terminate=True)`` is a no-op under the
``--pool=threads`` every GPU queue (and the redaction queue) runs by default
(issue #823).

Celery's ``terminate=True`` sends a signal to the pool's worker to kill the
running task. CPython cannot deliver a signal to an arbitrary thread, so under
``--pool=threads`` the call has no effect on the in-flight work -- it keeps
running and consuming GPU/VRAM in the background -- while this function still
optimistically flips the DB row to ``CANCELLED`` in the same call. A user who
cancels believes the job stopped; it has not.

The real fix is a cooperative-abort checkpoint the task itself reads at safe
boundaries -- exactly what issue #809 is building for the worker-shutdown
(SIGTERM) trigger. #823 is a different trigger (user-cancel) hitting the same
underlying limitation, and per #823's own body: "coordinate rather than build
two abort mechanisms." #809 is open and unimplemented (no checkpoint exists
anywhere in the transcription hot path today), so building the real fix here
would mean inventing #809's mechanism ad hoc, with its own unresolved design
question (celery.exceptions.Reject(requeue=True) behaviour on the Redis
transport, called out in #809 as needing empirical verification before it can
ship).

What IS in scope for a mechanical fix, and what this file pins:
  1. The optimistic-CANCELLED behavior is UNCHANGED (still a characterization,
     not a regression -- there is no honest alternative without #809 built:
     the file cannot be left "processing" forever, since #809's checkpoint
     does not exist to eventually flip it).
  2. `revoke()` is still called, with `terminate=True`, on every call
     regardless of which pool the task actually runs on -- pinned so a future
     change does not silently drop it for the tasks it DOES work for (a
     prefork worker, e.g. cpu-processor/cloud-asr, is genuinely terminated by
     this call).
  3. The log message is HONEST: it no longer claims unconditional success --
     it says the DB was updated and that termination is best-effort, with a
     cross-reference to #823/#809 so nobody reads the old "Successfully
     cancelled" wording and assumes the GPU work actually stopped.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import UTC
from datetime import datetime

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.utils.task_utils import cancel_active_task


def _mk_processing_file_with_task(db_session, user) -> tuple[MediaFile, Task]:
    media_file = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename="cancel_target.mp3",
        storage_path=f"user_{user.id}/cancel_target.mp3",
        file_size=1024,
        content_type="audio/mpeg",
        status=FileStatus.PROCESSING,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    task_id = str(uuid_module.uuid4())
    task = Task(
        id=task_id,
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        created_at=datetime.now(UTC),
    )
    db_session.add(task)
    media_file.active_task_id = task_id
    db_session.commit()
    db_session.refresh(media_file)
    return media_file, task


def test_revoke_is_called_with_terminate_true_regardless_of_pool(
    db_session, normal_user, monkeypatch
):
    """Pinned so a future change does not silently drop this for the tasks it
    DOES actually work for (a prefork worker genuinely honors terminate=True)."""
    media_file, task = _mk_processing_file_with_task(db_session, normal_user)

    calls: list[tuple[str, bool]] = []

    def _fake_revoke(task_id, terminate=False, **kwargs):
        calls.append((task_id, terminate))

    from app.core.celery import celery_app

    monkeypatch.setattr(celery_app.control, "revoke", _fake_revoke)

    result = cancel_active_task(db_session, media_file.id)

    assert result is True
    assert calls == [(task.id, True)]


def test_status_is_optimistically_cancelled_even_though_termination_is_not_confirmed(
    db_session, normal_user, monkeypatch
):
    """Characterization of the KNOWN LIMITATION, not an endorsement: replace this
    assertion once #809's cooperative-abort checkpoint lands and this function
    can condition the status flip on confirmed termination instead."""
    media_file, _task = _mk_processing_file_with_task(db_session, normal_user)

    from app.core.celery import celery_app

    monkeypatch.setattr(celery_app.control, "revoke", lambda *a, **k: None)

    cancel_active_task(db_session, media_file.id)

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.CANCELLED
    assert media_file.active_task_id is None


def test_cancellation_log_message_does_not_overclaim_confirmed_termination(
    db_session, normal_user, monkeypatch, caplog
):
    """The pre-#823 wording was "Successfully cancelled task for file {id}" --
    read by an operator as "the GPU work stopped", which is false under
    --pool=threads. The message must acknowledge the limitation."""
    import logging

    media_file, _task = _mk_processing_file_with_task(db_session, normal_user)

    from app.core.celery import celery_app

    monkeypatch.setattr(celery_app.control, "revoke", lambda *a, **k: None)

    with caplog.at_level(logging.INFO, logger="app.utils.task_utils"):
        cancel_active_task(db_session, media_file.id)

    messages = " ".join(r.message for r in caplog.records)
    assert "best-effort" in messages.lower() or "not guaranteed" in messages.lower(), (
        f"cancellation log message does not acknowledge the termination is "
        f"unconfirmed -- messages were: {messages!r}"
    )
