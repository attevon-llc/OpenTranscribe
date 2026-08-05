"""Token revocation must not silently weaken when Redis is unavailable (issue #324).

`TokenService.store` falls back to a per-process `InMemoryStore` when Redis cannot
be reached. That store is empty in a fresh process and is never shared between
replicas, so trusting it answers "not revoked" to *everything*: on multi-replica
AWS a token revoked on one replica still passes on every other, and "log out all
devices" / password-reset revocation silently stop meaning anything.

Redis here is a cache, not the system of record — `refresh_token.revoked_at` is
durable and shared. So the degraded path consults the database, and denies when it
cannot. These tests pin that contract.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.auth.token_service import TokenService


@pytest.mark.unit
class TestRevocationFailsClosed:
    """When Redis is gone, never answer "not revoked" from per-process memory."""

    def test_degraded_without_db_denies(self):
        """No database session while degraded must deny, not allow.

        This is the core regression: the old code consulted the empty in-memory
        store and returned False (valid) for every token.
        """
        svc = TokenService()
        svc._store = _RaisingStore()
        svc._degraded = True

        with patch.object(type(svc), "store", _RaisingStore()):
            assert svc.is_token_revoked("some-jti") is True

    def test_degraded_uses_db_verdict_for_refresh_token(self):
        """A refresh token's `revoked_at` is authoritative while degraded."""
        svc = TokenService()
        svc._degraded = True

        revoked = _Row(revoked_at=datetime.now(UTC))
        live = _Row(revoked_at=None)

        with patch.object(type(svc), "store", _RaisingStore()):
            assert svc.is_token_revoked("jti", db=cast(Session, _DbReturning(revoked))) is True
            assert svc.is_token_revoked("jti", db=cast(Session, _DbReturning(live))) is False

    def test_degraded_access_token_denied_when_all_sessions_revoked(self):
        """An access token dies with the user's last live refresh token.

        Access tokens have no `refresh_token` row, so the fallback infers from the
        user's sessions: they had some, all are revoked => logout-all / password
        reset happened => deny.
        """
        svc = TokenService()
        svc._degraded = True

        revoked = _Row(revoked_at=datetime.now(UTC))
        with patch.object(type(svc), "store", _RaisingStore()):
            # jti not found; user HAS tokens; none live => revoked.
            db = _DbSequence([None, revoked, None])
            assert svc.is_token_revoked("access-jti", db=cast(Session, db), user_uuid="u") is True

            # jti not found; user has tokens; one still live => valid.
            live = _Row(revoked_at=None, expires_at=datetime.now(UTC) + timedelta(hours=1))
            db2 = _DbSequence([None, live, live])
            assert svc.is_token_revoked("access-jti", db=cast(Session, db2), user_uuid="u") is False

    def test_user_with_no_refresh_tokens_is_not_treated_as_revoked(self):
        """Absence of refresh tokens is NOT evidence of revocation.

        Regression: an earlier version of this fallback denied whenever the user had
        no live refresh token, which also denied users whose auth path never mints
        one — locking out valid sessions every time Redis blinked. It broke
        `test_websocket_auth_accepts_active_user_with_valid_token`. Deny requires
        positive evidence: the user had sessions and all of them were revoked.
        """
        svc = TokenService()
        svc._degraded = True

        with patch.object(type(svc), "store", _RaisingStore()):
            # jti not found, and the user has no refresh tokens at all.
            db = _DbSequence([None, None])
            assert svc.is_token_revoked("access-jti", db=cast(Session, db), user_uuid="u") is False

    def test_degraded_access_token_without_user_denies(self):
        """Cannot identify the subject while degraded => deny."""
        svc = TokenService()
        svc._degraded = True

        with patch.object(type(svc), "store", _RaisingStore()):
            assert svc.is_token_revoked("access-jti", db=cast(Session, _DbReturning(None))) is True

    def test_db_failure_while_degraded_denies(self):
        """If the authoritative fallback itself fails, deny rather than allow."""
        svc = TokenService()
        svc._degraded = True

        with patch.object(type(svc), "store", _RaisingStore()):
            assert (
                svc.is_token_revoked("jti", db=cast(Session, _DbRaising()), user_uuid="u") is True
            )

    def test_redis_failing_mid_flight_switches_to_db(self):
        """A store that starts healthy and then raises must fall back, not 500."""
        svc = TokenService()
        svc._degraded = False

        with patch.object(type(svc), "store", _RaisingStore()):
            revoked = _Row(revoked_at=datetime.now(UTC))
            assert svc.is_token_revoked("jti", db=cast(Session, _DbReturning(revoked))) is True
            assert svc.degraded is True, "must latch the degraded flag"


# ---------------------------------------------------------------------------
# Fakes — this exercises the degraded decision path, not SQL or Redis.
# ---------------------------------------------------------------------------


class _RaisingStore:
    """Stands in for Redis being unreachable at lookup time."""

    def get(self, _key):
        raise ConnectionError("redis down")


class _Row:
    def __init__(self, revoked_at=None, expires_at=None):
        self.revoked_at = revoked_at
        self.expires_at = expires_at


class _Q:
    def __init__(self, db):
        self._db = db

    def filter(self, *_a, **_k):
        return self

    def join(self, *_a, **_k):
        return self

    def first(self):
        return self._db._next_result()


class _DbReturning:
    """Returns `first` on the first query and `second` on the next."""

    def __init__(self, first, second=None):
        self._results = [first, second]
        self._i = 0

    def _next_result(self):
        r = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return r

    def query(self, *_a, **_k):
        return _Q(self)


class _DbSequence:
    """Returns each queued result in turn, then repeats the last.

    The access-token path issues up to three queries: the jti lookup, the
    "does this user have any refresh token" probe, then the "is any of them
    live" probe.
    """

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    def _next_result(self):
        r = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return r

    def query(self, *_a, **_k):
        return _Q(self)


class _DbRaising:
    def query(self, *_a, **_k):
        raise RuntimeError("database unavailable")
