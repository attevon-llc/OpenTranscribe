"""The OIDC ID token is kept on the session row, encrypted, and never in a cookie.

RP-Initiated Logout 1.0 needs the ID token as `id_token_hint`, so it has to outlive the
callback that received it. There are two places to put it and the choice is a security
decision, not a storage detail: the reference implementation everyone copies defaults to
a **browser cookie**, and its own documentation calls that unsafe. An ID token carries
the user's full identity claim set, so a cookie exposes it to anything that reaches the
cookie jar and keeps it alive past the session that justified holding it.

We take the other branch. `refresh_token.oidc_id_token` (v378): encrypted at rest, and
its lifetime *is* the session's — rotation, revocation and the concurrent-session cap
already delete these rows, so nothing extra has to remember to clean it up.

These tests fail if that wiring is removed, which is the point: storage that round-trips
is not the property worth asserting; "the callback actually stores it, encrypted, on the
row it belongs to, and no response ever carries it" is.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.fixture
def session_row(db_session):
    """A real session row for a real user, rolled back by the fixture."""
    from sqlalchemy import text

    from app.auth.token_service import token_service

    user = db_session.execute(
        text('SELECT id, uuid, role FROM "user" ORDER BY id LIMIT 1')
    ).one_or_none()
    if user is None:
        pytest.skip("no user rows to hang a session off")

    _token, row = token_service.create_refresh_token(
        db=db_session,
        user_id=user.id,
        user_uuid=str(user.uuid),
        role=str(user.role),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    yield row
    db_session.rollback()


def test_the_column_exists_on_the_session_row(db_session):
    from sqlalchemy import inspect as sa_inspect

    columns = {c["name"] for c in sa_inspect(db_session.connection()).get_columns("refresh_token")}
    assert "oidc_id_token" in columns


def test_a_stored_id_token_is_encrypted_and_recoverable(db_session, session_row):
    """Ciphertext on the row, plaintext only after an explicit decrypt."""
    from app.api.endpoints.auth.oidc import _store_session_id_token
    from app.auth.mfa import MFAService

    # Not a credential: three literal dot-separated words shaped like a JWT.
    id_token = "header.eyJzdWIiOiJ1c2VyLTEifQ.signature"  # gitleaks:allow
    _store_session_id_token(db_session, session_row, id_token)

    stored = session_row.oidc_id_token
    assert stored, "the id_token was not stored on the session row"
    assert stored != id_token, "the id_token must not be stored in the clear"
    assert MFAService.decrypt_totp_secret(stored) == id_token


def test_the_callback_stores_it_on_the_session_it_just_created(db_session, session_row):
    """AST-level pin: the callback must call the storer with the row it created.

    Without this the storage helper can keep working perfectly while nothing calls it —
    the "setting written, never read" shape this branch exists to eliminate.
    """
    from app.api.endpoints.auth.oidc import oidc_callback

    source = inspect.getsource(oidc_callback)
    assert "_store_session_id_token(db, session_row, tokens.id_token)" in source
    assert "create_refresh_token" in source
    # The storer must be reached from the callback, not from a cookie helper.
    assert "set_cookie" not in source


def test_no_response_helper_ever_puts_the_id_token_in_a_cookie():
    """The decision, enforced: `auth/cookies.py` has no id-token setter."""
    from app.auth import cookies

    source = inspect.getsource(cookies)
    assert "id_token" not in source, (
        "an ID token must never reach app/auth/cookies.py — it belongs on the session "
        "row (refresh_token.oidc_id_token), not in the browser"
    )


def test_deleting_the_session_takes_the_id_token_with_it(db_session, session_row):
    """The lifetime claim, exercised rather than asserted in a comment."""
    from sqlalchemy import text

    from app.api.endpoints.auth.oidc import _store_session_id_token

    _store_session_id_token(db_session, session_row, "header.payload.signature")
    row_id = session_row.id

    db_session.execute(text("DELETE FROM refresh_token WHERE id = :i"), {"i": row_id})
    remaining = db_session.execute(
        text("SELECT count(*) FROM refresh_token WHERE oidc_id_token IS NOT NULL AND id = :i"),
        {"i": row_id},
    ).scalar()
    assert remaining == 0


def test_a_non_oidc_session_leaves_the_column_null(session_row):
    """Every local/LDAP/PKI session must not carry one."""
    assert session_row.oidc_id_token is None
