"""A failed account deletion must leave the account exactly as it was.

Both delete routes run the same cascade: owned records, then speakers, then media
files, then the ``user`` row. Each pass is a bulk statement, so a failure in the third
pass finds the first two already applied in the request's session. ``DELETE
/api/admin/users/{uuid}`` wrapped the passes in a savepoint and rolled them back;
``DELETE /api/users/{uuid}`` had no savepoint, so the same failure left the session
holding the deletes of rows the caller was told were not deleted.

The route is driven against real rows through the savepoint-isolated ``db_session``;
the media-file pass is replaced by one that raises, and every seeded table must still
hold its rows afterwards. The admin route is not parametrized in: its handler ends a
failure with ``db.rollback()``, which in this harness discards the test's own seeded
rows too, so the same observation cannot be made there.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.models.user import User
from tests.user_owned_rows import seed_owned_rows


class _InjectedCascadeError(Exception):
    """Raised by the stand-in media-file pass; nothing in the app catches it by name."""


@pytest.fixture
def external_purges(monkeypatch) -> list[object]:
    """Record any object-storage/OpenSearch purge, which must never run here."""
    import app.services.account_purge as ap

    calls: list[object] = []
    monkeypatch.setattr(ap, "purge_account_external_copies", lambda plan: calls.append(plan))
    return calls


@pytest.fixture
def failing_media_pass(monkeypatch) -> list[int]:
    """Make the third cascade pass raise, after the first two have run."""
    import app.services.user_deletion_service as user_deletion_service

    calls: list[int] = []

    def _boom(db, user_id: int) -> None:
        calls.append(user_id)
        raise _InjectedCascadeError("media-file pass failed")

    monkeypatch.setattr(user_deletion_service, "_delete_user_media_files", _boom)
    return calls


def test_a_failed_cascade_through_the_users_route_deletes_nothing(
    client,
    admin_token_headers,
    normal_user,
    db_session,
    failing_media_pass,
    external_purges,
):
    """``client`` is requested for the ``get_db`` override it installs on ``app``.

    The request goes through a second client that turns the unhandled error into the
    500 a real caller sees, instead of re-raising it into the test.
    """
    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)
    seeded = owned.counts(db_session)
    non_raising = TestClient(app, raise_server_exceptions=False)

    response = non_raising.delete(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, response.text
    assert failing_media_pass == [owned.user_id], "the injected failure never fired"
    assert external_purges == []
    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is not None
    assert owned.counts(db_session) == seeded
