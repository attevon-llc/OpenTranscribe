"""Takedown notice records under concurrency and real deletion paths (issue #1254, audit).

Each test here describes a way the record could disagree with what happened while every
single-request test stays green: an outcome written over a release that committed first, a
second notify call emailing an owner the first call already emailed, a retried delivery
that keeps the time of the first attempt, and a record that only survives a hand-rolled
``DELETE`` rather than the purge the application actually runs.
"""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.enums import FileStatus
from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNoticeType
from app.core.enums import TakedownNotifyEvent
from app.models.media import MediaFile
from app.models.takedown import TakedownNotice
from app.models.takedown import TakedownNoticeFile
from app.models.takedown import TakedownNotification
from app.services import file_cleanup_service
from app.services import takedown_notification_service as notify_mod
from app.services.takedown_notice_queries import owner_takedowns
from app.services.takedown_notice_service import TakedownNoticeError
from app.services.takedown_notice_service import create_notice
from app.services.takedown_notice_service import transition_link
from app.services.takedown_service import TakedownActor
from app.services.takedown_service import quarantine_file
from tests.fixtures.support_world import make_user


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Storage, cache, audit and the live owner notice are not what these tests are about."""
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", MagicMock())
    monkeypatch.setattr("app.services.takedown_service.notify_owner_takedown", MagicMock())
    monkeypatch.setattr("app.services.takedown_service.notify_owner_release", MagicMock())
    monkeypatch.setattr("app.services.takedown_service.audit_takedown", MagicMock())


def _file(db, owner) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"race_{file_uuid.hex[:8]}.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _notice(db, admin) -> TakedownNotice:
    notice = create_notice(
        db,
        handler=admin,
        authority="platform",
        notice_type=TakedownNoticeType.DMCA,
        received_at=datetime.now(UTC),
        requester_reference="CASE-RACE",
        basis="internal basis",
    )
    db.commit()
    return notice


def _takedown(db: Session, file, admin, notice, *, legal_hold=True) -> TakedownNoticeFile:
    quarantine_file(
        db,
        file,
        actor=TakedownActor.platform(admin),
        reason="shown to the owner",
        notice=notice,
        legal_hold=legal_hold,
    )
    return db.query(TakedownNoticeFile).filter(TakedownNoticeFile.file_id == file.id).one()


class _Mailer:
    """Records what would have been mailed; ``on_send`` runs inside the send, mid-request."""

    def __init__(
        self, *, available: bool = True, on_send: Callable[[str], None] | None = None
    ) -> None:
        self.available, self.on_send = available, on_send
        self.sent: list[str] = []

    def has_transport(self) -> bool:
        return self.available

    def send_takedown_notice(self, to_email, subject, heading, paragraphs) -> None:
        self.sent.append(to_email)
        if self.on_send is not None:
            self.on_send(to_email)


def test_an_outcome_on_a_stale_view_cannot_overwrite_a_release_that_committed_first(
    db_session, normal_user, admin_user
):
    """PATCH upheld loaded the link while it was open; a release then committed first.

    The release is applied as SQL the session's identity map never sees, which is exactly
    what a release committed by another request looks like to this one.
    """
    file = _file(db_session, normal_user)
    link = _takedown(db_session, file, admin_user, _notice(db_session, admin_user))
    assert (link.status, file.is_quarantined) == ("open", True)

    db_session.execute(
        text(
            "UPDATE media_file SET is_quarantined = false, quarantine_reason = NULL WHERE id = :fid"
        ),
        {"fid": file.id},
    )
    db_session.execute(
        text(
            "UPDATE takedown_notice_file SET status = 'released', outcome_at = now() "
            "WHERE id = :lid"
        ),
        {"lid": link.id},
    )

    with pytest.raises(TakedownNoticeError) as refused:
        transition_link(
            db_session, link, to=TakedownLinkStatus.UPHELD, received_at=None, note="upheld"
        )
    db_session.flush()

    assert refused.value.status_code == 409
    status = db_session.execute(
        text("SELECT status FROM takedown_notice_file WHERE id = :lid"), {"lid": link.id}
    ).scalar_one()
    assert status == "released", "a stale PATCH overwrote the release"


def test_an_overlapping_notify_call_never_emails_an_owner_twice(
    db_session, normal_user, admin_user, monkeypatch
):
    """While this call mails the first owner, another call has already mailed the second."""
    other_owner = make_user(db_session, role="user", label="race-owner")
    notice = _notice(db_session, admin_user)
    first_link = _takedown(db_session, _file(db_session, normal_user), admin_user, notice)
    second_link = _takedown(db_session, _file(db_session, other_owner), admin_user, notice)
    by_email = {normal_user.email: second_link, other_owner.email: first_link}

    def _the_other_call_got_there_first(to_email: str) -> None:
        if len(mailer.sent) == 1:
            other = by_email[to_email]
            db_session.add(
                TakedownNotification(
                    link_id=other.id,
                    event=TakedownNotifyEvent.QUARANTINED.value,
                    recipient_kind="owner",
                    recipient_user_uuid=other.owner_user_uuid,
                    channel="email",
                    status="sent",
                )
            )
            db_session.flush()

    mailer = _Mailer(on_send=_the_other_call_got_there_first)
    monkeypatch.setattr(notify_mod, "email_service", mailer)

    result = notify_mod.notify_owners(db_session, notice, TakedownNotifyEvent.QUARANTINED)

    assert len(mailer.sent) == 1, f"an owner was emailed twice: {mailer.sent}"
    assert (result.sent, result.already_notified) == (1, 1)
    rows = db_session.query(TakedownNotification).filter(
        TakedownNotification.link_id.in_([first_link.id, second_link.id])
    )
    assert sorted(r.status for r in rows) == ["sent", "sent"]


def test_a_retried_delivery_records_when_it_was_last_attempted(
    db_session, normal_user, admin_user, monkeypatch
):
    """A row first written as "no transport" and later sent must not keep the first time."""
    notice = _notice(db_session, admin_user)
    link = _takedown(db_session, _file(db_session, normal_user), admin_user, notice)
    mailer = _Mailer(available=False)
    monkeypatch.setattr(notify_mod, "email_service", mailer)
    notify_mod.notify_owners(db_session, notice, TakedownNotifyEvent.QUARANTINED)
    row = db_session.query(TakedownNotification).filter_by(link_id=link.id).one()
    assert row.status == "skipped_no_transport"
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    row.attempted_at = long_ago
    db_session.commit()

    mailer.available = True
    notify_mod.notify_owners(db_session, notice, TakedownNotifyEvent.QUARANTINED)

    db_session.expire_all()
    row = db_session.query(TakedownNotification).filter_by(link_id=link.id).one()
    assert row.status == "sent"
    assert row.attempted_at > long_ago, "the log still dates the delivery to the first attempt"


def test_the_record_survives_the_real_purge_and_the_owner_can_still_read_it(
    db_session, normal_user, admin_user
):
    """The application's own purge, not a hand-written DELETE (issue acceptance: retention)."""
    file = _file(db_session, normal_user)
    link = _takedown(
        db_session, file, admin_user, _notice(db_session, admin_user), legal_hold=False
    )
    file_id, file_uuid, link_id = file.id, file.uuid, link.id

    result = file_cleanup_service.purge_media_file(db_session, file)

    assert result["deleted"] is True
    db_session.expire_all()
    assert db_session.get(MediaFile, file_id) is None
    kept = db_session.get(TakedownNoticeFile, link_id)
    assert kept is not None, "the purge took the takedown record with it"
    assert (kept.file_id, kept.file_uuid, kept.status) == (None, file_uuid, "open")
    takedowns, total = owner_takedowns(db_session, normal_user.uuid, limit=10, offset=0)
    assert total == 1
    (seen,) = takedowns
    assert (seen.file_uuid, seen.file_title, seen.owner_reason) == (
        str(file_uuid),
        None,
        "shown to the owner",
    )
