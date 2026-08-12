"""The lockout sweep must delete only DEAD records (NIST AC-7).

``cleanup_expired_lockouts`` is the periodic memory reclaim for the in-memory lockout
store. Its one predicate decides what a "dead" record is::

    if is_expired and (last_activity is None or last_activity < cleanup_threshold):

Turn that ``and`` into an ``or`` and the sweep starts deleting **live** state:

* a record whose lock is still in force is dropped, so the account is instantly unlocked;
* every expired record is dropped regardless of how recently it was attacked, so
  ``failed_attempts`` and — the expensive one — ``lockout_count`` reset.

``lockout_count`` is what makes lockout *progressive* (1x, 2x, 4x, then the max). Losing
it on every sweep means an attacker who waits out one base lockout is always back to a
base lockout, forever: the escalation control is gone while the dashboard still reports
lockouts happening. ``rg cleanup_expired_lockouts backend/`` found no caller and no test
outside ``lockout.py`` itself before this file.

The store is a real :class:`~app.auth.lockout.InMemoryLockoutStore` — the backend this
function exists for — so no Redis is involved and the assertions are about the records,
not about a mock.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest

from app.auth import lockout as lockout_module
from app.auth.lockout import LOCKOUT_PREFIX
from app.auth.lockout import InMemoryLockoutStore
from app.auth.lockout import LockoutRecord
from app.auth.lockout import cleanup_expired_lockouts

#: The sweep's own idle window. Anything more recent than this is live state.
IDLE_HOURS = 24


@pytest.fixture
def store(monkeypatch) -> InMemoryLockoutStore:
    """A fresh in-memory lockout store, with lockout enforcement switched on."""
    from app.core.auth_settings import publish_process_auth_setting

    publish_process_auth_setting("account_lockout_enabled", True)
    backing = InMemoryLockoutStore()
    monkeypatch.setattr(lockout_module, "_get_store", lambda: backing)
    return backing


def _write(store: InMemoryLockoutStore, record: LockoutRecord) -> None:
    """Persist *record* under the key the module itself derives."""
    store.set(lockout_module._lockout_key(record.identifier), json.dumps(record.to_dict()))


def _read(store: InMemoryLockoutStore, identifier: str) -> LockoutRecord:
    raw = store.get(lockout_module._lockout_key(identifier))
    assert raw is not None, f"record for {identifier} is gone"
    return LockoutRecord.from_dict(json.loads(raw))


def _exists(store: InMemoryLockoutStore, identifier: str) -> bool:
    return store.get(lockout_module._lockout_key(identifier)) is not None


def _record(
    identifier: str,
    *,
    locked_until: datetime | None,
    last_attempt_hours_ago: float,
    lockout_count: int = 3,
) -> LockoutRecord:
    """A record as ``check_and_record_attempt`` would have left it."""
    now = datetime.now(UTC)
    last = now - timedelta(hours=last_attempt_hours_ago)
    return LockoutRecord(
        identifier=identifier,
        failed_attempts=5,
        lockout_count=lockout_count,
        locked_until=locked_until.isoformat() if locked_until else None,
        first_failed_attempt=(last - timedelta(minutes=5)).isoformat(),
        last_failed_attempt=last.isoformat(),
    )


def _expired_and_idle(identifier: str = "idle@example.com") -> LockoutRecord:
    return _record(
        identifier,
        locked_until=datetime.now(UTC) - timedelta(hours=2),
        last_attempt_hours_ago=48,
    )


def _live_lockout(identifier: str = "locked@example.com") -> LockoutRecord:
    """A long progressive lockout: still in force, last attempt beyond the idle window.

    Not a contrived shape — ``ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES`` defaults to 1440
    (24 h), so the fourth lockout of an account is locked for longer than the sweep's
    idle window. That is exactly the record an ``or`` would delete.
    """
    return _record(
        identifier,
        locked_until=datetime.now(UTC) + timedelta(hours=12),
        last_attempt_hours_ago=30,
    )


def _recently_attacked(identifier: str = "recent@example.com") -> LockoutRecord:
    """The lock has just expired, but the attempts are minutes old."""
    return _record(
        identifier,
        locked_until=datetime.now(UTC) - timedelta(minutes=1),
        last_attempt_hours_ago=0.1,
    )


class TestOnlyDeadRecordsAreSwept:
    """Consequence prevented: the sweep unlocking accounts and resetting the progressive
    lockout counter on every pass — a lockout bypass that leaves no trace."""

    def test_an_expired_and_idle_record_is_swept(self, store):
        _write(store, _expired_and_idle())

        assert cleanup_expired_lockouts() == 1

    def test_the_swept_record_is_actually_gone(self, store):
        _write(store, _expired_and_idle())

        cleanup_expired_lockouts()

        assert _exists(store, "idle@example.com") is False

    def test_a_live_lockout_is_not_swept(self, store):
        """THE control: an account still serving its lockout keeps serving it."""
        _write(store, _live_lockout())

        assert cleanup_expired_lockouts() == 0

    def test_a_live_lockout_keeps_its_lock_expiry(self, store):
        _write(store, _live_lockout())

        cleanup_expired_lockouts()

        assert _read(store, "locked@example.com").get_locked_until_datetime() is not None

    def test_a_recently_attacked_record_is_not_swept(self, store):
        """Its lock expired, but deleting it would reset the escalation an attacker is
        already climbing."""
        _write(store, _recently_attacked())

        assert cleanup_expired_lockouts() == 0

    def test_the_progressive_lockout_counter_survives(self, store):
        """``lockout_count`` is the whole progressive control; the sweep must not zero it."""
        _write(store, _recently_attacked())

        cleanup_expired_lockouts()

        assert _read(store, "recent@example.com").lockout_count == 3

    def test_a_record_with_no_recorded_activity_is_swept(self, store):
        """Nothing to preserve: no lock, no timestamps — pure garbage from an old format."""
        _write(store, LockoutRecord(identifier="blank@example.com"))

        assert cleanup_expired_lockouts() == 1

    def test_a_mixed_store_keeps_exactly_the_live_records(self, store):
        _write(store, _expired_and_idle())
        _write(store, _live_lockout())
        _write(store, _recently_attacked())

        swept = cleanup_expired_lockouts()

        assert swept == 1
        assert sorted(store.keys(f"{LOCKOUT_PREFIX}*")) == [
            lockout_module._lockout_key("locked@example.com"),
            lockout_module._lockout_key("recent@example.com"),
        ]


class TestTheSweepStaysInsideItsOwnKeyspace:
    """Consequence prevented: dropping the prefix filter and sweeping unrelated keys out
    of a store the rest of the auth plane shares."""

    def test_a_foreign_key_is_left_alone(self, store):
        store.set("revoked:jti:some-token", "revoked")
        _write(store, _expired_and_idle())

        cleanup_expired_lockouts()

        assert store.get("revoked:jti:some-token") == "revoked"

    def test_a_foreign_key_is_not_counted_as_swept(self, store):
        store.set("revoked:jti:some-token", "revoked")

        assert cleanup_expired_lockouts() == 0


class TestTheSweepRespectsItsPreconditions:
    """Consequence prevented: the sweep running where it must not — with lockout
    disabled (nothing should be touched at all) or against Redis, whose TTLs own
    expiry and where reaching into ``_data``/``_lock`` would simply crash."""

    def test_a_disabled_lockout_policy_sweeps_nothing(self, store):
        from app.core.auth_settings import publish_process_auth_setting

        publish_process_auth_setting("account_lockout_enabled", False)
        _write(store, _expired_and_idle())

        assert cleanup_expired_lockouts() == 0

    def test_a_disabled_policy_leaves_the_record_in_place(self, store):
        from app.core.auth_settings import publish_process_auth_setting

        publish_process_auth_setting("account_lockout_enabled", False)
        _write(store, _expired_and_idle())

        cleanup_expired_lockouts()

        assert _exists(store, "idle@example.com") is True

    def test_a_redis_backed_store_is_left_to_its_ttls(self, monkeypatch):
        """Detected by the presence of ``pipeline`` — the Redis-only API."""
        from app.core.auth_settings import publish_process_auth_setting

        publish_process_auth_setting("account_lockout_enabled", True)
        redis_like = _RedisLikeStore()
        monkeypatch.setattr(lockout_module, "_get_store", lambda: redis_like)

        assert cleanup_expired_lockouts() == 0


class _RedisLikeStore:
    """A store that looks like Redis to the sweep: it has ``pipeline``.

    Deliberately has no ``_data``/``_lock``, so a sweep that ignored the branch would
    raise ``AttributeError`` here rather than quietly passing.
    """

    def pipeline(self) -> Any:
        raise AssertionError("the sweep must not open a pipeline")
