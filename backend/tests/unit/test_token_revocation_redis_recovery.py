"""Token revocation leaves its Redis-degraded mode once Redis is reachable again.

``TokenService`` used to latch ``_degraded`` for the life of the process: a Redis outage
at startup, or one failed lookup later, and that replica answered every revocation
question from Postgres until it was restarted, with its blacklist writes going to a
per-process store nobody read. It now re-probes on the same timer the lockout and OIDC
state stores use (``lockout_store.REDIS_REPROBE_SECONDS``).

Two things must hold across the recovery, and both are pinned here:

* Within the re-probe interval nothing changes: still degraded, still the database.
* Clearing the flag must not hand back a token that was revoked while Redis was down.
  Logout-all during the outage wrote its epoch to a store Redis never saw, so a token
  issued before the recovery still gets the database verdict until such tokens expire.
"""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.auth import token_service as token_service_module
from app.auth.lockout_store import REDIS_REPROBE_SECONDS
from app.auth.session import InMemoryStore
from app.auth.token_service import REVOKED_TOKEN_PREFIX
from app.auth.token_service import TokenService
from app.core.config import settings

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000ee"


class _Clock:
    """A per-instance monotonic clock, so no other thread sees a fake time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Probe:
    """Stand-in for ``get_redis_client``: answers from a list, counts its calls."""

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers)
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self._answers[min(self.calls, len(self._answers)) - 1]


class _RaisingRedis:
    """A Redis client whose server has gone away."""

    def get(self, _key: str) -> Any:
        raise ConnectionError("redis down")

    def set(self, *_a: Any, **_k: Any) -> None:
        raise ConnectionError("redis down")


class _Row:
    def __init__(self, revoked_at: datetime | None) -> None:
        self.revoked_at = revoked_at
        self.expires_at = datetime.now(UTC) + timedelta(days=1)


class _Query:
    def __init__(self, result: Any) -> None:
        self._result = result

    def filter(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def join(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def first(self) -> Any:
        return self._result


class _Db:
    """Answers each ``query().first()`` in turn; the SQL filters are not under test."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.queries = 0

    def query(self, *_a: Any, **_k: Any) -> _Query:
        result = self._results[min(self.queries, len(self._results) - 1)]
        self.queries += 1
        return _Query(result)


def _all_sessions_revoked_db() -> Session:
    """The user had a session and every one of them is revoked (logout-all)."""
    return cast(Session, _Db(None, _Row(revoked_at=datetime.now(UTC)), None))


@pytest.fixture(autouse=True)
def _revocation_enabled(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)


def _service(clock: _Clock) -> TokenService:
    svc = TokenService()
    svc._clock = clock
    return svc


def _touch_store(svc: TokenService) -> object:
    """Read the ``store`` property, which is where the re-probe happens."""
    return svc.store


def _degraded_at_startup(clock: _Clock, probe: _Probe) -> TokenService:
    svc = _service(clock)
    with patch.object(token_service_module, "get_redis_client", probe):
        assert svc.store is not None
    assert svc.degraded is True
    return svc


class TestTheDegradedFlagClearsAfterRecovery:
    def test_startup_outage_clears_once_redis_answers_the_reprobe(self):
        clock, recovered = _Clock(), InMemoryStore()
        probe = _Probe(None, recovered)
        svc = _degraded_at_startup(clock, probe)

        clock.now += REDIS_REPROBE_SECONDS
        with patch.object(token_service_module, "get_redis_client", probe):
            store = svc.store

        assert probe.calls == 2
        assert svc.degraded is False
        assert store is recovered

    def test_a_lookup_failure_after_startup_also_recovers(self):
        clock, recovered = _Clock(), InMemoryStore()
        probe = _Probe(_RaisingRedis(), recovered)
        svc = _service(clock)
        with patch.object(token_service_module, "get_redis_client", probe):
            assert svc.is_token_revoked("jti-a", db=cast(Session, _Db(_Row(None)))) is False
            assert svc.degraded is True

            clock.now += REDIS_REPROBE_SECONDS
            _touch_store(svc)

        assert svc.degraded is False
        assert svc.store is recovered

    def test_the_blacklist_is_read_from_redis_again(self):
        """Redis-backed behaviour resumes: a blacklisted JTI is refused although its row is live.

        While degraded the database row (live) is the only answer, so this token passes;
        after recovery the shared blacklist entry is what refuses it.
        """
        clock, recovered = _Clock(), InMemoryStore()
        recovered.set(f"{REVOKED_TOKEN_PREFIX}blacklisted", "revoked", ex=600)
        probe = _Probe(None, recovered)
        svc = _degraded_at_startup(clock, probe)
        live_row = cast(Session, _Db(_Row(revoked_at=None)))
        assert svc.is_token_revoked("blacklisted", db=live_row) is False

        clock.now += REDIS_REPROBE_SECONDS
        with patch.object(token_service_module, "get_redis_client", probe):
            verdict = svc.is_token_revoked("blacklisted", db=cast(Session, _Db(_Row(None))))

        assert verdict is True

    def test_a_token_issued_after_recovery_needs_no_database(self):
        """With Redis back, the database is no longer consulted for new tokens."""
        clock = _Clock()
        probe = _Probe(None, InMemoryStore())
        svc = _degraded_at_startup(clock, probe)
        clock.now += REDIS_REPROBE_SECONDS
        with patch.object(token_service_module, "get_redis_client", probe):
            _touch_store(svc)

        db = _Db(_Row(revoked_at=datetime.now(UTC)))
        verdict = svc.is_token_revoked(
            "new-jti", db=cast(Session, db), user_uuid=USER_UUID, issued_at=int(time.time()) + 2
        )

        assert verdict is False
        assert db.queries == 0


class TestWithinTheIntervalNothingChanges:
    def test_no_reprobe_and_still_degraded_one_second_early(self):
        clock, recovered = _Clock(), InMemoryStore()
        probe = _Probe(None, recovered)
        svc = _degraded_at_startup(clock, probe)

        clock.now += REDIS_REPROBE_SECONDS - 1
        with patch.object(token_service_module, "get_redis_client", probe):
            store = svc.store

        assert probe.calls == 1
        assert svc.degraded is True
        assert store is not recovered

    def test_the_degraded_window_still_answers_from_the_database(self):
        clock = _Clock()
        probe = _Probe(None, InMemoryStore())
        svc = _degraded_at_startup(clock, probe)
        clock.now += REDIS_REPROBE_SECONDS - 1

        with patch.object(token_service_module, "get_redis_client", probe):
            assert svc.is_token_revoked("jti", db=None) is True
            revoked_row = cast(Session, _Db(_Row(revoked_at=datetime.now(UTC))))
            assert svc.is_token_revoked("jti", db=revoked_row) is True

    def test_a_failed_reprobe_stays_degraded_and_waits_another_interval(self):
        clock = _Clock()
        probe = _Probe(None, None, InMemoryStore())
        svc = _degraded_at_startup(clock, probe)

        clock.now += REDIS_REPROBE_SECONDS
        with patch.object(token_service_module, "get_redis_client", probe):
            _touch_store(svc)
            assert svc.degraded is True
            clock.now += REDIS_REPROBE_SECONDS - 1
            _touch_store(svc)

        assert probe.calls == 2
        assert svc.degraded is True


class TestRevocationsFromTheOutageSurviveRecovery:
    def test_logout_all_during_the_outage_still_refuses_an_older_token(self):
        clock = _Clock()
        probe = _Probe(None, InMemoryStore())
        svc = _degraded_at_startup(clock, probe)
        issued = int(time.time()) - 60
        assert svc.stamp_user_revocation_epoch(USER_UUID) is True
        assert (
            svc.is_token_revoked(
                "old-jti", db=_all_sessions_revoked_db(), user_uuid=USER_UUID, issued_at=issued
            )
            is True
        )

        clock.now += REDIS_REPROBE_SECONDS
        with patch.object(token_service_module, "get_redis_client", probe):
            verdict = svc.is_token_revoked(
                "old-jti", db=_all_sessions_revoked_db(), user_uuid=USER_UUID, issued_at=issued
            )

        assert svc.degraded is False
        assert verdict is True

    def test_an_older_token_of_a_user_with_live_sessions_still_passes(self):
        """Control: the post-recovery check refuses only on positive evidence."""
        clock = _Clock()
        probe = _Probe(None, InMemoryStore())
        svc = _degraded_at_startup(clock, probe)
        clock.now += REDIS_REPROBE_SECONDS
        live_sessions = cast(Session, _Db(None, _Row(None), _Row(None)))

        with patch.object(token_service_module, "get_redis_client", probe):
            verdict = svc.is_token_revoked(
                "old-jti", db=live_sessions, user_uuid=USER_UUID, issued_at=int(time.time()) - 60
            )

        assert svc.degraded is False
        assert verdict is False
