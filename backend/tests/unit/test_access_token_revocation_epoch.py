"""The per-user revocation epoch is the ONLY kill switch for a live access token.

Access tokens are stateless: nothing durable records their JTIs, so "revoke all of this
user's tokens" can only blacklist their *refresh* tokens. ``stamp_user_revocation_epoch``
closes that gap by recording "nothing issued before now is acceptable", and
``get_current_user`` feeds each token's ``iat`` claim into the check:

    issued_at=payload.get("iat")   # dependencies.py

Mutate that claim name (or drop the argument) and ``_issued_before_user_epoch`` receives
``None``, returns ``False`` for everything, and the epoch stops rejecting anything. The
consequence is that **logout-all, a password change, a password reset, an admin session
termination and a role change all stop taking effect on the current access token** — it
keeps working for its full lifetime. The refresh token is dead, so it looks like the
revocation worked; the still-live access token is the part nobody sees.

Nothing in the suite covered the epoch through the dependency before this file.

Runs against a real :class:`~app.auth.session.InMemoryStore` behind a private
``TokenService``, so it needs no Redis and cannot be polluted by (or pollute) the shared
module-level service. ``_degraded`` stays ``False`` because the store is injected, which
matters: the degraded path deliberately skips the epoch and answers from Postgres
instead (``tests/unit/test_revocation_fails_closed.py`` covers that half).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import dependencies as deps_module
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.session import InMemoryStore
from app.auth.token_service import REVOKED_TOKEN_PREFIX
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.core.config import settings
from app.models.user import User
from tests.jwt_compat import jwt

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000cc"
JTI = "epoch-test-jti"


# ── fakes (structural stand-ins, as in tests/unit/test_account_lifecycle.py) ─────


class _FakeQuery:
    """Returns one canned row regardless of the filter — the filters are SQL, not logic."""

    def __init__(self, result: Any):
        self._result = result

    def filter(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def first(self) -> Any:
        return self._result

    def all(self) -> list[Any]:
        return [] if self._result is None else [self._result]


class _FakeDB:
    """Minimal ``Session`` stand-in keyed by model class."""

    def __init__(self, rows: dict | None = None):
        self.rows = rows or {}
        self.commits = 0

    def query(self, model: Any) -> _FakeQuery:
        return _FakeQuery(self.rows.get(model))

    def commit(self) -> None:
        self.commits += 1


def _user(**overrides: Any) -> Any:
    attrs: dict[str, Any] = {
        "id": 7,
        "uuid": UUID(USER_UUID),
        "email": "person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "external_org_id": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request() -> Any:
    """A Request stand-in carrying only what ``get_current_user`` touches."""
    return SimpleNamespace(
        headers={},
        cookies={},
        state=SimpleNamespace(),
        client=SimpleNamespace(host="10.0.0.1"),
        url=SimpleNamespace(path="/api/files"),
        scope={},
    )


def _token(*, issued_at: int, jti: str = JTI, subject: str = USER_UUID) -> str:
    """An otherwise-valid access token with a chosen ``iat``.

    Minted through ``tests/jwt_compat.py`` rather than the app's own helper because
    ``direct_auth.create_access_token`` stamps ``iat`` itself — the claim under test has
    to be the test's choice, not the clock's.
    """
    return jwt.encode(
        {
            "sub": subject,
            "type": TOKEN_TYPE_ACCESS,
            "jti": jti,
            "iat": issued_at,
            "exp": int(time.time()) + 600,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


@pytest.fixture
def revocation(monkeypatch) -> TokenService:
    """A ``TokenService`` on a real in-memory store, wired into the dependency."""
    service = TokenService()
    service._store = InMemoryStore()
    monkeypatch.setattr(deps_module, "token_service", service)
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    return service


def _epoch_of(service: TokenService, user_uuid: str = USER_UUID) -> int:
    """The epoch the service actually recorded, read back from its store.

    Read rather than recomputed from ``time.time()``: the comparison is
    ``issued_at < epoch`` to the second, so a test that guessed the epoch would be
    racing the clock.
    """
    marker = service.store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}")
    assert marker is not None, "stamp_user_revocation_epoch wrote nothing"
    return int(marker)


def _epoch_stamped_at(service: TokenService, *, seconds_ago: int) -> None:
    """Record a revocation epoch in the past, under the key the service itself writes.

    Needed because a token cannot be minted *after* ``now``: joserfc's claims registry
    rejects a future ``iat`` outright ("The token was issued in the future"), so the
    only way to express "revoked, then re-authenticated" is to age the epoch rather
    than post-date the token. That is also the real sequence — the epoch is stamped at
    logout/password-change time and the new session is issued afterwards.
    """
    service.store.set(
        f"{USER_REVOCATION_EPOCH_PREFIX}{USER_UUID}",
        str(int(time.time()) - seconds_ago),
        ex=600,
    )


def _authenticate(token: str, user: Any) -> Any:
    return deps_module.get_current_user(
        request=_request(), token=token, db=cast(Any, _FakeDB({User: user}))
    )


class TestATokenPredatingTheEpochIsRefused:
    """Consequence prevented: logout-all / password change / role change leaving the
    caller's current access token usable until it expires on its own."""

    def test_a_token_issued_before_the_epoch_is_refused(self, revocation):
        user = _user()
        revocation.stamp_user_revocation_epoch(USER_UUID)
        stale = _token(issued_at=_epoch_of(revocation) - 60)

        with pytest.raises(HTTPException) as exc:
            _authenticate(stale, user)

        assert exc.value.status_code == 401

    def test_the_refusal_is_the_generic_credentials_error(self, revocation):
        """A distinct message would tell an attacker their target just re-authenticated."""
        user = _user()
        revocation.stamp_user_revocation_epoch(USER_UUID)
        stale = _token(issued_at=_epoch_of(revocation) - 60)

        with pytest.raises(HTTPException) as exc:
            _authenticate(stale, user)

        assert exc.value.detail == "Could not validate credentials"

    def test_a_token_issued_after_the_epoch_is_accepted(self, revocation):
        """The control: re-authenticating after the revocation must work immediately."""
        user = _user()
        _epoch_stamped_at(revocation, seconds_ago=120)
        fresh = _token(issued_at=int(time.time()))

        assert _authenticate(fresh, user) is user

    def test_a_token_minted_in_the_same_second_as_the_epoch_survives(self, revocation):
        """``<`` not ``<=``: the token a password change just re-issued must not be
        killed by its own stamp — that is an instant logout loop for the user who just
        changed their password."""
        user = _user()
        revocation.stamp_user_revocation_epoch(USER_UUID)
        same_second = _token(issued_at=_epoch_of(revocation))

        assert _authenticate(same_second, user) is user

    def test_without_an_epoch_an_old_token_is_still_accepted(self, revocation):
        """The other control: the epoch must only bite when something stamped it."""
        user = _user()
        old = _token(issued_at=int(time.time()) - 3600)

        assert _authenticate(old, user) is user

    def test_another_users_epoch_does_not_refuse_this_token(self, revocation):
        """The epoch is per user. A shared key would sign the whole deployment out
        every time one person logged out of all their devices."""
        user = _user()
        revocation.stamp_user_revocation_epoch("019ec90a-1b2c-7def-8000-0000000000dd")
        token = _token(issued_at=int(time.time()) - 3600)

        assert _authenticate(token, user) is user


class TestRevokingSessionsStampsTheEpoch:
    """Consequence prevented: the epoch never being written, which is the same outcome
    from the other end — ``revoke_all_user_tokens_in_transaction`` is what every
    credential/privilege change funnels through."""

    def test_revoking_all_sessions_stamps_the_epoch(self, revocation):
        before = int(time.time())

        revocation.revoke_all_user_tokens_in_transaction(
            cast(Any, _FakeDB()), user_id=7, user_uuid=USER_UUID
        )

        # "Present" is not the contract — the epoch means "nothing issued before NOW",
        # so a stamp of 0 (or of some stale value) is a kill switch that kills nothing
        # while reading as armed.
        assert _epoch_of(revocation) >= before
        assert _epoch_of(revocation) <= int(time.time())

    def test_the_stamped_epoch_refuses_a_token_issued_a_moment_earlier(self, revocation):
        """End to end through the dependency: revoke, then the live token stops working."""
        user = _user()
        before = int(time.time()) - 5

        revocation.revoke_all_user_tokens_in_transaction(
            cast(Any, _FakeDB()), user_id=7, user_uuid=USER_UUID
        )

        with pytest.raises(HTTPException) as exc:
            _authenticate(_token(issued_at=before), user)

        assert exc.value.status_code == 401


class TestTheBlacklistStillApplies:
    """Consequence prevented: the epoch check replacing the JTI blacklist rather than
    complementing it — a single logout must still kill the access token it named."""

    def test_a_blacklisted_jti_is_refused(self, revocation):
        user = _user()
        revocation.store.set(f"{REVOKED_TOKEN_PREFIX}{JTI}", "revoked", ex=600)

        with pytest.raises(HTTPException) as exc:
            _authenticate(_token(issued_at=int(time.time())), user)

        assert exc.value.status_code == 401

    def test_a_different_jti_is_unaffected(self, revocation):
        user = _user()
        revocation.store.set(f"{REVOKED_TOKEN_PREFIX}some-other-jti", "revoked", ex=600)

        assert _authenticate(_token(issued_at=int(time.time())), user) is user
