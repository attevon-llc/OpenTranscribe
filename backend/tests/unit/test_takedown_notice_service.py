"""Takedown notice records: the invariants that make the record trustworthy (issue #1254).

The property under test is that a quarantined file cannot exist without a record, and that
the record outlives the file, its owner, its handler and its organization. Each test below
is one whose removal is silent: a link written after the commit, a release that forgets to
close its links, a foreign key that cascades the evidence away.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.enums import FileStatus
from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNoticeType
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.takedown import TakedownNotice
from app.models.takedown import TakedownNoticeFile
from app.models.user import User
from app.services.takedown_notice_service import TakedownNoticeError
from app.services.takedown_notice_service import create_notice
from app.services.takedown_notice_service import derive_notice_status
from app.services.takedown_notice_service import transition_link
from app.services.takedown_service import TakedownActor
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Storage, cache and owner-notice side effects are not what these tests are about."""
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", MagicMock())
    monkeypatch.setattr("app.services.takedown_service.notify_owner_takedown", MagicMock())
    monkeypatch.setattr("app.services.takedown_service.notify_owner_release", MagicMock())
    monkeypatch.setattr("app.services.takedown_service.audit_takedown", MagicMock())
    monkeypatch.setattr("app.tasks.erasure_reconciliation.notify_hold_released", MagicMock())


def _file(db, owner, organization_id=None) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"notice_{file_uuid.hex[:8]}.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _notice(db, admin, notice_type=TakedownNoticeType.DMCA, ref="CASE-1") -> TakedownNotice:
    notice = create_notice(
        db,
        handler=admin,
        authority="platform",
        notice_type=notice_type,
        received_at=datetime.now(UTC),
        requester_reference=ref,
        basis="internal basis",
    )
    db.commit()
    return notice


def _links(db, file) -> list[TakedownNoticeFile]:
    db.expire_all()
    return list(
        db.query(TakedownNoticeFile)
        .filter(TakedownNoticeFile.file_uuid == file.uuid)
        .order_by(TakedownNoticeFile.id)
        .all()
    )


def _quarantine(db, file, admin, **kw):
    kw.setdefault("reason", "shown to the owner")
    if "notice" not in kw:
        kw.setdefault("operator_action_reason", "operator decided")
    return quarantine_file(db, file, actor=TakedownActor.platform(admin), **kw)


def test_quarantine_writes_exactly_one_link_with_the_flags(db_session, normal_user, admin_user):
    file = _file(db_session, normal_user)
    _quarantine(db_session, file, admin_user, legal_hold=True)

    (link,) = _links(db_session, file)
    assert file.is_quarantined is True
    assert (link.status, link.owner_reason, link.legal_hold_placed) == (
        "open",
        "shown to the owner",
        True,
    )
    assert link.owner_user_uuid == normal_user.uuid and link.file_id == file.id
    assert link.notice.notice_type == "operator_action"
    assert link.notice.basis == "operator decided"
    assert link.notice.requester_reference is None
    assert link.notice.recorded_by_authority == "platform"
    assert link.notice.handled_by_user_uuid == admin_user.uuid


@pytest.mark.parametrize("both", [False, True])
def test_a_takedown_must_cite_a_notice_or_state_it_had_none(
    db_session, normal_user, admin_user, both
):
    file = _file(db_session, normal_user)
    kwargs: dict[str, Any] = {"reason": "r"}
    if both:
        kwargs["notice"] = _notice(db_session, admin_user)
        kwargs["operator_action_reason"] = "also this"
    with pytest.raises(ValueError):
        quarantine_file(db_session, file, actor=TakedownActor.platform(admin_user), **kwargs)
    db_session.refresh(file)
    assert file.is_quarantined is False and file.status == FileStatus.COMPLETED
    assert _links(db_session, file) == []


def test_a_link_that_cannot_be_written_leaves_the_file_untouched(
    db_session, normal_user, admin_user, monkeypatch
):
    """The record goes in before the first commit. Written afterwards it could fail alone."""
    file = _file(db_session, normal_user)
    savepoint = db_session.begin_nested()
    monkeypatch.setattr(db_session, "rollback", savepoint.rollback)
    boom = MagicMock(side_effect=RuntimeError("link insert failed"))
    monkeypatch.setattr("app.services.takedown_service.record_quarantine", boom)

    with pytest.raises(RuntimeError):
        _quarantine(db_session, file, admin_user)

    db_session.expire_all()
    reloaded = db_session.get(MediaFile, file.id)
    assert reloaded.is_quarantined is False
    assert reloaded.legal_hold is False
    assert reloaded.status == FileStatus.COMPLETED
    assert reloaded.quarantine_reason is None


def test_requarantine_under_the_same_notice_is_a_noop_on_the_link(
    db_session, normal_user, admin_user
):
    file = _file(db_session, normal_user)
    notice = _notice(db_session, admin_user)
    _quarantine(db_session, file, admin_user, notice=notice, operator_action_reason=None)
    (first,) = _links(db_session, file)
    first.status = "upheld"
    first.outcome_at = datetime.now(UTC)
    db_session.commit()

    _quarantine(
        db_session, file, admin_user, notice=notice, operator_action_reason=None, reason="x"
    )

    (link,) = _links(db_session, file)
    assert link.id == first.id
    assert link.status == "upheld" and link.outcome_at is not None, "a refresh must not reset"


def test_a_second_notice_adds_a_second_link_and_one_release_closes_both(
    db_session, normal_user, admin_user
):
    file = _file(db_session, normal_user)
    _quarantine(
        db_session,
        file,
        admin_user,
        notice=_notice(db_session, admin_user, ref="A"),
        operator_action_reason=None,
    )
    _quarantine(
        db_session,
        file,
        admin_user,
        notice=_notice(db_session, admin_user, ref="B"),
        operator_action_reason=None,
    )
    assert len(_links(db_session, file)) == 2

    release_file(
        db_session, file, actor=TakedownActor.platform(admin_user), reason="counter-notice upheld"
    )

    links = _links(db_session, file)
    assert [lk.status for lk in links] == ["released", "released"]
    assert all(lk.outcome_at is not None for lk in links)
    assert all(lk.outcome_note == "counter-notice upheld" for lk in links)
    # The live columns are wiped by a release; the record still says what the owner was shown.
    db_session.refresh(file)
    assert file.quarantine_reason is None
    assert all(lk.owner_reason == "shown to the owner" for lk in links)


def test_a_closed_notice_cannot_be_reused_for_the_same_file(
    db_session, normal_user, admin_user, monkeypatch
):
    file = _file(db_session, normal_user)
    notice = _notice(db_session, admin_user)
    _quarantine(db_session, file, admin_user, notice=notice, operator_action_reason=None)
    release_file(db_session, file, actor=TakedownActor.platform(admin_user))
    savepoint = db_session.begin_nested()
    monkeypatch.setattr(db_session, "rollback", savepoint.rollback)

    with pytest.raises(TakedownNoticeError) as refused:
        _quarantine(db_session, file, admin_user, notice=notice, operator_action_reason=None)

    assert refused.value.status_code == 409
    db_session.expire_all()
    assert db_session.get(MediaFile, file.id).is_quarantined is False


def test_no_quarantined_file_lacks_a_live_link_and_no_free_file_keeps_one(
    db_session, normal_user, admin_user
):
    """The sweep an integrity check would run, after every shape of quarantine and release."""
    files = [_file(db_session, normal_user) for _ in range(3)]
    notice = _notice(db_session, admin_user)
    _quarantine(db_session, files[0], admin_user)
    _quarantine(db_session, files[1], admin_user, notice=notice, operator_action_reason=None)
    _quarantine(db_session, files[1], admin_user)
    _quarantine(db_session, files[2], admin_user)
    release_file(
        db_session, files[2], actor=TakedownActor.platform(admin_user), clear_legal_hold=False
    )

    ids = [f.id for f in files]
    live = ("open", "counter_notice", "upheld")
    unrecorded = (
        db_session.query(MediaFile)
        .filter(MediaFile.id.in_(ids), MediaFile.is_quarantined.is_(True))
        .filter(
            ~db_session.query(TakedownNoticeFile)
            .filter(TakedownNoticeFile.file_id == MediaFile.id, TakedownNoticeFile.status.in_(live))
            .exists()
        )
        .count()
    )
    stale = (
        db_session.query(TakedownNoticeFile)
        .join(MediaFile, MediaFile.id == TakedownNoticeFile.file_id)
        .filter(MediaFile.id.in_(ids), MediaFile.is_quarantined.is_(False))
        .filter(TakedownNoticeFile.status.in_(live))
        .count()
    )
    assert (unrecorded, stale) == (0, 0)
    # Released-but-held (#825) is a released link with the hold still on.
    db_session.refresh(files[2])
    assert files[2].legal_hold is True and files[2].is_quarantined is False


def test_the_record_survives_the_file_the_owner_the_handler_and_the_organization(
    db_session, normal_user, admin_user
):
    org = Organization(name="Takedown Org", slug=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    file = _file(db_session, normal_user, organization_id=org.id)
    _quarantine(db_session, file, admin_user)
    (link,) = _links(db_session, file)
    file_uuid, owner_uuid, org_uuid = file.uuid, normal_user.uuid, org.uuid
    notice_id = link.notice_id

    # Deleting the file would fail (NO ACTION) or erase the record (CASCADE) if the FK were wrong.
    db_session.delete(file)
    db_session.commit()
    db_session.delete(org)
    db_session.commit()
    db_session.delete(normal_user)
    db_session.commit()
    db_session.delete(admin_user)
    db_session.commit()

    db_session.expire_all()
    kept = db_session.get(TakedownNoticeFile, link.id)
    assert kept is not None and kept.file_id is None
    assert (kept.file_uuid, kept.owner_user_uuid, kept.organization_uuid) == (
        file_uuid,
        owner_uuid,
        org_uuid,
    )
    assert kept.organization_id is None
    notice = db_session.get(TakedownNotice, notice_id)
    assert notice.handled_by_user_id is None and notice.handled_by_user_uuid is not None


def test_an_operator_action_carries_no_reference_and_a_notice_must():
    admin = MagicMock(spec=User)
    base: dict[str, Any] = {
        "handler": admin,
        "authority": "platform",
        "received_at": datetime.now(UTC),
        "basis": "b",
    }
    with pytest.raises(ValueError):
        create_notice(
            MagicMock(),
            notice_type=TakedownNoticeType.OPERATOR_ACTION,
            requester_reference="X",
            **base,
        )
    with pytest.raises(ValueError):
        create_notice(
            MagicMock(), notice_type=TakedownNoticeType.DMCA, requester_reference=None, **base
        )


# --- the status machine (4.4) -----------------------------------------------------------

_O, _C, _U, _R = (
    TakedownLinkStatus.OPEN,
    TakedownLinkStatus.COUNTER_NOTICE,
    TakedownLinkStatus.UPHELD,
    TakedownLinkStatus.RELEASED,
)


def _link_in(db, owner, admin, status: TakedownLinkStatus, *, held: bool = True):
    file = _file(db, owner)
    _quarantine(db, file, admin)
    (link,) = _links(db, file)
    link.status = status.value
    if status is _C:
        link.counter_notice_received_at = datetime.now(UTC)
    if status in (_U, _R):
        link.outcome_at = datetime.now(UTC)
    if not held:
        file.is_quarantined = False
    db.commit()
    return link


@pytest.mark.parametrize(
    ("start", "target", "allowed"),
    [
        (_O, _C, True),
        (_O, _U, True),
        (_C, _U, True),
        (_U, _C, True),
        (_O, _O, False),
        (_C, _C, False),
        (_U, _U, False),
        (_C, _O, False),
        (_R, _C, False),
        (_R, _U, False),
        (_O, _R, False),
        (_C, _R, False),
        (_U, _R, False),
    ],
)
def test_every_transition_in_the_table(db_session, normal_user, admin_user, start, target, allowed):
    link = _link_in(db_session, normal_user, admin_user, start)
    if allowed:
        transition_link(db_session, link, to=target, received_at=None, note="n")
        assert link.status == target.value
        if target is _C:
            assert link.counter_notice_received_at is not None
        else:
            assert link.outcome_at is not None
    else:
        with pytest.raises(TakedownNoticeError) as refused:
            transition_link(db_session, link, to=target, received_at=None, note=None)
        assert refused.value.status_code == 409
        assert link.status == start.value


def test_a_takedown_that_is_not_in_force_cannot_be_upheld(db_session, normal_user, admin_user):
    link = _link_in(db_session, normal_user, admin_user, _O, held=False)
    with pytest.raises(TakedownNoticeError) as refused:
        transition_link(db_session, link, to=_U, received_at=None, note=None)
    assert refused.value.code == "takedown_not_in_force"


def test_a_notice_over_several_files_is_open_until_every_file_is_resolved(
    db_session, normal_user, admin_user
):
    notice = _notice(db_session, admin_user)
    files = [_file(db_session, normal_user) for _ in range(3)]
    for f in files:
        _quarantine(db_session, f, admin_user, notice=notice, operator_action_reason=None)
    db_session.refresh(notice)
    assert derive_notice_status(notice.files) == "open"

    by_file = {lk.file_uuid: lk for lk in notice.files}
    transition_link(db_session, by_file[files[0].uuid], to=_U, received_at=None, note=None)
    transition_link(db_session, by_file[files[1].uuid], to=_C, received_at=None, note=None)
    assert derive_notice_status(notice.files) == "open", "a contested file keeps it open"
    release_file(db_session, files[2], actor=TakedownActor.platform(admin_user))
    transition_link(db_session, by_file[files[1].uuid], to=_U, received_at=None, note=None)
    db_session.refresh(notice)

    assert [lk.status for lk in notice.files] == ["upheld", "upheld", "released"]
    assert derive_notice_status(notice.files) == "resolved"
    assert derive_notice_status([]) == "open"
