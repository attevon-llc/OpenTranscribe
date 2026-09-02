"""Deleting a USER must not destroy that user's legally-held files (issue #689).

#664 put the hold guard inside ``file_cleanup_service.purge_media_file`` — the
canonical per-file destroy — and on the retention sweep's candidate query. Admin
user-deletion reaches neither: ``admin._delete_user_media_files`` ends in a **bulk**
``db.query(MediaFile).filter(...).delete()``, which emits one statement, loads no
instances, and consults no ``legal_hold``. So the one deletion path that destroys a
whole account's files at once was also the one path with no hold guard at all, and the
destruction is irreversible.

The policy implemented here is **refuse the whole deletion**, matching
``purge_media_file``'s shipped refusal. Deleting everything except the held files would
leave ``media_file`` rows owned by a deleted account (and ``media_file.user_id`` is a
plain ``NO ACTION`` FK, so the ``user`` delete would then fail mid-cascade anyway);
reassigning them to another owner would invent a retention policy nobody asked for.

Every test drives the real HTTP endpoints against real rows through the savepoint-rolled
back ``db_session``. Both entry points are covered, because there are two:
``DELETE /api/admin/users/{uuid}`` and ``DELETE /api/users/{uuid}`` call the same three
cascade helpers.
"""

from fastapi import status

from app.models.media import MediaFile
from app.models.user import User
from tests.user_owned_rows import make_media_file
from tests.user_owned_rows import seed_owned_rows


def _place_under_legal_hold(db_session, media_file: MediaFile) -> int:
    """Put one existing file under an active legal hold.

    Args:
        db_session: The savepoint-isolated test session.
        media_file: The file to hold.

    Returns:
        The held file's ``media_file.id``.
    """
    media_file.legal_hold = True
    db_session.commit()
    db_session.refresh(media_file)
    return int(media_file.id)


def test_deleting_a_user_who_owns_a_held_file_is_refused(
    client, admin_token_headers, normal_user, db_session
):
    """The defect: a bulk delete destroyed held evidence along with the account.

    Asserts the refusal AND that nothing was destroyed on the way to it —
    ``assert_all_present`` re-checks every table the cascade sweeps, so a guard that
    fired only after ``_delete_user_owned_records`` had already run would fail here
    rather than read as a pass.
    """
    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)
    held_file = db_session.query(MediaFile).filter(MediaFile.id == owned.media_file_id).one()
    held_id = _place_under_legal_hold(db_session, held_file)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "FILE_UNDER_LEGAL_HOLD"
    assert detail["files_under_legal_hold"] == 1
    assert "legal hold" in detail["message"]

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is not None
    assert db_session.query(MediaFile).filter(MediaFile.id == held_id).first() is not None
    # Nothing was deleted on the way to the refusal — a partial destroy followed by a
    # 409 would be worse than either outcome alone.
    owned.assert_all_present(db_session)


def test_an_ordinary_user_with_no_held_files_is_still_deleted(
    client, admin_token_headers, normal_user, db_session
):
    """CONTROL: without it, the guard is indistinguishable from a broken query.

    The refusal test above is satisfied by any change that makes user deletion fail.
    This one proves the deletion the guard protects still happens, and that the files
    really go — ``remaining == {}`` names any table that leaked.
    """
    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)
    media_file_id = owned.media_file_id

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is None
    assert db_session.query(MediaFile).filter(MediaFile.id == media_file_id).first() is None
    assert owned.remaining(db_session) == {}


def test_a_quarantined_file_with_no_hold_does_not_block_the_deletion(
    client, admin_token_headers, normal_user, db_session
):
    """The guard is keyed on the HOLD, not on quarantine — deliberately, as in #664.

    ``purge_media_file`` guards ``legal_hold`` only; the ``is_quarantined`` predicate
    lives on the unattended retention sweep, which must never destroy a file under
    review. An admin acting deliberately is a different actor: deleting an abusive
    *account* is the commonest reason its files were quarantined in the first place, so
    blocking on quarantine would turn a takedown into a shield against removal. Without
    this test, widening the guard to ``is_quarantined`` would look like an improvement.
    """
    owned = seed_owned_rows(db_session, normal_user)
    quarantined = db_session.query(MediaFile).filter(MediaFile.id == owned.media_file_id).one()
    quarantined.is_quarantined = True
    quarantined.quarantine_reason = "AUP review"
    db_session.commit()
    quarantined_id = int(quarantined.id)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is None
    assert db_session.query(MediaFile).filter(MediaFile.id == quarantined_id).first() is None


def test_the_refusal_counts_every_held_file(client, admin_token_headers, normal_user, db_session):
    """The count is measured, not a hardcoded 1 — the admin needs to know the scale.

    Two of the user's three files are held; the message and the structured field must
    both say two.
    """
    owned = seed_owned_rows(db_session, normal_user)
    first = db_session.query(MediaFile).filter(MediaFile.id == owned.media_file_id).one()
    _place_under_legal_hold(db_session, first)
    second = make_media_file(db_session, int(normal_user.id))
    _place_under_legal_hold(db_session, second)
    # A third, unheld file so the count cannot simply be "every file this user owns".
    make_media_file(db_session, int(normal_user.id))

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert detail["files_under_legal_hold"] == 2
    assert "2 files" in detail["message"]
    db_session.expire_all()
    assert db_session.query(MediaFile).filter(MediaFile.user_id == owned.user_id).count() == 3


def test_the_users_router_delete_is_refused_too(
    client, admin_token_headers, normal_user, db_session
):
    """``DELETE /api/users/{uuid}`` is the second caller and has no savepoint at all.

    It calls the same three cascade helpers with no ``begin_nested()`` and no
    ``except Exception``, so a guard that only lived inside ``_delete_user_media_files``
    would refuse here only *after* ``_delete_user_owned_records`` and
    ``_delete_user_speakers`` had already deleted rows in the request's session.
    """
    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)
    held_file = db_session.query(MediaFile).filter(MediaFile.id == owned.media_file_id).one()
    held_id = _place_under_legal_hold(db_session, held_file)

    response = client.delete(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is not None
    assert db_session.query(MediaFile).filter(MediaFile.id == held_id).first() is not None
    owned.assert_all_present(db_session)


def test_the_users_router_still_deletes_an_ordinary_user(
    client, admin_token_headers, normal_user, db_session
):
    """CONTROL for the second entry point: 204 and the data really goes."""
    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)

    response = client.delete(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is None
    assert owned.remaining(db_session) == {}
