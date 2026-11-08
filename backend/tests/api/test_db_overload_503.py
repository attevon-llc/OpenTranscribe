"""Database overload is a 503 with Retry-After, logged once (scale_foundations_plan.md 2.4.3).

#1337 M2 #3/#7: at pool exhaustion the API answered 500 after the pool timeout and logged
each one as a ~200-line chained traceback through the four BaseHTTPMiddleware layers
(718,590 log lines in one run), CPU the saturated process did not have.

These drive the real ``app`` (its exception handlers and middleware); the database side is
a tiny dedicated engine so a pool can be exhausted or a statement cancelled without
touching the shared test connection.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.api.endpoints.auth.dependencies import get_current_active_user
from app.auth.direct_auth import create_access_token
from app.core.config import settings
from app.db.base import engine as app_engine
from app.db.base import get_db
from app.main import app

ROUTE = "/api/users/me"
OVERLOAD_LOGGER = "app.api.overload_response"


@pytest.fixture
def tiny_engine() -> Iterator[Engine]:
    """One connection and a 0.1 s pool wait."""
    eng = create_engine(app_engine.url, pool_size=1, max_overflow=0, pool_timeout=0.1)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def client(tiny_engine) -> Iterator[TestClient]:
    factory = sessionmaker(bind=tiny_engine, autoflush=False)

    def override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)


def _token_headers() -> dict[str, str]:
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "user"})
    return {"Authorization": f"Bearer {token}"}


def _override_user(dependency) -> None:
    app.dependency_overrides[get_current_active_user] = dependency


def assert_server_busy(response, caplog) -> None:
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == "2"
    assert response.json() == {"detail": "Server busy, try again shortly", "code": "server_busy"}
    overload = [r for r in caplog.records if r.name == OVERLOAD_LOGGER]
    assert len(overload) == 1, [r.getMessage() for r in caplog.records]
    assert overload[0].levelno == logging.WARNING
    assert overload[0].exc_info is None
    assert ROUTE in overload[0].getMessage()
    tracebacks = [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]
    assert tracebacks == []


def test_pool_exhaustion_during_authentication_is_a_503(client, tiny_engine, caplog):
    caplog.set_level(logging.DEBUG)
    with tiny_engine.connect():  # hold the only connection
        response = client.get(ROUTE, headers=_token_headers())

    assert_server_busy(response, caplog)
    assert not [r for r in caplog.records if "Error retrieving user" in r.getMessage()]


def test_a_cancelled_statement_is_a_503(client, caplog):
    def cancels(db: Session = Depends(get_db)):
        db.execute(text("SET LOCAL statement_timeout = 1"))
        db.execute(text("SELECT pg_sleep(1)"))

    _override_user(cancels)
    caplog.set_level(logging.DEBUG)
    response = client.get(ROUTE)

    assert_server_busy(response, caplog)
    assert "statement_timeout" in next(
        r.getMessage() for r in caplog.records if r.name == OVERLOAD_LOGGER
    )


def test_a_request_past_its_deadline_is_a_503(client, tiny_engine, caplog, monkeypatch):
    from app.db.request_deadline import install_request_deadline

    install_request_deadline(tiny_engine)  # the app engine has it; this one is the test's
    # One nanosecond: the deadline has passed by the time any statement runs.
    monkeypatch.setattr(settings, "REQUEST_DEADLINE_SECONDS", 1e-9)

    def queries(db: Session = Depends(get_db)):
        db.execute(text("SELECT 1"))

    _override_user(queries)
    caplog.set_level(logging.DEBUG)
    response = client.get(ROUTE)

    assert_server_busy(response, caplog)


def test_a_5xx_raised_from_an_overload_is_a_503(client, tiny_engine, caplog):
    """A handler that maps every failure to a 500 still answers overload as 503."""

    def maps_to_500(db: Session = Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Internal server error") from exc

    _override_user(maps_to_500)
    caplog.set_level(logging.DEBUG)
    with tiny_engine.connect():
        response = client.get(ROUTE)

    assert_server_busy(response, caplog)


def test_an_unrelated_operational_error_stays_a_500(client):
    """Negative control: only the three overload classes become 503."""

    def broken():
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    _override_user(broken)
    response = client.get(ROUTE)

    assert response.status_code == 500
    assert "Retry-After" not in response.headers


def test_an_ordinary_500_and_a_4xx_from_an_overload_are_unchanged(client, tiny_engine):
    """Control: the HTTPException handler only rewrites a 5xx caused by an overload."""

    def plain_500():
        raise HTTPException(status_code=500, detail="Internal server error")

    _override_user(plain_500)
    assert client.get(ROUTE).status_code == 500

    def overload_as_404(db: Session = Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc

    _override_user(overload_as_404)
    with tiny_engine.connect():
        response = client.get(ROUTE)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
