"""No pooled connection is held while a login hashes the password (scale_foundations_plan.md 5.6).

PBKDF2 at 600,000 iterations is the slowest step of a login. ``direct_authenticate_user``
held its raw connection, idle in a transaction, across it, and the request's own Session
held another from the account lookup before it, so every concurrent sign-in pinned two
connections for the length of a hash. A wrong password then hashed a second time on the
ORM fallback, with the Session's connection still out.

Like ``test_auth_releases_connection.py`` this runs on a dedicated engine, not the
``db_session`` savepoint harness (which always has a transaction open): the dedicated
pool's ``checkedout()`` during each hash is the measure.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

import app.db.base as db_base
from app.auth import direct_auth
from app.core import security
from app.core.security import get_password_hash
from app.db.base import engine as app_engine
from app.db.base import get_db
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User

pytestmark = pytest.mark.unit

#: Generated per run: the user is committed for real, so its credential is never a constant.
PASSWORD = f"Release-{uuid.uuid4().hex}-1!"


@pytest.fixture
def dedicated_engine() -> Iterator[Engine]:
    eng = create_engine(app_engine.url, pool_size=3, max_overflow=0)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def committed_user(dedicated_engine: Engine) -> Iterator[User]:
    """A committed local user (the password check reads on its own connection)."""
    factory = sessionmaker(bind=dedicated_engine, autoflush=False)
    with factory() as db:
        user = User(
            email=f"login_release_{uuid.uuid4().hex[:8]}@example.com",
            full_name="Login Release",
            hashed_password=get_password_hash(PASSWORD),
            is_active=True,
            is_superuser=False,
            role="user",
            auth_type="local",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    try:
        yield user
    finally:
        with factory() as db:
            db.query(RefreshToken).filter(RefreshToken.user_id == user.id).delete()
            db.query(User).filter(User.id == user.id).delete()
            db.commit()


@pytest.fixture
def hashes(dedicated_engine, monkeypatch) -> Iterator[list[tuple[str, int]]]:
    """``(path, connections checked out)`` for every password hash a login computes."""
    pool = dedicated_engine.pool
    assert isinstance(pool, QueuePool)
    seen: list[tuple[str, int]] = []
    real_direct = direct_auth.verify_and_update_password
    real_orm = security.verify_password

    def _direct(password, hashed):
        seen.append(("direct", pool.checkedout()))
        return real_direct(password, hashed)

    def _orm(password, hashed):
        seen.append(("orm", pool.checkedout()))
        return real_orm(password, hashed)

    monkeypatch.setattr(direct_auth, "verify_and_update_password", _direct)
    monkeypatch.setattr(security, "verify_password", _orm)
    # The suite sets TESTING, which routes login to an ORM-only test authenticator.
    monkeypatch.setenv("TESTING", "False")
    # The password check takes its connection from app.db.base.engine; point it here.
    monkeypatch.setattr(db_base, "engine", dedicated_engine)

    factory = sessionmaker(bind=dedicated_engine, autoflush=False)

    def override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield seen
    finally:
        app.dependency_overrides.pop(get_db, None)


def _login(user: User, password: str):
    # No ``with``: the app's startup (storage, search) is not what this measures.
    client = TestClient(app)
    return client.post("/api/auth/token", data={"username": user.email, "password": password})


def test_a_successful_login_hashes_with_no_connection_held(committed_user, hashes):
    response = _login(committed_user, PASSWORD)

    assert response.status_code == 200, response.text
    assert hashes == [("direct", 0)]


def test_a_wrong_password_hashes_with_no_connection_held_on_either_path(committed_user, hashes):
    response = _login(committed_user, "not-the-password")

    assert response.status_code == 401, response.text
    assert [path for path, _held in hashes] == ["direct", "orm"]
    assert hashes == [("direct", 0), ("orm", 0)]
