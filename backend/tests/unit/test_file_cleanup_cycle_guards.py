"""``FileCleanupService``'s selection and recovery guards: what the unattended sweeps may destroy.

A HEAD-vs-split mutation comparison of ``services/file_cleanup_service.py`` (repo cleanup,
wave b10) found the predicates of ``cleanup.deep_cleanup``'s force delete and of the
recovery cycle pinned by nothing: the one force-delete suite seeds a single eligible orphan,
so dropping the ``force_delete_eligible`` filter, widening the status filter to every file,
or counting a legal-hold REFUSAL as a deletion all left it green, as did every change to the
recovery-attempt limit and the old-orphan threshold.

Storage and OpenSearch are stubbed at the purge's ``_purge_external_copies`` seam; the rows,
the hold check and the row delete are real. ``force_cleanup_orphaned_files`` sweeps the
whole deployment, so every other eligible row is un-flagged first (undone with the savepoint)
and this module shares the force-delete suite's xdist group.
"""

import datetime
import uuid as uuid_pkg

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.services import file_cleanup_cycle
from app.services import file_cleanup_service as fcs

pytestmark = pytest.mark.xdist_group("force_cleanup_orphaned")


@pytest.fixture
def sweep_scope(db_session, monkeypatch):
    """Only this test's rows are eligible, and the purge touches nothing outside Postgres."""
    monkeypatch.setattr(fcs, "_purge_external_copies", lambda plan: [])
    monkeypatch.setattr(fcs, "_refresh_surviving_centroids", lambda db, owner_id, plan: [])
    db_session.query(MediaFile).filter(MediaFile.force_delete_eligible.is_(True)).update(
        {"force_delete_eligible": False}, synchronize_session=False
    )


def _file(db_session, owner, **fields) -> int:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename=f"cycle-{uuid_pkg.uuid4().hex[:8]}.mp4",
        content_type="video/mp4",
        file_size=2048,
        storage_path=f"cycle-guard-test/{uuid_pkg.uuid4().hex}",
        **fields,
    )
    db_session.add(media_file)
    db_session.flush()
    return int(media_file.id)


def _exists(db_session, file_id: int) -> bool:
    db_session.expire_all()
    return db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is not None


def test_force_cleanup_deletes_only_eligible_orphaned_or_errored_files(
    db_session, normal_user, sweep_scope
):
    orphan = _file(db_session, normal_user, status=FileStatus.ORPHANED, force_delete_eligible=True)
    errored = _file(db_session, normal_user, status=FileStatus.ERROR, force_delete_eligible=True)
    not_flagged = _file(
        db_session, normal_user, status=FileStatus.ORPHANED, force_delete_eligible=False
    )
    completed = _file(
        db_session, normal_user, status=FileStatus.COMPLETED, force_delete_eligible=True
    )

    results = file_cleanup_cycle.cleanup_service.force_cleanup_orphaned_files(db_session)

    assert results["eligible_for_deletion"] == 2, results
    assert results["successfully_deleted"] == 2, results
    assert not _exists(db_session, orphan)
    assert not _exists(db_session, errored)
    assert _exists(db_session, not_flagged)
    assert _exists(db_session, completed)


def test_a_held_orphan_is_reported_as_an_error_not_a_deletion(db_session, normal_user, sweep_scope):
    held = _file(
        db_session,
        normal_user,
        status=FileStatus.ORPHANED,
        force_delete_eligible=True,
        legal_hold=True,
    )

    results = file_cleanup_cycle.cleanup_service.force_cleanup_orphaned_files(db_session)

    assert results["successfully_deleted"] == 0, results
    assert len(results["deletion_errors"]) == 1, results
    assert results["files_processed"][0]["status"] == "error"
    assert _exists(db_session, held)


def test_force_cleanup_cancels_an_attached_task_first(
    db_session, normal_user, sweep_scope, monkeypatch
):
    attached = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        force_delete_eligible=True,
        active_task_id="abandoned-task",
    )
    cancelled: list[int] = []
    monkeypatch.setattr(
        "app.utils.task_cancel.cancel_active_task", lambda db, file_id: cancelled.append(file_id)
    )

    results = file_cleanup_cycle.cleanup_service.force_cleanup_orphaned_files(db_session)

    assert results["successfully_deleted"] == 1, results
    assert cancelled == [attached]


@pytest.mark.parametrize(("attempts", "recovered"), [(2, True), (3, False)])
def test_recovery_stops_at_the_attempt_limit(
    db_session, normal_user, monkeypatch, attempts, recovered
):
    """Below the limit the file is retried; AT it, it is orphaned and made deletable."""
    file_id = _file(
        db_session, normal_user, status=FileStatus.PROCESSING, recovery_attempts=attempts
    )
    retried: list[int] = []

    def recover(db, fid):
        retried.append(fid)
        return True

    monkeypatch.setattr(file_cleanup_cycle, "recover_stuck_file", recover)
    service = file_cleanup_cycle.FileCleanupService()
    assert service.max_recovery_attempts == 3

    assert service._attempt_file_recovery(db_session, file_id) is recovered

    db_session.expire_all()
    row = db_session.get(MediaFile, file_id)
    if recovered:
        assert retried == [file_id]
        assert row.status == FileStatus.PROCESSING
        assert not row.force_delete_eligible
    else:
        assert retried == []
        assert row.status == FileStatus.ORPHANED
        assert row.force_delete_eligible is True


def test_only_long_orphaned_files_are_made_deletable(db_session, normal_user):
    now = datetime.datetime.now(datetime.UTC)
    service = file_cleanup_cycle.FileCleanupService()
    old = _file(
        db_session,
        normal_user,
        status=FileStatus.ORPHANED,
        force_delete_eligible=False,
        last_recovery_attempt=now - datetime.timedelta(hours=service.orphan_threshold_hours + 1),
    )
    recent = _file(
        db_session,
        normal_user,
        status=FileStatus.ORPHANED,
        force_delete_eligible=False,
        last_recovery_attempt=now - datetime.timedelta(hours=1),
    )

    assert service._handle_old_orphaned_files(db_session) >= 1

    db_session.expire_all()
    assert db_session.get(MediaFile, old).force_delete_eligible is True
    assert not db_session.get(MediaFile, recent).force_delete_eligible
