"""Account-lockout atomicity and Redis-failure degradation (#284).

Two regressions are pinned here:

1. The "atomic check-and-record" Redis path was not atomic. ``redis.Redis.watch()``
   issues WATCH on a pooled connection while ``redis.Redis.pipeline()`` acquires a
   *different* connection for its MULTI/EXEC, so the WATCH guarded nothing and the
   sequence was a plain read-modify-write. Two concurrent failed logins could both
   read ``failed_attempts = 4`` and both write 5, so the threshold never tripped.

2. The Redis error path handed ``_get_store()`` — which returns the *Redis* client —
   to ``_check_and_record_attempt_memory``, which immediately touches ``store._lock``.
   A ``redis.Redis`` has no such attribute, so a Redis hiccup raised AttributeError
   out of ``check_and_record_attempt`` and every login returned HTTP 500.
"""

# mypy: disable-error-code="assignment,no-any-return,operator"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.auth import lockout
from app.core.config import settings


class _FakeRedis:
    """Redis stand-in implementing GET plus the compare-and-set script contract.

    Only the operations the lockout path is allowed to use are implemented. ``watch``
    and ``pipeline`` exist purely to flag a regression back to the non-atomic shape.
    """

    def __init__(self):
        self.data: dict[str, str] = {}
        self.cas_calls: list[dict] = []
        self.writes: list[str] = []
        self.script_src: str | None = None
        self.pipeline_used = False
        self.watch_used = False
        #: Callable invoked once, inside the script, to simulate a concurrent writer.
        self.interleave = None

    # -- operations the lockout path may use ---------------------------------

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Unconditional write — used only by the audit-only and admin paths."""
        self.data[key] = value
        self.writes.append(value)

    def register_script(self, src: str):
        self.script_src = src
        return self._run_script

    def _run_script(self, keys, args):
        """Emulate ``_CAS_LUA``: write only if the key still holds ``expected``."""
        key = keys[0]
        expected, new_value, ttl, expect_missing = args
        self.cas_calls.append(
            {"expected": expected, "new": new_value, "ttl": ttl, "expect_missing": expect_missing}
        )

        if self.interleave is not None:
            hook, self.interleave = self.interleave, None
            hook(self)

        current = self.data.get(key)
        if expect_missing == "1":
            if current is not None:
                return [0, current]
        elif current != expected:
            return [0, current or ""]

        self.data[key] = new_value
        self.writes.append(new_value)
        return [1, ""]

    # -- operations that must NOT be used ------------------------------------

    def pipeline(self, transaction: bool = True):
        self.pipeline_used = True
        raise AssertionError("lockout must not write through a pipeline on another connection")

    def watch(self, key):
        self.watch_used = True
        raise AssertionError("WATCH on a pooled connection cannot guard the pipeline's write")


class _BrokenRedis(_FakeRedis):
    """A Redis client that fails the moment the transaction path is entered."""

    def register_script(self, src: str):
        raise ConnectionError("redis down")


@pytest.fixture(autouse=True)
def lockout_settings(monkeypatch):
    """Pin lockout tunables so dev/CI env overrides cannot change the expectations."""
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_DURATION_MINUTES", 15)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES", 1440)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_PROGRESSIVE", True)


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Clear the module singletons so each test starts on a known store."""
    monkeypatch.setattr(lockout, "_redis_client", None)
    monkeypatch.setattr(lockout, "_in_memory_store", None)
    monkeypatch.setattr(lockout, "_store_initialized", False)
    monkeypatch.setattr(lockout, "_cas_script", None)
    monkeypatch.setattr(lockout, "_cas_script_client", None)


def _use(monkeypatch, client) -> None:
    """Install ``client`` as the active store without probing a real Redis."""
    monkeypatch.setattr(lockout, "_redis_client", client)
    monkeypatch.setattr(lockout, "_store_initialized", True)


def _record(fake: _FakeRedis, identifier: str) -> dict:
    return json.loads(fake.data[lockout._lockout_key(identifier)])


def _stored_record(identifier: str, **fields) -> str:
    record = lockout.LockoutRecord(identifier=identifier, **fields)
    return json.dumps(record.to_dict())


# ── 1. atomicity ────────────────────────────────────────────────────────────────


def test_concurrent_failed_attempts_cannot_write_the_same_count(monkeypatch):
    """The core race: a losing writer must recompute, not overwrite with a stale count."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "race@example.com"
    key = lockout._lockout_key(identifier)

    # Another replica records its own failure after we read, before we write.
    def interloper(store: _FakeRedis) -> None:
        store.data[key] = _stored_record(identifier, failed_attempts=1)

    fake.interleave = interloper

    is_locked, _ = lockout.check_and_record_attempt(identifier, success=False)

    assert is_locked is False
    assert len(fake.cas_calls) == 2, "the losing write must be retried, not applied"
    assert _record(fake, identifier)["failed_attempts"] == 2, (
        "two concurrent failures must count as two, not one"
    )


def test_write_is_conditional_on_the_exact_value_read(monkeypatch):
    """Every write carries the value it was computed from, so a stale write is refused."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "conditional@example.com"

    lockout.check_and_record_attempt(identifier, success=False)
    first = fake.cas_calls[0]
    assert first["expect_missing"] == "1", "a first attempt must require the key to be absent"

    lockout.check_and_record_attempt(identifier, success=False)
    second = fake.cas_calls[1]
    assert second["expect_missing"] == "0"
    assert json.loads(second["expected"])["failed_attempts"] == 1, (
        "the write must be conditional on the record this attempt actually read"
    )


def test_read_and_write_happen_inside_one_server_side_script(monkeypatch):
    """A pooled-connection WATCH guards nothing, so the GET and SET must share a script."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)

    lockout.check_and_record_attempt("script@example.com", success=False)

    assert fake.script_src is not None, "the Redis path must register a CAS script"
    assert "redis.call('GET'" in fake.script_src
    assert "redis.call('SET'" in fake.script_src
    assert fake.watch_used is False
    assert fake.pipeline_used is False


def test_losing_writer_recomputes_from_the_winning_record(monkeypatch):
    """The retry must start from the winner's state, including its lockout_count."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "recompute@example.com"
    key = lockout._lockout_key(identifier)
    fake.data[key] = _stored_record(identifier, failed_attempts=1)

    def interloper(store: _FakeRedis) -> None:
        store.data[key] = _stored_record(identifier, failed_attempts=2, lockout_count=1)

    fake.interleave = interloper

    is_locked, unlock_time = lockout.check_and_record_attempt(identifier, success=False)

    stored = _record(fake, identifier)
    assert is_locked is True, "attempt 3 of 3 must lock once the fresh count is read"
    assert stored["failed_attempts"] == 3
    # lockout_count 1 -> second lockout -> 2x base duration.
    assert unlock_time is not None
    assert stored["lockout_count"] == 2


# ── 2. Redis failure must degrade, not 500 ─────────────────────────────────────


def test_redis_failure_does_not_raise_attribute_error(monkeypatch):
    """The regression: the fallback used to receive the Redis client itself."""
    _use(monkeypatch, _BrokenRedis())

    is_locked, unlock_time = lockout.check_and_record_attempt("degrade@example.com", success=False)

    assert is_locked is False
    assert unlock_time is None


def test_redis_failure_falls_back_to_a_real_in_memory_store(monkeypatch):
    """Degraded mode must still count attempts and still lock at the threshold."""
    _use(monkeypatch, _BrokenRedis())
    identifier = "degrade-count@example.com"

    results = [lockout.check_and_record_attempt(identifier, success=False)[0] for _ in range(3)]

    assert results == [False, False, True]
    assert isinstance(lockout._in_memory_store, lockout.InMemoryLockoutStore)


def test_degradation_is_counted_for_alerting(monkeypatch):
    """Silent fallback to per-process lockout state is the thing worth alerting on."""
    metrics = pytest.importorskip("app.core.metrics")
    counter = metrics.security_state_degraded_total.labels(
        control="account_lockout", fallback="local"
    )
    before = counter._value.get()

    _use(monkeypatch, _BrokenRedis())
    lockout.check_and_record_attempt("degrade-metric@example.com", success=False)

    assert counter._value.get() == before + 1


# ── 3. existing behaviour is preserved ─────────────────────────────────────────


def test_threshold_reached_locks_the_account(monkeypatch):
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "threshold@example.com"

    results = [lockout.check_and_record_attempt(identifier, success=False) for _ in range(3)]

    assert [locked for locked, _ in results] == [False, False, True]
    assert results[-1][1] is not None
    assert lockout.get_lockout_info(identifier)["is_locked"] is True


def test_locked_account_short_circuits_without_writing(monkeypatch):
    """A locked account must not have its counter bumped by further attempts."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "locked@example.com"

    for _ in range(3):
        lockout.check_and_record_attempt(identifier, success=False)
    writes_when_locked = len(fake.writes)

    is_locked, unlock_time = lockout.check_and_record_attempt(identifier, success=False)

    assert is_locked is True
    assert unlock_time is not None
    assert len(fake.writes) == writes_when_locked
    assert _record(fake, identifier)["failed_attempts"] == 3


def test_progressive_durations_and_sticky_lockout_count(monkeypatch):
    """Durations follow 1x/2x/4x then cap, and lockout_count survives expiry."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "progressive@example.com"
    key = lockout._lockout_key(identifier)

    now = datetime(2026, 1, 1, tzinfo=UTC)
    durations = []

    for _ in range(4):
        for _ in range(settings.ACCOUNT_LOCKOUT_THRESHOLD):
            is_locked, unlock_time = lockout._check_and_record_attempt_redis(
                fake, key, identifier, success=False, now=now
            )
        durations.append(round((unlock_time - now).total_seconds() / 60))
        # Jump past this lockout so the next round starts from an expired record.
        now = unlock_time + timedelta(minutes=1)

    assert durations == [15, 30, 60, 1440]
    assert _record(fake, identifier)["lockout_count"] == 4


def test_successful_login_clears_the_counter(monkeypatch):
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "clears@example.com"

    lockout.check_and_record_attempt(identifier, success=False)
    lockout.check_and_record_attempt(identifier, success=False)
    assert _record(fake, identifier)["failed_attempts"] == 2

    is_locked, unlock_time = lockout.check_and_record_attempt(identifier, success=True)

    assert (is_locked, unlock_time) == (False, None)
    stored = _record(fake, identifier)
    assert stored["failed_attempts"] == 0
    assert stored["locked_until"] is None


def test_successful_login_preserves_lockout_count(monkeypatch):
    """Progressive escalation must not be resettable by simply logging in."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "sticky@example.com"
    fake.data[lockout._lockout_key(identifier)] = _stored_record(
        identifier, failed_attempts=2, lockout_count=2
    )

    lockout.check_and_record_attempt(identifier, success=True)

    assert _record(fake, identifier)["lockout_count"] == 2


def test_exempt_account_is_recorded_but_never_locked(monkeypatch):
    """Super admins keep emergency access; attempts are audit-only (NIST AC-7)."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    identifier = "exempt@example.com"

    for _ in range(6):
        is_locked, unlock_time = lockout.check_and_record_attempt(
            identifier, success=False, exempt_from_lockout=True
        )
        assert (is_locked, unlock_time) == (False, None)

    stored = _record(fake, identifier)
    assert stored["failed_attempts"] == 6
    assert stored["locked_until"] is None
    assert lockout.get_lockout_info(identifier)["is_locked"] is False


def test_lockout_disabled_records_nothing(monkeypatch):
    fake = _FakeRedis()
    _use(monkeypatch, fake)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_ENABLED", False)

    assert lockout.check_and_record_attempt("off@example.com", success=False) == (False, None)
    assert fake.data == {}


def test_ttl_outlives_the_maximum_lockout(monkeypatch):
    """A record that expires mid-lockout would silently unlock the account."""
    fake = _FakeRedis()
    _use(monkeypatch, fake)

    lockout.check_and_record_attempt("ttl@example.com", success=False)

    ttl_seconds = int(fake.cas_calls[0]["ttl"])
    assert ttl_seconds > settings.ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES * 60
