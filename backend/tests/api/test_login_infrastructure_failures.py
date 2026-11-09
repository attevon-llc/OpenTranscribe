"""The password login under a database that cannot answer (scale_foundations_plan.md 5.6).

``direct_authenticate_user`` caught every exception and returned ``None``, so a pool timeout,
a cancelled statement or a database that was down read as **bad credentials**. Login then
fell back to the ORM check: a wrong password answered 401 and a right one signed in on a
second connection. Under load the SPA met the 401s by signing in again (#1337 M2: 1,000-3,800
401s per contended run). Now the failure is raised and the endpoint answers 503 with
``Retry-After``, records no lockout attempt (no password was checked), and the ordinary
outcomes are unchanged: a wrong password is still a counted 401.

Real endpoint, real database (the ``client`` savepoint harness), production authenticator;
only the failure itself is injected, at the connection the password check opens. Under the
harness that connection cannot see the uncommitted fixture user, so a healthy check falls
through to the ORM password check, which is what answers the controls here.
"""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from psycopg2 import OperationalError as PsycopgOperationalError
from psycopg2 import errors as pg_errors
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.endpoints.auth import login as login_module
from app.auth import direct_auth
from app.core.config import settings

URL = "/api/auth/token"


@pytest.fixture(autouse=True)
def production_authenticator(client, monkeypatch):
    """The suite sets ``TESTING``, which routes login to an ORM-only test authenticator;
    the password check under test is the production one. Cleared after ``client`` has
    started the app, whose startup reads it too."""
    monkeypatch.setenv("TESTING", "False")


@pytest.fixture
def attempts(monkeypatch) -> list[tuple[str, bool]]:
    """Every lockout attempt the login records, still recorded for real."""
    recorded: list[tuple[str, bool]] = []
    real = login_module.check_and_record_attempt

    def _recording(identifier, success, exempt_from_lockout=False):
        recorded.append((identifier, success))
        return real(identifier, success=success, exempt_from_lockout=exempt_from_lockout)

    monkeypatch.setattr(login_module, "check_and_record_attempt", _recording)
    return recorded


def _fail_the_password_check_connection(monkeypatch, error: Exception) -> None:
    def _raises():
        raise error

    monkeypatch.setattr(direct_auth, "get_db_connection", _raises)


def _login(client, user, password: str):
    return client.post(URL, data={"username": user.email, "password": password})


_POOL_TIMEOUT = PoolTimeoutError("QueuePool limit of size 1 overflow 0 reached")
_STATEMENT_TIMEOUT = OperationalError(
    "SELECT ...", {}, pg_errors.QueryCanceled("canceling statement due to statement timeout")
)
_DATABASE_DOWN = OperationalError(
    "connect", {}, PsycopgOperationalError("connection to server failed: Connection refused")
)


@pytest.mark.parametrize("password", ["password123", "not-the-password"], ids=["right", "wrong"])
@pytest.mark.parametrize(
    "error",
    [_POOL_TIMEOUT, _STATEMENT_TIMEOUT],
    ids=["pool_timeout", "statement_timeout"],
)
def test_an_overload_during_the_password_check_is_a_503_and_is_not_counted(
    client, normal_user, attempts, monkeypatch, error, password
):
    _fail_the_password_check_connection(monkeypatch, error)

    response = _login(client, normal_user, password)

    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(settings.DB_OVERLOAD_RETRY_AFTER_SECONDS)
    assert response.json()["code"] == "server_busy"
    assert not response.cookies.get("access_token")
    assert attempts == [], "no password was checked, so no attempt may be counted"


@pytest.mark.parametrize("password", ["password123", "not-the-password"], ids=["right", "wrong"])
def test_a_database_that_cannot_be_reached_is_a_503_and_is_not_counted(
    client, normal_user, attempts, monkeypatch, password
):
    _fail_the_password_check_connection(monkeypatch, _DATABASE_DOWN)

    response = _login(client, normal_user, password)

    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(settings.DB_OVERLOAD_RETRY_AFTER_SECONDS)
    assert response.json()["detail"] == login_module.SIGN_IN_UNAVAILABLE_DETAIL
    assert attempts == []


def test_a_wrong_password_is_still_a_counted_401(client, normal_user, attempts):
    """Control: with the database answering, nothing about a bad password changed."""
    response = _login(client, normal_user, "not-the-password")

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Incorrect username or password"
    assert "Retry-After" not in response.headers
    assert len(attempts) == 1
    assert attempts[0][1] is False


def test_a_nonexistent_account_is_still_a_counted_401(client, attempts):
    """Control: enumeration protection — an unknown account gets the wrong-password answer."""
    response = client.post(
        URL, data={"username": "nobody-infra-check@example.com", "password": "whatever-1"}
    )

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Incorrect username or password"
    assert [success for _identifier, success in attempts] == [False]


def test_an_overload_in_the_lockout_account_lookup_is_a_503(
    client, normal_user, attempts, monkeypatch
):
    """The lookup's own fail-secure refusal is for a broken query, not a busy database."""

    class _OverloadedQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            raise _POOL_TIMEOUT

    class _OverloadedSession:
        def query(self, *_args, **_kwargs):
            return _OverloadedQuery()

    real = login_module._resolve_lockout_account
    monkeypatch.setattr(
        login_module,
        "_resolve_lockout_account",
        lambda db, username, uuid_str: real(cast(Any, _OverloadedSession()), username, uuid_str),
    )

    response = _login(client, normal_user, "not-the-password")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "server_busy"
    assert response.headers["Retry-After"] == str(settings.DB_OVERLOAD_RETRY_AFTER_SECONDS)
    assert attempts == []


def test_the_unresolved_account_refusal_carries_retry_after(client, normal_user, monkeypatch):
    """A lookup that fails for another reason keeps its 503, now with ``Retry-After``."""

    def _broken(db, username, user_uuid_str):
        raise login_module.AccountLookupError

    monkeypatch.setattr(login_module, "_resolve_lockout_account", _broken)

    response = _login(client, normal_user, "password123")  # the normal_user fixture's

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == login_module.SIGN_IN_UNAVAILABLE_DETAIL
    assert response.headers["Retry-After"] == str(settings.DB_OVERLOAD_RETRY_AFTER_SECONDS)
