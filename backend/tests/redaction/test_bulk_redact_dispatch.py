"""Pressing "Redact" must not be able to withhold a healthy transcript forever.

``test_lazy_redaction_dispatch.py`` covers the *first read* of a never-scanned file.
This file covers the other writer of ``redaction_status = pending``: the bulk /
selective **Redact** action, ``endpoints/files/management._handle_redact_action``.

The two are the same protocol — claim ``pending``, publish the scan, and withdraw the
claim if the publish never landed — and only the first one had it. The second
committed ``pending`` and then called ``redaction_detect_task.delay(...)`` unguarded,
so an admin bulk-redacting 50 files across a Redis restart left every file the publish
raised on at ``pending`` with nothing queued.

**That state is not recoverable by anything the product does on its own**, which is
what makes it worse here than on a legacy unscanned file:

* ``crud._redaction_pending`` re-dispatches only from ``NULL``. ``pending`` is not
  ``NULL``, so reloading the page does nothing.
* ``RedactionService._mark_redaction_stale`` returns early when the status is already
  ``pending``/``processing``, so editing a segment does not re-queue it either.
* ``redaction_reindex_all_task`` defaults to ``only_stale=True``, which selects on
  ``redaction_model_version != REDACTION_MODEL_VERSION | IS NULL``. A file that *had*
  been scanned still carries the current version, so the admin backfill **skips** it.

and the status is shared by all four surfaces that read
``export_policy.export_masking_is_pending`` — the transcript page, the single-file
subtitle download, the bulk-export ZIP, and the burned-in-subtitle render. So one
click on a healthy file removes its transcript from the product until a superuser
re-runs the reindex with ``only_stale=false``.

Nothing in this file relaxes the withholding rule itself. ``NULL``/``pending`` are
withheld deliberately (all four consumers mask from *cached* spans, so letting an
unexamined file through exports raw text — issue #85); the repair is that the claim is
withdrawn when no scan is coming, never that the gate is loosened.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812


@pytest.fixture(autouse=True)
def celery_is_live(monkeypatch):
    """The suite-wide ``SKIP_CELERY=True`` would take the test-mode branch.

    ``tests/conftest.py`` sets it before any app import so that no test reaches a real
    broker. That is right, and it also means the *dispatch* half of this function is
    unreachable by default — the branch every assertion here is about. The ``broker``
    fixture below is what keeps the publish from leaving the process.
    """
    monkeypatch.setenv("SKIP_CELERY", "False")


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def redacting_user(db_session, normal_user):
    """An owner who turned redaction on — the only case the withholding gate applies to."""
    _set_prefs(db_session, normal_user, redaction_enabled="true")
    return normal_user


def _completed_file(db_session, owner, redaction_status: str | None):
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename=f"bulk-redact-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"bulk-redact-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language="en",
        status=FileStatus.COMPLETED,
        redaction_status=redaction_status,
    )
    db_session.add(media)
    db_session.flush()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            start_time=0.0,
            end_time=5.0,
            text="a transcript the owner can read right up until they press Redact",
        )
    )
    db_session.flush()
    return media


@pytest.fixture
def scanned_file(db_session, redacting_user):
    """A perfectly healthy file: scanned, readable, at the current model version.

    The starting state that makes this bug worse than the legacy one — the admin
    backfill's ``only_stale`` filter keys on ``redaction_model_version``, so a file
    holding the current version is *skipped* by the operator's only recovery tool.
    """
    media = _completed_file(db_session, redacting_user, C.REDACTION_STATUS_DONE)
    media.redaction_model_version = C.REDACTION_MODEL_VERSION
    db_session.flush()
    return media


@pytest.fixture
def never_scanned_file(db_session, redacting_user):
    """The legacy shape: completed, with a transcript, never examined."""
    return _completed_file(db_session, redacting_user, None)


@pytest.fixture
def broker(monkeypatch):
    """Stand in for ``redaction_detect_task.delay``; publishing can be made to fail.

    An unreachable broker is the only condition that produces the permanent
    withholding, and there is no way to reach it through a working stack.
    """
    from app.tasks import redaction_task

    class _Task:
        id = "test-task-id"

    class _Broker:
        def __init__(self) -> None:
            self.published: list[dict] = []
            self.reachable = True
            self.on_publish = None

        def delay(self, **kwargs):
            if self.on_publish is not None:
                self.on_publish()
            if not self.reachable:
                raise OSError("[Errno 111] Connection refused: redis://redis:6379//")
            self.published.append(kwargs)
            return _Task()

    stub = _Broker()
    monkeypatch.setattr(redaction_task.redaction_detect_task, "delay", stub.delay)
    return stub


def _withheld(db_session, user, media) -> bool:
    """The rule all four export/read surfaces share, asked about this file's status."""
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.export_policy import export_masking_is_pending

    cfg = resolve_effective_config(db_session, user.id)
    return export_masking_is_pending(cfg, getattr(media, "redaction_status", None))


def _redact(db_session, media):
    from app.api.endpoints.files.management import _handle_redact_action

    return _handle_redact_action(db_session, str(media.uuid), media.id)


def _redact_through_the_batch(db_session, user, media):
    """Drive the real bulk endpoint, not just the handler.

    ``bulk_file_action`` wraps each file in its own ``try/except``, so an unguarded
    publish that raises is reported as one failed file and the batch carries on. That
    is exactly why the stranded row was never noticed: the click *does* report an
    error, and the damage it leaves behind is silent, permanent, and invisible in the
    response. Going through the endpoint rather than the handler keeps that
    distinction in the test instead of in prose.
    """
    from app.api.deps_context import RequestContext
    from app.api.endpoints.files.management import BulkActionRequest
    from app.api.endpoints.files.management import bulk_file_action

    results = bulk_file_action(
        request=BulkActionRequest(file_uuids=[str(media.uuid)], action="redact"),
        db=db_session,
        current_user=user,
        ctx=RequestContext(user=user),
    )
    assert len(results) == 1, "one file in, one per-file result out"
    return results[0]


# --------------------------------------------------------------------- the control


def test_a_bulk_redact_that_reaches_the_broker_claims_pending_and_queues_the_scan(
    db_session, redacting_user, scanned_file, broker
):
    """With a reachable broker, ``pending`` is a transient state a worker will clear.

    The claim is written *before* the publish on purpose — it is what drives the UI's
    "Redacting…" state and what stops a second overlapping batch queueing a duplicate
    scan of the same file.
    """
    result = _redact(db_session, scanned_file)

    assert result.success is True
    assert result.error is None
    assert [call["file_id"] for call in broker.published] == [scanned_file.id]

    db_session.refresh(scanned_file)
    assert scanned_file.redaction_status == C.REDACTION_STATUS_PENDING


# ----------------------------------------------- the bug: a publish that never lands


def test_a_failed_publish_leaves_a_scanned_file_exactly_as_readable_as_before(
    db_session, redacting_user, scanned_file, broker
):
    """The click must not be able to take a healthy transcript away.

    Asserted as the exact expected VALUE rather than "not pending": ``failed`` or a
    half-written ``processing`` would satisfy a negated assertion while leaving the
    file just as unrecoverable. ``done`` is the only status that restores the file to
    what it was — its cached spans were never invalidated, because the re-scan that
    would have replaced them never started.
    """
    broker.reachable = False

    _redact_through_the_batch(db_session, redacting_user, scanned_file)

    db_session.refresh(scanned_file)
    assert scanned_file.redaction_status == C.REDACTION_STATUS_DONE, (
        f"expected the file to be returned to 'done' — the status it held before the "
        f"click — but it is {scanned_file.redaction_status!r}. At 'pending' the row "
        "claims a scan is queued when the publish raised and none is; no read, no "
        "segment edit and no `only_stale` reindex will ever move it off, so this "
        "transcript is withheld from the transcript page, the subtitle download, the "
        "bulk-export ZIP and the burned-in render for the life of the row."
    )
    assert _withheld(db_session, redacting_user, scanned_file) is False


def test_a_failed_publish_leaves_a_never_scanned_file_re_dispatchable(
    db_session, redacting_user, never_scanned_file, broker
):
    """Restoring the *previous* status, not some fixed value, is what buys recovery.

    ``NULL`` is the one status ``crud._redaction_pending`` dispatches from, so a file
    that was unscanned before the click has to be unscanned after it — then the next
    ordinary read queues the scan for free.
    """
    broker.reachable = False

    _redact_through_the_batch(db_session, redacting_user, never_scanned_file)

    db_session.refresh(never_scanned_file)
    assert never_scanned_file.redaction_status is None, (
        f"expected NULL — the only status the lazy dispatch re-fires from — but it is "
        f"{never_scanned_file.redaction_status!r}"
    )

    broker.reachable = True
    from app.api.endpoints.files.crud import _redaction_pending
    from app.services.redaction.config import resolve_effective_config

    cfg = resolve_effective_config(db_session, redacting_user.id)
    assert _redaction_pending(db_session, cfg, never_scanned_file) is True
    assert [call["file_id"] for call in broker.published] == [never_scanned_file.id], (
        "the retry the user gets for free — opening the file — must queue the scan"
    )


def test_the_batch_reports_the_failure_rather_than_claiming_the_scan_started(
    db_session, redacting_user, scanned_file, broker
):
    """A file the broker refused is a failed file, not a silent success.

    The old code's message said "Redaction started (task: …)" only when ``.delay()``
    returned; when it raised, the batch's generic handler reported
    ``UNEXPECTED_ERROR``. Either way the operator had no way to tell that the row had
    been left in a state nothing would repair.
    """
    broker.reachable = False

    result = _redact_through_the_batch(db_session, redacting_user, scanned_file)

    assert result.success is False
    assert result.error == "DISPATCH_FAILED"
    assert broker.published == []


def test_the_withdrawal_does_not_overwrite_a_status_another_writer_moved_on(
    db_session, redacting_user, scanned_file, broker
):
    """Undoing the claim is only correct while the claim is still the row's state.

    Between our commit of ``pending`` and the withdrawal, another session may
    legitimately advance the row — a worker a concurrent dispatch *did* queue writing
    ``processing``, or a re-scan queued by a segment edit. Writing our captured
    ``done`` back over that would resurrect cached spans that no longer cover the text
    and cancel, on paper, a scan that is genuinely running. The withdrawal is
    therefore a guarded ``UPDATE ... WHERE redaction_status = 'pending'``.

    Simulated at the row, which is the level the guard operates at: the concurrent
    write is issued as its own statement while the publish is in flight.
    """
    from sqlalchemy import update

    from app.models.media import MediaFile

    def another_writer_advances_the_row():
        db_session.execute(
            update(MediaFile)
            .where(MediaFile.id == scanned_file.id)
            .values(redaction_status=C.REDACTION_STATUS_PROCESSING)
        )
        db_session.commit()

    broker.reachable = False
    broker.on_publish = another_writer_advances_the_row

    _redact_through_the_batch(db_session, redacting_user, scanned_file)

    db_session.refresh(scanned_file)
    assert scanned_file.redaction_status == C.REDACTION_STATUS_PROCESSING, (
        "the withdrawal clobbered a status this call did not write; a worker that is "
        "actually scanning has had its progress overwritten with a stale terminal state"
    )


# ------------------------------------------- and only one copy of the protocol exists


def test_the_claim_and_withdraw_protocol_has_exactly_one_implementation():
    """Two copies of this recovery protocol is precisely how the bug shipped.

    ``crud._lazy_dispatch_redaction`` was fixed and ``management._handle_redact_action``
    — the identical claim-then-publish, twenty lines of a different module away — was
    not. This pins the shared helper as the only way ``management`` may touch either
    the status or the task, so a future re-fork fails here instead of in production.
    """
    import inspect

    from app.api.endpoints.files import management

    source = inspect.getsource(management)

    assert "redaction_detect_task" not in source, (
        "management.py publishes the redaction scan itself again — it must go through "
        "crud.claim_and_dispatch_redaction_scan, which owns the withdrawal on failure"
    )
    assert "REDACTION_STATUS_PENDING" not in source, (
        "management.py writes the pending claim itself again — the claim and its "
        "withdrawal are one protocol and must not be separated"
    )
    assert "claim_and_dispatch_redaction_scan" in source
