"""``FileCleanupService.force_cleanup_orphaned_files`` — the admin delete that deleted nothing.

``cleanup.deep_cleanup`` calls it with ``dry_run=False`` and logs its counters as a
success. Every file it touched actually raised, so the pass reported
``successfully_deleted: 0`` on every run and the orphaned files stayed forever.

Storage and OpenSearch side effects are patched out: ``db_session`` rolls the
database back, but MinIO and OpenSearch would not be.
"""

import uuid as uuid_pkg

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.services import file_cleanup_service as svc

pytestmark = pytest.mark.xdist_group("force_cleanup_orphaned")


@pytest.fixture
def isolated_orphan(db_session, normal_user, monkeypatch):
    """One force-delete-eligible orphan, and no other eligible file in scope.

    ``force_cleanup_orphaned_files`` sweeps the whole deployment, so any other
    eligible row in the dev database would make the counters ambiguous. Clearing
    their flag is undone with the savepoint, and the external side effects of
    ``purge_media_file`` are stubbed so nothing outside Postgres is touched.

    Returns:
        The id of the created orphan.
    """
    monkeypatch.setattr(svc, "delete_file_storage_artifacts", lambda db, file: True)
    monkeypatch.setattr(svc, "_cleanup_opensearch_for_file", lambda file, file_uuid: None)

    db_session.query(MediaFile).filter(MediaFile.force_delete_eligible.is_(True)).update(
        {"force_delete_eligible": False}, synchronize_session=False
    )

    orphan = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename=f"orphan-{uuid_pkg.uuid4().hex[:8]}.mp4",
        content_type="video/mp4",
        file_size=2048,
        storage_path=f"orphan-cleanup-test/{uuid_pkg.uuid4().hex}",
        status=FileStatus.ORPHANED,
        force_delete_eligible=True,
    )
    db_session.add(orphan)
    db_session.flush()
    return int(orphan.id)


def test_eligible_orphan_is_really_deleted(db_session, isolated_orphan):
    """Defect: the delete path was dead — ``str(file.id)`` given to a ``file_uuid`` param.

    ``delete_media_file(db, str(file.id), …)`` resolved its argument as a UUID, so
    ``UUID("123")`` raised for every file: each one landed in ``deletion_errors``
    while ``run_deep_cleanup`` logged the pass as a success with 0 deletions.
    """
    results = svc.cleanup_service.force_cleanup_orphaned_files(db_session, dry_run=False)

    assert results["successfully_deleted"] == 1
    assert results["deletion_errors"] == []
    assert db_session.query(MediaFile).filter(MediaFile.id == isolated_orphan).first() is None


def test_dry_run_deletes_nothing(db_session, isolated_orphan):
    """Control: the preview mode must still only preview.

    Without this, "successfully_deleted == 1" above could be satisfied by a
    function that deletes unconditionally.
    """
    results = svc.cleanup_service.force_cleanup_orphaned_files(db_session, dry_run=True)

    assert results["successfully_deleted"] == 0
    assert results["files_processed"][0]["status"] == "would_delete"
    assert db_session.query(MediaFile).filter(MediaFile.id == isolated_orphan).first() is not None
