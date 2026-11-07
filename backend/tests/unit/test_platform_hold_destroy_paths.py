"""A PLATFORM hold (v467) keeps a file alive through every destroy path, with no change to them.

A platform hold is placed by an instance administrator without a takedown. It is an ordinary
``content_hold`` row, so ``hold_guard``'s predicates (which every destroy path asks) already
cover it; this module is the proof, one case per destroy path, each with its control (the same
call goes through once the hold is released, so a refusal never means "this path always
refuses"). It also pins the service's rules for the platform authority: any tenant's file or
collection may be held, a platform hold and an organization hold never release each other, and
a release nudges the erasure sweep only for files nobody holds any more.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

import pytest
from fastapi import status
from sqlalchemy import text

from app.core.exceptions import ConflictError
from app.models.content_hold import AUTHORITY_ORGANIZATION
from app.models.content_hold import AUTHORITY_PLATFORM
from app.models.content_hold import ContentHold
from app.models.media import MediaFile
from app.services import content_hold_service as holds
from app.services import file_cleanup_service
from app.services import trash_purge
from app.services import trash_service
from app.services.hold_guard import HoldRefusedError
from app.services.hold_guard import hold_state

REASON = "Preservation demand 2026-77"


@pytest.fixture(autouse=True)
def _no_external_copies(monkeypatch, db_session):
    """No object storage or search here; the purge's own sessions run on the test session."""
    import contextlib

    monkeypatch.setattr(file_cleanup_service, "_purge_external_copies", lambda plan: [])
    monkeypatch.setattr(
        "app.services.file_cleanup_service.delete_file_storage_artifacts", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "app.services.file_cleanup_service._cleanup_opensearch_for_file", lambda *a, **k: []
    )

    @contextlib.contextmanager
    def _same_session():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(trash_purge, "session_scope", _same_session)


@pytest.fixture()
def operator(gov):
    user = gov.user("operator")
    user.role = "admin"
    gov.db.commit()
    return user


def _hold(gov, operator, *files: MediaFile) -> ContentHold:
    change = holds.place_file_hold(
        gov.db,
        org_id=None,
        authority=AUTHORITY_PLATFORM,
        actor=operator,
        files=list(files),
        reason=REASON,
    )
    gov.db.commit()
    return change.hold


def _release(gov, operator, hold: ContentHold) -> holds.HoldChange:
    change = holds.release_hold(
        gov.db, org_id=None, authority=AUTHORITY_PLATFORM, actor=operator, hold=hold, reason=REASON
    )
    gov.db.commit()
    return change


def _exists(gov, file: MediaFile) -> bool:
    gov.db.expire_all()
    return gov.db.query(MediaFile.id).filter(MediaFile.id == file.id).first() is not None


# --------------------------------------------------------------------------- #
# Destroy paths: each returns True when it went ahead, False when the hold stopped it
# --------------------------------------------------------------------------- #


def _purge(gov, f: MediaFile) -> bool:
    return bool(file_cleanup_service.purge_media_file(gov.db, f)["deleted"])


def _trash(gov, f: MediaFile) -> bool:
    try:
        trash_service.trash_files(gov.db, [f], actor=gov.alice, request=None, bypass=None)
    except HoldRefusedError:
        # Raised before any write, so there is nothing to roll back.
        return False
    gov.db.commit()
    return True


def _trash_purge(gov, f: MediaFile) -> bool:
    """A file trashed BEFORE the hold stays in the trash, overdue, and is never purged."""
    gov.db.execute(
        text(
            "UPDATE media_file SET deleted_at = now(), purge_after = now() - interval '1 hour', "
            "trash_origin = 'user' WHERE id = :i"
        ),
        {"i": f.id},
    )
    gov.db.commit()
    trash_purge.purge_due(file_ids=[f.id])
    return not _exists(gov, f)


def _retention(gov, f: MediaFile) -> bool:
    from app.core.tenant_limits import resolve_retention_days
    from app.tasks import cleanup

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    return f.id in {
        file_id
        for file_id, _ in cleanup._select_expired_files(
            gov.db, {"delete_error_files": False}, cutoff, cutoff, resolve_retention_days
        )
    }


def _account_deletion(gov, f: MediaFile) -> bool:
    from app.services.user_deletion_service import _assert_no_held_files

    try:
        _assert_no_held_files(gov.db, gov.alice.id)
    except ConflictError as refused:
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert isinstance(refused.detail, dict) and refused.detail["held_by"] == ["platform"]
        assert REASON not in str(refused.detail)
        return False
    return True


def _gdpr_erasure(gov, f: MediaFile) -> bool:
    from app.services import gdpr_erasure_service as gdpr

    summary = gdpr._new_summary("user", gov.alice.id)
    gdpr._purge_files(gov.db, [f], summary)
    return not _exists(gov, f)


DESTROY_PATHS: dict[str, Callable[..., bool]] = {
    "purge_media_file": _purge,
    "trash": _trash,
    "trash_purge": _trash_purge,
    "retention": _retention,
    "account_deletion": _account_deletion,
    "gdpr_erasure": _gdpr_erasure,
}


@pytest.mark.parametrize("path", sorted(DESTROY_PATHS))
def test_a_platform_hold_stops_every_destroy_path_and_releasing_it_lets_it_through(
    gov, operator, path
):
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=400)
    f = gov.file(gov.alice, upload_time=old, completed_at=old)
    hold = _hold(gov, operator, f)
    assert hold_state(gov.db, f.id).held_by == ["platform"]

    assert DESTROY_PATHS[path](gov, f) is False, f"{path} went ahead on a platform-held file"
    assert _exists(gov, f)

    _release(gov, operator, hold)
    assert DESTROY_PATHS[path](gov, f) is True, f"{path} still refused after the release"


# --------------------------------------------------------------------------- #
# The service's rules for the platform authority
# --------------------------------------------------------------------------- #


def test_any_tenants_file_can_be_held_by_the_platform(gov, operator):
    files = [
        gov.file(gov.alice),
        gov.file(gov.outsider, org=gov.other_org),
        gov.file(gov.bob, org=None),
    ]
    hold = _hold(gov, operator, *files)
    assert (hold.authority, hold.organization_id) == (AUTHORITY_PLATFORM, None)
    assert hold.placed_by_user_id == operator.id
    for f in files:
        assert hold_state(gov.db, f.id).platform_hold_uuids == (str(hold.uuid),)


def test_a_platform_collection_hold_covers_files_added_later(gov, operator):
    collection = gov.collection(gov.alice)
    early, late = gov.file(gov.alice), gov.file(gov.alice)
    gov.put(collection, early)
    change = holds.place_collection_hold(
        gov.db,
        org_id=None,
        authority=AUTHORITY_PLATFORM,
        actor=operator,
        collection=collection,
        reason=REASON,
    )
    gov.db.commit()
    assert change.file_count == 1 and not hold_state(gov.db, late.id).held

    gov.put(collection, late)
    assert hold_state(gov.db, late.id).platform_hold_uuids == (str(change.hold.uuid),)


@pytest.mark.parametrize(
    ("org_id", "authority"),
    [(None, AUTHORITY_ORGANIZATION), (1, AUTHORITY_PLATFORM), (None, "operator")],
)
def test_an_authority_and_an_organization_must_agree(gov, operator, org_id, authority):
    f = gov.file(gov.alice)
    with pytest.raises(ValueError, match="authority"):
        holds.place_file_hold(
            gov.db, org_id=org_id, authority=authority, actor=operator, files=[f], reason=REASON
        )
    assert not hold_state(gov.db, f.id).held


def test_a_platform_hold_and_an_organization_hold_never_release_each_other(gov, operator):
    f = gov.file(gov.alice)
    platform = _hold(gov, operator, f)
    org = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=[f], reason=REASON
    ).hold
    gov.db.commit()

    with pytest.raises(holds.HoldRequestError) as by_org:
        holds.release_hold(gov.db, org_id=gov.org.id, actor=gov.admin, hold=platform, reason=REASON)
    with pytest.raises(holds.HoldRequestError) as by_platform:
        holds.release_hold(
            gov.db,
            org_id=None,
            authority=AUTHORITY_PLATFORM,
            actor=operator,
            hold=org,
            reason=REASON,
        )
    assert by_org.value.code == by_platform.value.code == "not_found"
    assert platform.active and org.active


def test_release_frees_only_files_nobody_else_holds_and_nudges_only_those(gov, operator):
    shared, alone, flagged = gov.file(gov.alice), gov.file(gov.alice), gov.file(gov.alice)
    flagged.legal_hold = True  # the takedown's own hold
    gov.db.commit()
    hold = _hold(gov, operator, shared, alone, flagged)
    holds.place_file_hold(gov.db, org_id=gov.org.id, actor=gov.admin, files=[shared], reason=REASON)
    gov.db.commit()

    change = _release(gov, operator, hold)

    assert [f.id for f in change.unheld_files] == [alone.id]
    assert change.unlock_paths == [alone.storage_path]
    assert hold_state(gov.db, shared.id).held_by == ["organization"]
    assert hold_state(gov.db, flagged.id).held_by == ["platform"]
    with pytest.raises(holds.HoldRequestError) as again:
        _release(gov, operator, hold)
    assert again.value.code == "already_released"


def test_the_audit_event_names_the_authority_and_never_the_reason(gov, operator, monkeypatch):
    from app.services import audit_outbox

    staged: list[dict] = []
    real_stage = audit_outbox.stage

    def _stage(db, **kw):
        staged.append(kw)
        return real_stage(db, **kw)

    monkeypatch.setattr(audit_outbox, "stage", _stage)
    f = gov.file(gov.outsider, org=gov.other_org)
    change = holds.place_file_hold(
        gov.db,
        org_id=None,
        authority=AUTHORITY_PLATFORM,
        actor=operator,
        files=[f],
        reason=REASON,
        matter_ref="MATTER-9",
    )
    holds.record_hold_change(gov.db, None, operator, change)
    gov.db.commit()

    assert len(staged) == 1
    event = staged[0]
    assert event["resource_type"] == "content_hold"
    assert event["resource_uuid"] == str(change.hold.uuid)
    assert event["organization_id"] is None
    assert event["details"] == {
        "authority": "platform",
        "scope": "file",
        "file_count": 1,
        "file_uuids": [str(f.uuid)],
    }
    assert REASON not in str(staged) and "MATTER-9" not in str(staged)
