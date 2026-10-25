"""An ORGANIZATION hold keeps a file alive through every path that would destroy it (#1276).

``media_file.legal_hold`` was the only hold, and eleven places read it. An organization hold is a
row in ``content_hold``, so each of those places had to start asking ``hold_guard`` instead. Each
test below is one of them with an org-held file, paired with the control that the same call
succeeds once the hold is released (so a refusal can never mean "this path always refuses").
``tests/unit/test_hold_guard_single_source.py`` fails when a new reader of ``legal_hold`` appears.
"""

from __future__ import annotations

import contextlib
import datetime
import uuid as uuid_pkg
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi import status
from sqlalchemy import select

from app.core.enums import FileStatus
from app.models.content_hold import ContentHold
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services import content_hold_service as holds
from app.services import file_cleanup_service
from app.services.hold_guard import held_pred
from app.services.hold_guard import hold_state
from app.services.hold_guard import not_held_pred

REASON = "Litigation matter 2026-14"


@pytest.fixture(autouse=True)
def _no_external_copies(monkeypatch):
    """The object-storage and search legs of a destroy address nothing that exists here."""
    monkeypatch.setattr(file_cleanup_service, "_purge_external_copies", lambda plan: [])


def _hold(gov, *files: MediaFile) -> ContentHold:
    change = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=list(files), reason=REASON
    )
    gov.db.commit()
    return change.hold


def _release(gov, hold: ContentHold) -> None:
    holds.release_hold(gov.db, org_id=gov.org.id, actor=gov.admin, hold=hold, reason=REASON)
    gov.db.commit()


def _exists(gov, file: MediaFile) -> bool:
    gov.db.expire_all()
    return gov.db.query(MediaFile.id).filter(MediaFile.id == file.id).first() is not None


# --------------------------------------------------------------------------- #
# The guard itself
# --------------------------------------------------------------------------- #


def test_state_names_who_holds_a_file(gov):
    free, by_org, by_platform, by_both = (gov.file(gov.alice) for _ in range(4))
    hold = _hold(gov, by_org, by_both)
    for f in (by_platform, by_both):
        f.legal_hold = True
    gov.db.commit()

    assert hold_state(gov.db, free.id).held is False
    org = hold_state(gov.db, by_org.id)
    assert (org.held, org.held_by, org.org_hold_uuids) == (
        True,
        ["organization"],
        (str(hold.uuid),),
    )
    assert hold_state(gov.db, by_platform.id).held_by == ["platform"]
    assert hold_state(gov.db, by_both.id).held_by == ["platform", "organization"]


def test_the_sql_predicates_agree_with_the_per_file_state(gov):
    files = [gov.file(gov.alice) for _ in range(5)]
    free, by_org, platform, both, released = files
    live = _hold(gov, by_org, both)
    done = _hold(gov, released)
    _release(gov, done)
    for f in (platform, both):
        f.legal_hold = True
    gov.db.commit()
    assert live.active and not done.active

    ids = [f.id for f in files]
    held_ids = set(gov.db.scalars(select(MediaFile.id).where(MediaFile.id.in_(ids), held_pred())))
    free_ids = set(
        gov.db.scalars(select(MediaFile.id).where(MediaFile.id.in_(ids), not_held_pred()))
    )

    assert held_ids == {by_org.id, platform.id, both.id}
    assert free_ids == {free.id, released.id}
    assert held_ids | free_ids == set(ids) and not held_ids & free_ids
    for f in files:
        assert (f.id in held_ids) == hold_state(gov.db, f.id).held


def test_a_collection_hold_covers_files_added_later_and_refuses_their_removal(gov):
    collection = gov.collection(gov.alice)
    early, late = gov.file(gov.alice), gov.file(gov.alice)
    gov.put(collection, early)
    change = holds.place_collection_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, collection=collection, reason=REASON
    )
    gov.db.commit()
    assert hold_state(gov.db, early.id).held and not hold_state(gov.db, late.id).held

    gov.put(collection, late)
    holds.on_collection_media_added(gov.db, collection.id, [late])
    gov.db.commit()
    assert hold_state(gov.db, late.id).held
    # Idempotent: adding the same file again does not duplicate its cover.
    assert holds.on_collection_media_added(gov.db, collection.id, [late]) == []

    from app.services.content_hold_queries import held_by_collection_hold

    assert held_by_collection_hold(gov.db, collection.id, [early.id, late.id]) == {
        early.id,
        late.id,
    }
    _release(gov, change.hold)
    assert not hold_state(gov.db, early.id).held


def test_releasing_one_hold_keeps_a_file_another_hold_still_covers(gov):
    f = gov.file(gov.alice)
    first, second = _hold(gov, f), _hold(gov, f)
    _release(gov, first)
    assert hold_state(gov.db, f.id).org_hold_uuids == (str(second.uuid),)
    _release(gov, second)
    assert not hold_state(gov.db, f.id).held


@pytest.mark.parametrize("reason", ["", "too short", "x" * 2001])
def test_a_reason_outside_its_bounds_is_refused(gov, reason):
    f = gov.file(gov.alice)
    with pytest.raises(holds.HoldRequestError) as refused:
        holds.place_file_hold(gov.db, org_id=gov.org.id, actor=gov.admin, files=[f], reason=reason)
    assert refused.value.code == "reason_length"


def test_only_the_organizations_own_files_can_be_held(gov):
    foreign = gov.file(gov.outsider, org=gov.other_org)
    personal = gov.file(gov.alice, org=None)
    for f in (foreign, personal):
        with pytest.raises(holds.HoldRequestError) as refused:
            holds.place_file_hold(
                gov.db, org_id=gov.org.id, actor=gov.admin, files=[f], reason=REASON
            )
        assert refused.value.code == "not_org_file"


# --------------------------------------------------------------------------- #
# Every destroy path
# --------------------------------------------------------------------------- #


def test_purge_refuses_an_org_held_file_and_deletes_it_once_released(gov):
    f = gov.file(gov.alice)
    hold = _hold(gov, f)

    refused = file_cleanup_service.purge_media_file(gov.db, f)
    assert refused["deleted"] is False and refused["refused_legal_hold"] is True
    assert refused["hold_state"].held_by == ["organization"]
    assert _exists(gov, f)

    _release(gov, hold)
    done = file_cleanup_service.purge_media_file(gov.db, f)
    assert done["deleted"] is True and not _exists(gov, f)


def test_the_delete_route_says_who_holds_the_file_and_only_an_org_admin_gets_the_uuid(client, gov):
    f = gov.file(gov.alice)
    hold = _hold(gov, f)

    with gov.acting_as(gov.alice):
        member = client.delete(f"/api/files/{f.uuid}")
    with gov.acting_as(gov.admin):
        admin = client.delete(f"/api/files/{f.uuid}")

    for response in (member, admin):
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"
        assert response.json()["detail"]["held_by"] == ["organization"]
    assert "holds" not in member.json()["detail"]
    assert admin.json()["detail"]["holds"] == [str(hold.uuid)]
    # The reason is never on the wire, for anyone.
    assert REASON not in member.text + admin.text
    assert _exists(gov, f)

    _release(gov, hold)
    with gov.acting_as(gov.alice):
        assert client.delete(f"/api/files/{f.uuid}").status_code == status.HTTP_204_NO_CONTENT
    assert not _exists(gov, f)


def test_an_editor_share_never_grants_delete_so_a_hold_is_not_the_only_gate(client, gov):
    """Delete is its own right (owner or org admin). A write grant on the collection that
    holds the file does not carry it, held or not, so a hold never has to defend against a
    grantee: it defends against the people who CAN delete."""
    f = gov.file(gov.alice)
    collection = gov.collection(gov.alice)
    gov.put(collection, f)
    gov.share(collection, gov.bob, "editor")
    with gov.acting_as(gov.bob):
        unheld = client.delete(f"/api/files/{f.uuid}")
    _hold(gov, f)
    with gov.acting_as(gov.bob):
        held = client.delete(f"/api/files/{f.uuid}")
    assert unheld.status_code == status.HTTP_403_FORBIDDEN
    assert held.status_code == status.HTTP_403_FORBIDDEN
    assert _exists(gov, f)


def test_cancelling_a_pending_upload_is_refused_while_held(client, gov, monkeypatch):
    monkeypatch.setattr("app.services.multipart_upload.abort_uploads_for_object", lambda path: None)
    f = gov.file(gov.alice, status=FileStatus.PENDING.value)
    _hold(gov, f)
    with gov.acting_as(gov.alice):
        response = client.delete(f"/api/files/{f.uuid}")
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["held_by"] == ["organization"]
    assert _exists(gov, f)


def test_deleting_an_account_that_owns_an_org_held_file_is_refused(gov):
    from app.api.endpoints.admin import _assert_no_held_files

    f = gov.file(gov.alice)
    gov.file(gov.alice)  # an unheld sibling: only the held one counts
    hold = _hold(gov, f)

    with pytest.raises(HTTPException) as refused:
        _assert_no_held_files(gov.db, gov.alice.id)
    detail = refused.value.detail
    assert isinstance(detail, dict)
    assert refused.value.status_code == status.HTTP_409_CONFLICT
    assert detail["files_under_legal_hold"] == 1 and detail["held_by"] == ["organization"]
    assert "holds" not in detail

    _assert_no_held_files(gov.db, gov.bob.id)  # control: nobody holds bob's files
    _release(gov, hold)
    _assert_no_held_files(gov.db, gov.alice.id)


def test_retention_does_not_select_an_org_held_file(gov):
    from app.core.tenant_limits import resolve_retention_days
    from app.tasks import cleanup

    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=400)
    held = gov.file(gov.alice, upload_time=old, completed_at=old)
    free = gov.file(gov.alice, upload_time=old, completed_at=old)
    _hold(gov, held)

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    chosen = {
        file_id
        for file_id, _ in cleanup._select_expired_files(
            gov.db, {"delete_error_files": False}, cutoff, cutoff, resolve_retention_days
        )
    }
    assert free.id in chosen
    assert held.id not in chosen


def test_gdpr_erasure_defers_an_org_held_file(gov):
    from app.services import gdpr_erasure_service as gdpr

    held, free = gov.file(gov.alice), gov.file(gov.alice)
    _hold(gov, held)
    summary = gdpr._new_summary("user", gov.alice.id)

    gdpr._purge_files(gov.db, [held, free], summary)

    assert summary["legal_holds_skipped"] == 1
    assert summary["media_files_deleted"] == 1
    assert _exists(gov, held) and not _exists(gov, free)


def test_erasing_an_organization_keeps_the_row_while_a_hold_is_active(gov):
    from app.models.organization import Organization
    from app.services import gdpr_erasure_service as gdpr

    held, free = gov.file(gov.alice), gov.file(gov.alice)
    hold = _hold(gov, held)
    org_id = gov.org.id

    summary = gdpr.erase_organization(gov.db, org_id)

    gov.db.expire_all()
    assert gov.db.get(Organization, org_id) is not None
    assert summary["legal_holds_skipped"] >= 1
    assert any("legal hold" in str(e.get("error")) for e in summary["errors"])
    assert _exists(gov, held) and not _exists(gov, free)
    assert gov.db.get(ContentHold, hold.id).active


def test_the_duration_backfill_leaves_an_org_held_file_alone(gov, monkeypatch):
    from app.tasks import recovery_tasks

    @contextlib.contextmanager
    def _scope():
        yield gov.db

    monkeypatch.setattr(recovery_tasks, "session_scope", _scope)
    held, free = (
        gov.file(gov.alice, duration=30.0, duration_source=None, metadata_important={})
        for _ in range(2)
    )
    for f in (held, free):
        gov.db.add(TranscriptSegment(media_file_id=f.id, start_time=0.0, end_time=30.0, text="x"))
    gov.db.commit()
    _hold(gov, held)

    summary = recovery_tasks.media_duration_backfill(user_id=gov.alice.id, dry_run=True)

    assert summary["examined"] == 1, "only the unheld file is a candidate"


def test_a_held_file_keeps_its_completion_time_but_a_free_one_moves_it(gov):
    from app.services.takedown_service import stamp_completion_time

    long_ago = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    now = datetime.datetime.now(datetime.UTC)
    held = gov.file(gov.alice, completed_at=long_ago)
    free = gov.file(gov.alice, completed_at=long_ago)
    _hold(gov, held)

    stamp_completion_time(held, now)
    stamp_completion_time(free, now)

    assert held.completed_at == long_ago
    assert free.completed_at == now


# --------------------------------------------------------------------------- #
# Reprocess
# --------------------------------------------------------------------------- #


def test_reprocess_and_retry_are_refused_while_held_and_say_so(client, gov):
    f = gov.file(gov.alice, status=FileStatus.ERROR.value)
    hold = _hold(gov, f)
    with gov.acting_as(gov.alice):
        reprocess = client.post(f"/api/files/{f.uuid}/reprocess")
        retry = client.post(f"/api/files/{f.uuid}/retry")
    for response in (reprocess, retry):
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "FILE_UNDER_LEGAL_HOLD"
        assert detail["held_by"] == ["organization"]
        assert "Editing the transcript is still allowed" in detail["message"]

    from app.api.endpoints.files.reprocess import refuse_reprocess_if_held

    with pytest.raises(HTTPException):
        refuse_reprocess_if_held(gov.db, f.id)
    _release(gov, hold)
    refuse_reprocess_if_held(gov.db, f.id)  # control: the same file passes once released


def test_the_retry_reset_backstop_refuses_a_held_file(gov):
    from app.utils.task_utils import reset_file_for_retry

    f = gov.file(gov.alice, status=FileStatus.ERROR.value)
    hold = _hold(gov, f)
    assert reset_file_for_retry(gov.db, f.id) is False
    gov.db.expire_all()
    assert gov.db.get(MediaFile, f.id).status == FileStatus.ERROR.value
    _release(gov, hold)


# --------------------------------------------------------------------------- #
# The platform side stays correct beside it
# --------------------------------------------------------------------------- #


def test_releasing_the_platforms_hold_does_not_lift_the_object_lock_an_org_hold_needs(gov):
    from app.services.takedown_service import TakedownActor
    from app.services.takedown_service import quarantine_file
    from app.services.takedown_service import release_file

    f = gov.file(gov.alice)
    admin = gov.user("platform")
    admin.role, admin.is_superuser = "super_admin", True
    gov.db.commit()
    locks: list[tuple[str, bool]] = []

    def _record_lock(path: str, on: bool) -> bool:
        locks.append((path, on))
        return True

    with (
        patch("app.services.minio_service.set_object_quarantine_tag", return_value=True),
        patch("app.services.minio_service.set_object_legal_hold", side_effect=_record_lock),
    ):
        quarantine_file(
            gov.db,
            f,
            actor=TakedownActor.platform(admin),
            reason="dmca",
            operator_action_reason="operator action (test)",
        )
        hold = _hold(gov, f)
        release_file(gov.db, f, actor=TakedownActor.platform(admin), clear_legal_hold=True)
        assert (f.storage_path, False) not in locks, "an org hold still covers the file"
        assert hold_state(gov.db, f.id).held_by == ["organization"]

        _release(gov, hold)
        quarantine_file(
            gov.db,
            f,
            actor=TakedownActor.platform(admin),
            reason="dmca",
            operator_action_reason="operator action (test)",
        )
        release_file(gov.db, f, actor=TakedownActor.platform(admin), clear_legal_hold=True)
        assert (f.storage_path, False) in locks, "with no hold left the lock is lifted"


def test_a_released_hold_is_history_not_deleted(gov):
    f = gov.file(gov.alice)
    hold = _hold(gov, f)
    _release(gov, hold)
    row = gov.db.get(ContentHold, hold.id)
    assert row is not None and row.released_at is not None
    assert row.released_by_user_id == gov.admin.id and row.release_reason == REASON
    assert uuid_pkg.UUID(str(row.uuid))
