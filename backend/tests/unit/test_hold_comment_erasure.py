"""A comment on SOMEBODY ELSE's held file outlives the commenter's erasure (plan §3.6).

``comment.user_id`` is NOT NULL, so erasing or deleting an account must delete or de-attribute
every comment it wrote. Under the legal-hold freeze a comment on a held file is part of that
file's preserved record, so deleting it is refused like any other change to the file; the
existing hold deferral covered only the subject's OWN files. The resolution:

* GDPR erasure (account-wide and an organization member's) keeps the comment, counts its file in
  ``legal_holds_skipped`` (so the ledger entry is ``deferred`` / ``legal_hold`` and the sweep
  finishes the job once the hold is released) and retains the user row the FK needs.
* Account deletion (``delete_user_account``) has no deferral: it is refused, as it already is
  for an account that owns a held file.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.core.exceptions import ConflictError
from app.core.security import get_password_hash
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.user import User
from app.services.gdpr_erasure_service import erase_user
from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
from app.services.user_deletion_service import delete_user_account

pytestmark = pytest.mark.usefixtures("no_receipt_measurement", "no_erasure_extras")


def _user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _file(db, owner: User, *, held: bool) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="shared.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        legal_hold=held,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _comment(db, file: MediaFile, author: User) -> int:
    comment = Comment(media_file_id=file.id, user_id=author.id, text="noted for the matter")
    db.add(comment)
    db.commit()
    return int(comment.id)


def _exists(db, model, row_id: int) -> bool:
    db.expire_all()
    return db.get(model, row_id) is not None


def test_erasing_a_commenter_keeps_their_comment_on_a_held_file_and_defers(db_session):
    owner = _user(db_session, "owner")
    commenter = _user(db_session, "commenter")
    on_held = _comment(db_session, _file(db_session, owner, held=True), commenter)
    on_free = _comment(db_session, _file(db_session, owner, held=False), commenter)

    summary = erase_user(db_session, commenter.id)

    assert _exists(db_session, Comment, on_held), "the held file's record keeps the comment"
    assert not _exists(db_session, Comment, on_free)
    assert summary["comments_deleted"] == 1
    assert summary["legal_holds_skipped"] == 1, "a deferral the sweep will finish"
    assert summary["users_deleted"] == 0
    assert _exists(db_session, User, commenter.id), "the comment's NOT NULL author is kept"


def test_erasing_a_commenter_with_nothing_held_completes(db_session):
    """Control: the same erasure with no hold anywhere deletes the comment and the account."""
    owner = _user(db_session, "owner")
    commenter = _user(db_session, "commenter")
    on_free = _comment(db_session, _file(db_session, owner, held=False), commenter)

    summary = erase_user(db_session, commenter.id)

    assert not _exists(db_session, Comment, on_free)
    assert summary["legal_holds_skipped"] == 0
    assert summary["users_deleted"] == 1


def test_an_org_member_erasure_defers_the_same_way(db_session):
    """The helper both erasure paths share, against a query scoped like the member path's."""
    from app.services.gdpr_erasure_steps import _delete_comments_deferring_held

    owner = _user(db_session, "owner")
    commenter = _user(db_session, "commenter")
    held_file = _file(db_session, owner, held=True)
    on_held = _comment(db_session, held_file, commenter)
    on_free = _comment(db_session, _file(db_session, owner, held=False), commenter)
    errors: list[dict[str, str]] = []
    summary: dict[str, Any] = {"legal_holds_skipped": 0, "errors": errors}

    deleted = _delete_comments_deferring_held(
        db_session, db_session.query(Comment).filter(Comment.user_id == commenter.id), summary
    )

    assert deleted == 1
    assert _exists(db_session, Comment, on_held)
    assert not _exists(db_session, Comment, on_free)
    assert summary["legal_holds_skipped"] == 1
    assert [e["file_uuid"] for e in errors] == [str(held_file.uuid)]


def test_deleting_an_account_that_commented_on_a_held_file_is_refused(db_session):
    owner = _user(db_session, "owner")
    commenter = _user(db_session, "commenter")
    on_held = _comment(db_session, _file(db_session, owner, held=True), commenter)

    with pytest.raises(ConflictError) as refused:
        delete_user_account(db_session, commenter)

    detail = refused.value.detail
    assert isinstance(detail, dict)
    assert detail["error"] == LEGAL_HOLD_ERROR_CODE
    assert detail["files_under_legal_hold"] == 0
    assert detail["commented_files_under_legal_hold"] == 1
    assert _exists(db_session, Comment, on_held)
    assert _exists(db_session, User, commenter.id)
