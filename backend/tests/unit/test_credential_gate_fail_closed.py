"""``get_current_user``'s error paths must never invent a session (#284 A0.8).

Two controls live in the exception handling at the bottom of
``api/endpoints/auth/dependencies.get_current_user``, and one in the expiry gate:

1. ``os.environ.get("TESTING", ...) == "true" and not settings.is_hardened``.
   ``TESTING`` makes a database failure resolve to a **fabricated** authenticated user
   built from the token's UUID. Turn that ``and`` into an ``or`` and a *relaxed* real
   deployment — ``ENVIRONMENT=development`` on a real host, which is a supported way to
   run this app — fabricates an authenticated user whenever the database hiccups. Any
   holder of a syntactically valid token becomes ``test@example.com`` with a live
   session. ``settings.is_hardened`` is the fail-closed half; ``TESTING`` alone is an
   environment variable an attacker-influenced deployment can end up carrying.
2. ``except HTTPException: raise``. ``credentials_exception`` (401, unknown user) and the
   400 "Inactive user" are *authorization decisions*, not infrastructure errors — but
   they are ``HTTPException``s, so before this re-raise the broad handler below caught
   them and, under ``TESTING``, replaced the denial with the same fabricated user. Delete
   the two-line handler and "unknown user" and "deactivated user" both resolve to a valid
   session again.
3. The naive→aware coercion in ``_enforce_account_expiry``::

       if expires_at.tzinfo is None:
           expires_at = expires_at.replace(tzinfo=UTC)

   Delete those two lines and comparing naive to aware raises ``TypeError`` **inside a
   dependency**, i.e. an HTTP 500 on *every* request from any account that has an expiry
   set. The column reads back naive through some driver configurations, and the existing
   coverage in ``test_account_lifecycle.py`` only passes a naive *past* value — where the
   refusal is the expected outcome anyway. The uncovered case is the ordinary one: a
   contractor account whose expiry has **not** passed.

The existing ``test_handler_and_ws_hardening.py`` guards control 1 by counting substrings
in the source; these tests drive the real dependency instead, so they survive a refactor
and still fail on the mutation.
"""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_EXPIRED
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.core.config import settings
from tests.jwt_compat import jwt

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000ee"

DB_FAILURE = "database went away"


def _user(**overrides: Any) -> Any:
    """A ``User`` stand-in carrying what the credential and lifecycle paths read."""
    attrs: dict[str, Any] = {
        "id": 11,
        "uuid": UUID(USER_UUID),
        "email": "person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "must_change_password": False,
        "account_expires_at": None,
        "external_org_id": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request(path: str = "/api/files") -> Any:
    return SimpleNamespace(
        headers={"User-Agent": "pytest"},
        cookies={},
        state=SimpleNamespace(),
        client=SimpleNamespace(host="10.0.0.1"),
        url=SimpleNamespace(path=path),
        scope={"route": SimpleNamespace(path=path)},
    )


def _token() -> str:
    """A structurally valid, unexpired access token for ``USER_UUID``."""
    return jwt.encode(
        {
            "sub": USER_UUID,
            "type": TOKEN_TYPE_ACCESS,
            "jti": "fail-closed-test-jti",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


class _Query:
    def __init__(self, result: Any):
        self._result = result

    def filter(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def first(self) -> Any:
        return self._result


class _DB:
    """Minimal ``Session`` stand-in returning one canned row."""

    def __init__(self, row: Any = None):
        self._row = row

    def query(self, *_a: Any, **_k: Any) -> _Query:
        return _Query(self._row)


class _BrokenDB:
    """The database being unavailable — the ONLY thing the mock-user shortcut is for."""

    def query(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError(DB_FAILURE)


@pytest.fixture(autouse=True)
def _no_revocation_lookup(monkeypatch):
    """Take the revocation blacklist out of the picture.

    It is a separate control with its own suite
    (``test_access_token_revocation_epoch.py`` / ``test_revocation_fails_closed.py``),
    and with it enabled a broken database would be answered by the revocation
    fallback's own fail-closed 401 before these paths were ever reached — which would
    make these tests pass for the wrong reason.
    """
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", False)


@pytest.fixture
def testing_flag(monkeypatch):
    """The suite's own ``TESTING=True``, set explicitly so the tests do not depend on it."""
    monkeypatch.setenv("TESTING", "True")


def _authenticate(db: Any) -> Any:
    return get_current_user(request=_request(), token=_token(), db=cast(Any, db))


class TestTheFabricatedUserIsGatedOnBothConditions:
    """Consequence prevented: a relaxed real deployment minting an authenticated session
    out of a database error for anyone holding a well-formed token."""

    def test_a_hardened_deployment_never_fabricates_a_user(self, monkeypatch, testing_flag):
        """``TESTING`` is present and the database is down — and it still fails closed."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")

        with pytest.raises(RuntimeError, match=DB_FAILURE):
            _authenticate(_BrokenDB())

    def test_a_relaxed_deployment_without_the_flag_never_fabricates_a_user(self, monkeypatch):
        """The other half of the ``and``: relaxed alone must not be enough either."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        monkeypatch.setenv("TESTING", "False")

        with pytest.raises(RuntimeError, match=DB_FAILURE):
            _authenticate(_BrokenDB())

    def test_the_shortcut_still_works_where_it_is_meant_to(self, monkeypatch, testing_flag):
        """The control. Without this the two tests above would pass just as well if the
        shortcut had been deleted outright, and this suite would stop measuring the gate."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")

        fabricated = _authenticate(_BrokenDB())

        assert fabricated.email == "test@example.com"

    def test_the_fabricated_user_is_never_privileged(self, monkeypatch, testing_flag):
        """It exists to let a test proceed, not to hand anyone the admin surface."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")

        fabricated = _authenticate(_BrokenDB())

        assert fabricated.is_superuser is False


class TestAuthorizationDecisionsAreNeverReplacedByAFabricatedUser:
    """Consequence prevented: an unknown or deactivated account resolving to a valid
    session because its refusal is an ``HTTPException`` that the broad handler caught."""

    def test_an_unknown_user_is_refused_even_under_the_shortcut(self, monkeypatch, testing_flag):
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")

        with pytest.raises(HTTPException) as exc:
            _authenticate(_DB(None))

        assert exc.value.status_code == 401

    def test_a_deactivated_user_is_refused_even_under_the_shortcut(self, monkeypatch, testing_flag):
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")

        with pytest.raises(HTTPException) as exc:
            _authenticate(_DB(_user(is_active=False)))

        assert exc.value.status_code == 400

    def test_a_valid_active_user_is_returned_unchanged(self, monkeypatch, testing_flag):
        """The control: the two refusals above are decisions, not a broken happy path."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        user = _user()

        assert _authenticate(_DB(user)) is user


class TestNaiveAccountExpiryDoesNotBreakEveryRequest:
    """Consequence prevented: an HTTP 500 on every request of every account that has an
    ``account_expires_at`` — the naive value read back from Postgres cannot be compared
    to an aware ``now`` without the coercion.

    ``db`` is left at its default (an unresolved ``Depends``) exactly as
    ``test_account_lifecycle.py`` does: the approval and banner gates both short-circuit
    on an object with no ``query``, so this exercises the expiry gate alone.
    """

    def test_a_naive_future_expiry_still_allows_access(self):
        """The uncovered case: a time-boxed account that has NOT expired yet."""
        naive_future = (datetime.now(UTC) + timedelta(days=30)).replace(tzinfo=None)
        user = _user(account_expires_at=naive_future)

        assert get_current_active_user(request=_request(), current_user=user) is user

    def test_a_naive_expiry_far_in_the_future_still_allows_access(self):
        naive_future = (datetime.now(UTC) + timedelta(days=365)).replace(tzinfo=None)
        user = _user(account_expires_at=naive_future)

        assert get_current_active_user(request=_request(), current_user=user) is user

    def test_a_naive_past_expiry_is_refused(self, monkeypatch):
        """The other side of the same coercion: it must still deny, not crash."""
        audited: list[dict] = []
        monkeypatch.setattr(deps_module.audit_logger, "log", lambda **kw: audited.append(kw))
        naive_past = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(), current_user=_user(account_expires_at=naive_past)
            )

        assert cast(dict, exc.value.detail)["code"] == ERROR_CODE_ACCOUNT_EXPIRED

    def test_an_aware_future_expiry_still_allows_access(self):
        """The control: the coercion must not have changed the aware path's answer."""
        user = _user(account_expires_at=datetime.now(UTC) + timedelta(days=30))

        assert get_current_active_user(request=_request(), current_user=user) is user
