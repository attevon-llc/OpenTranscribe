"""A legal hold must survive every deletion path (issue #664).

``purge_media_file`` is the canonical destroy — it removes the object from storage
and the row from Postgres, and nothing brings either back. Until this suite it
consulted ``legal_hold`` **nowhere**, and the hourly retention sweep built its
candidate set from ``status`` alone. A DMCA/litigation hold could therefore be
destroyed by a background task, which is the one failure mode legal hold exists
to prevent.

``status`` is not a durable stand-in for the flag either:
``tasks/transcription/storage.update_media_file_transcription_status`` writes
``status = COMPLETED`` and ``completed_at = now()`` unconditionally, with no
quarantine check. A file quarantined *while it was still transcribing* therefore
ends up ``legal_hold=True, is_quarantined=True, status='completed'`` with a
retention clock starting at the moment of that clobber —
:func:`test_a_hold_survives_a_mid_transcription_status_clobber` reproduces it
through the real writer.

Every test drives real rows through the savepoint-rolled-back ``db_session``;
the two purge tests use fabricated storage paths, so the object-storage and
OpenSearch legs of the destroy address nothing that exists.
"""

import datetime
import uuid

import pytest
from fastapi import HTTPException

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services import file_cleanup_service
from app.tasks import cleanup

#: The sweep window every test in this module measures against: a file whose
#: reference timestamp predates this is past its retention.
_RETENTION_DAYS = 30

#: How far back an "expired" file's ``completed_at`` is placed. Comfortably
#: outside :data:`_RETENTION_DAYS` so eligibility is never a boundary question.
_LONG_EXPIRED_DAYS = 400


def _make_file(
    db_session,
    owner,
    *,
    legal_hold: bool = False,
    is_quarantined: bool = False,
    status: str = FileStatus.COMPLETED.value,
    age_days: int = _LONG_EXPIRED_DAYS,
) -> MediaFile:
    """Persist one MediaFile that the retention sweep would otherwise expire.

    Args:
        db_session: The savepoint-isolated test session.
        owner: The ``User`` the file belongs to.
        legal_hold: Value for the source-of-truth legal-hold flag.
        is_quarantined: Value for the takedown/quarantine flag.
        status: Processing status to store.
        age_days: How many days ago the file completed and was uploaded.

    Returns:
        The persisted, refreshed ``MediaFile``.
    """
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=age_days)
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename=f"hold-guard-{uuid.uuid4().hex[:8]}.mp4",
        storage_path=f"media/hold-guard/{uuid.uuid4().hex}.mp4",
        content_type="video/mp4",
        file_size=1024,
        user_id=owner.id,
        status=status,
        is_public=False,
        upload_time=when,
        completed_at=when,
        legal_hold=legal_hold,
        is_quarantined=is_quarantined,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _sweep_candidate_ids(db_session) -> set[int]:
    """Run the retention sweep's SELECTION phase and return the file ids it chose.

    Calls ``_select_expired_files`` directly rather than the task: selection is a
    pure read, while the task's second phase destroys everything it selected.

    Args:
        db_session: The savepoint-isolated test session.

    Returns:
        The set of ``media_file.id`` values the sweep would hand to the purger.
    """
    from app.core.tenant_limits import resolve_retention_days

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=_RETENTION_DAYS)
    config = {"delete_error_files": False}
    expired = cleanup._select_expired_files(
        db_session, config, cutoff, cutoff, resolve_retention_days
    )
    return {file_id for file_id, _ in expired}


def test_the_sweep_still_selects_an_ordinary_expired_file(db_session, sample_user):
    """CONTROL: without it, a guard is indistinguishable from a broken query.

    Every other selection test in this module asserts an *absence*. An empty
    result set satisfies all of them, so this one proves the query still finds
    the files retention exists to delete.
    """
    ordinary = _make_file(db_session, sample_user)

    assert int(ordinary.id) in _sweep_candidate_ids(db_session)


def test_the_sweep_skips_a_file_under_legal_hold(db_session, sample_user):
    """A held file is otherwise fully eligible — completed and long past the window."""
    held = _make_file(db_session, sample_user, legal_hold=True)
    ordinary = _make_file(db_session, sample_user)

    candidates = _sweep_candidate_ids(db_session)

    assert int(held.id) not in candidates
    # Paired with the control in the same query, so an empty result cannot pass.
    assert int(ordinary.id) in candidates


def test_the_sweep_skips_a_quarantined_file(db_session, sample_user):
    """A file under review for a takedown must not be destroyed by a beat task.

    Quarantine is deliberately independent of ``status`` (the row keeps its
    processing state so a release restores it verbatim), so nothing else in the
    candidate query excludes it.
    """
    quarantined = _make_file(db_session, sample_user, is_quarantined=True)
    ordinary = _make_file(db_session, sample_user)

    candidates = _sweep_candidate_ids(db_session)

    assert int(quarantined.id) not in candidates
    assert int(ordinary.id) in candidates


def test_a_hold_survives_a_mid_transcription_status_clobber(db_session, sample_user):
    """Issue #664's exact scenario, reproduced through the real status writer.

    A file is quarantined and held while it is still PROCESSING. The transcription
    pipeline then finishes and calls the real
    ``update_media_file_transcription_status``, which writes ``COMPLETED`` and a
    fresh ``completed_at`` with no quarantine check — so the only thing that had
    been keeping the row out of the candidate set is gone, and the retention clock
    now starts at the clobber. The file must still survive the sweep.
    """
    from app.tasks.transcription.storage import update_media_file_transcription_status

    held = _make_file(
        db_session,
        sample_user,
        legal_hold=True,
        is_quarantined=True,
        status=FileStatus.PROCESSING.value,
    )
    file_id = int(held.id)

    update_media_file_transcription_status(db_session, file_id, [{"end": 12.0}], "en")
    db_session.refresh(held)

    # The clobber really did happen — otherwise this test proves nothing about it.
    assert held.status == FileStatus.COMPLETED.value
    assert bool(held.legal_hold) is True

    # Age the clobbered timestamp past the window, i.e. the sweep that runs
    # _RETENTION_DAYS after the clobber rather than the one that runs today.
    held.completed_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=_LONG_EXPIRED_DAYS
    )
    db_session.commit()

    assert file_id not in _sweep_candidate_ids(db_session)


def test_purge_media_file_refuses_a_file_under_legal_hold(db_session, sample_user):
    """The fail-closed backstop every caller inherits.

    The sweep predicate protects one caller. This is the guard that protects the
    interactive delete, the orphan sweep, and anything added later — and it must
    report the refusal, never a quiet no-op that reads as success.
    """
    held = _make_file(db_session, sample_user, legal_hold=True)
    file_id = int(held.id)

    result = file_cleanup_service.purge_media_file(db_session, held)

    assert result["deleted"] is False
    assert result["refused_legal_hold"] is True
    assert "legal hold" in result["error"]
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is not None


def test_purge_media_file_still_destroys_an_ordinary_file(db_session, sample_user):
    """CONTROL: the guard must not have broken the destroy it guards."""
    ordinary = _make_file(db_session, sample_user)
    file_id = int(ordinary.id)

    result = file_cleanup_service.purge_media_file(db_session, ordinary)

    assert result["deleted"] is True
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is None


def test_purge_media_file_still_destroys_a_quarantined_file_with_no_hold(db_session, sample_user):
    """The in-function guard is keyed on the HOLD, not on quarantine — deliberately.

    ``quarantine_file`` can be called with ``legal_hold=False``, and taking an
    AUP-violating upload down and then deleting it is a real admin workflow. The
    unattended sweep still skips quarantined files (it must never destroy anything
    under review), but an admin acting deliberately is not blocked. Without this
    test, widening the guard to ``is_quarantined`` would look like an improvement.
    """
    quarantined = _make_file(db_session, sample_user, is_quarantined=True)
    file_id = int(quarantined.id)

    result = file_cleanup_service.purge_media_file(db_session, quarantined)

    assert result["deleted"] is True
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is None


def test_the_interactive_delete_reports_a_hold_as_a_conflict(db_session, sample_user):
    """A refusal is not a server fault, and the admin needs to know which it was.

    ``crud.delete_media_file`` turns any failed purge into a 500. A legal hold is
    a deliberate, actionable refusal — the admin must release the hold first — so
    it surfaces as a 409 naming the cause instead of an opaque internal error.
    """
    from app.api.endpoints.files import crud

    held = _make_file(db_session, sample_user, legal_hold=True)
    file_id = int(held.id)

    with pytest.raises(HTTPException) as excinfo:
        crud.delete_media_file(db_session, str(held.uuid), sample_user, force=True)

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"] == "FILE_UNDER_LEGAL_HOLD"
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is not None
