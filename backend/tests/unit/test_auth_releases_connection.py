"""Authentication returns its pooled connection before the handler runs.

A request used to check its connection out at the user lookup and keep it, idle in a
transaction, until ``get_db`` closed the Session after the response was sent. Under load
every connection sat in that state while Postgres had a handful of active backends, and
the next request waited out the pool timeout (scale_foundations_plan.md section 2).

These run on a dedicated engine, not the ``db_session`` savepoint harness: that harness
always has a transaction open (it restarts its savepoint on every commit), so it cannot
show whether a transaction ended. The dedicated engine's pool is the measure: a checked-in
connection is one Postgres no longer sees idle in a transaction.

``Session.in_transaction()`` is not the measure. Re-attaching the kept user with
``Session.add()`` begins a new ORM-level transaction (``Session._before_attach`` autobegins,
SQLAlchemy 2.0.55) that holds no connection until the next statement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.auth.direct_auth import create_access_token
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import register_verifier
from app.auth.provider_registry import unregister_verifier
from app.db.base import engine as app_engine
from app.db.base import get_db
from app.models.user import User
from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


@pytest.fixture
def dedicated_engine() -> Iterator[Engine]:
    eng = create_engine(app_engine.url, pool_size=2, max_overflow=0)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def seeded_user(dedicated_engine: Engine) -> Iterator[User]:
    """A committed user (the probe's Session must see it), deleted afterwards."""
    factory = sessionmaker(bind=dedicated_engine, autoflush=False)
    with factory() as db:
        user = User(
            email=f"conn_release_{uuid.uuid4().hex[:8]}@example.com",
            full_name="Connection Release",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    try:
        yield user
    finally:
        with factory() as db:
            db.query(User).filter(User.id == user.id).delete()
            db.commit()


def _probe_app(dedicated_engine: Engine, dependency) -> tuple[FastAPI, dict]:
    factory = sessionmaker(bind=dedicated_engine, autoflush=False)
    seen: dict = {}

    def override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    probe = FastAPI()
    probe.dependency_overrides[get_db] = override_get_db

    @probe.get("/probe")
    def handler(current_user: User | None = Depends(dependency), db: Session = Depends(get_db)):
        pool = dedicated_engine.pool
        assert isinstance(pool, QueuePool)
        seen["checked_out"] = pool.checkedout()
        with count_statements(dedicated_engine, include_app_engine=False) as log:
            seen["email"] = current_user.email if current_user is not None else None
        seen["statements"] = log.count
        return {}

    return probe, seen


def _auth(user: User) -> dict[str, str]:
    token = create_access_token({"sub": str(user.uuid), "role": user.role})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "dependency",
    [get_current_active_user, get_current_user, get_optional_current_user],
    ids=["active_user", "current_user", "optional_user"],
)
def test_the_handler_starts_with_no_connection_held(dedicated_engine, seeded_user, dependency):
    probe, seen = _probe_app(dedicated_engine, dependency)

    with TestClient(probe) as client:
        response = client.get("/probe", headers=_auth(seeded_user))

    assert response.status_code == 200, response.text
    assert seen["checked_out"] == 0
    # The user stays usable: its loaded attributes were kept through the commit.
    assert seen["email"] == seeded_user.email
    assert seen["statements"] == 0


class _AcceptingVerifier:
    """External verifier (cloud seam) that claims one token for one identity."""

    def __init__(self, token: str, identity: ExternalIdentity) -> None:
        self.token = token
        self.identity = identity

    def verify(self, token: str, request) -> ExternalIdentity | None:
        return self.identity if token == self.token else None


@pytest.mark.parametrize(
    "dependency",
    [get_current_active_user, get_optional_current_user],
    ids=["active_user", "optional_user"],
)
def test_an_external_verifier_user_is_released_the_same_way(dedicated_engine, dependency):
    """The JIT-synced user from the cloud seam returns its connection too (plan 2.5)."""
    external_id = f"ext-{uuid.uuid4().hex[:12]}"
    email = f"{external_id}@example.com"
    identity = ExternalIdentity(provider="oidc", external_id=external_id, email=email)
    register_verifier("oidc", _AcceptingVerifier("external-token", identity))
    probe, seen = _probe_app(dedicated_engine, dependency)
    try:
        with TestClient(probe) as client:
            response = client.get("/probe", headers={"Authorization": "Bearer external-token"})
    finally:
        unregister_verifier("oidc")
        with sessionmaker(bind=dedicated_engine)() as db:
            db.query(User).filter(User.email == email).delete()
            db.commit()

    assert response.status_code == 200, response.text
    assert seen["email"] == email
    assert seen["checked_out"] == 0
    assert seen["statements"] == 0


def test_a_modified_kept_object_is_refused(db_session, normal_user):
    """A read transaction never commits a change to the object it keeps."""
    from app.db.session_utils import KeptObjectDirtyError
    from app.db.session_utils import end_read_transaction

    normal_user.full_name = "changed during authentication"

    with pytest.raises(KeptObjectDirtyError):
        end_read_transaction(db_session, normal_user)


def test_a_kept_object_survives_the_commit_without_a_reload(db_session, normal_user):
    """Control for the test above: an unmodified object is kept, loaded, and attached."""
    from app.db.session_utils import end_read_transaction

    end_read_transaction(db_session, normal_user)

    assert normal_user in db_session
    with count_statements(db_session.connection()) as log:
        assert normal_user.email.endswith("@example.com")
    assert log.count == 0, log.describe()
