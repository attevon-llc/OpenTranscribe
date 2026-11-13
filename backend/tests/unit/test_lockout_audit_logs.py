"""The account-lockout audit trail is a control, not decoration (NIST AC-7, issue #1373).

``auth/lockout.py`` is the only place a failed-login streak, a lockout, a lost
compare-and-set race and a Redis degradation are recorded. An operator alerting on
"Account locked" or "falling back to in-memory" reads exactly those log lines, so a
mutant that blanks, corrupts or mis-attributes one of them leaves the control green and
the audit trail wrong. These tests pin, for each event, the **level**, the **logger
name**, a **stable key phrase** and the **masked identifier** -- never the full string, so
a reword that keeps the meaning does not break them.

Each test drives the public ``check_and_record_attempt`` (or the audit-only entry point)
on the real code path of the event: the in-memory path while Redis is down, and the
compare-and-set path against a Redis stand-in.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest

from app.auth import lockout
from app.auth import lockout_store
from app.core.config import settings

LOGGER = "app.auth.lockout"
ALICE = "alice@example.com"
ALICE_MASKED = "a***@example.com"  # mask_identifier(ALICE); mask_identifier(None) is "***"


@pytest.fixture(autouse=True)
def lockout_settings(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_DURATION_MINUTES", 15)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES", 1440)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_PROGRESSIVE", True)


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    monkeypatch.setattr(lockout_store, "_redis_client", None)
    monkeypatch.setattr(lockout_store, "_in_memory_store", None)
    monkeypatch.setattr(lockout_store, "_store_initialized", False)
    monkeypatch.setattr(lockout, "_cas_script", None)
    monkeypatch.setattr(lockout, "_cas_script_client", None)
    monkeypatch.setattr(lockout_store, "_last_redis_probe", 0.0)


class _CasRedis:
    """Redis stand-in: GET, ``set`` and a compare-and-set script, plus a hook to lose races."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.always_lose = False
        self.interleave_once: Any = None
        self.register_error: Exception | None = None

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    def pipeline(self, transaction: bool = True):  # selects the Redis path via hasattr
        raise AssertionError("lockout must use the CAS script, not a pipeline")

    def register_script(self, src: str, /):
        if self.register_error is not None:
            raise self.register_error
        return self._run

    def _run(self, keys: list[str], args: list[Any]) -> list[Any]:
        key = keys[0]
        expected, new_value, _ttl, expect_missing = args
        if self.interleave_once is not None:
            hook, self.interleave_once = self.interleave_once, None
            hook(self)
        current = self.data.get(key)
        if self.always_lose:
            return [0, current or ""]
        if expect_missing == "1":
            if current is not None:
                return [0, current]
        elif current != expected:
            return [0, current or ""]
        self.data[key] = new_value
        return [1, ""]


@pytest.fixture
def memory_store(monkeypatch) -> lockout_store.InMemoryLockoutStore:
    store = lockout_store.InMemoryLockoutStore()
    monkeypatch.setattr(lockout, "_get_store", lambda: store)
    return store


@pytest.fixture
def redis(monkeypatch) -> _CasRedis:
    fake = _CasRedis()
    monkeypatch.setattr(lockout, "_get_store", lambda: fake)
    return fake


def _events(caplog, level: int) -> list[str]:
    """Messages the lockout module logged at exactly ``level``."""
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == level]


def _has(caplog, level: int, *phrases: str) -> bool:
    return any(all(p in m for p in phrases) for m in _events(caplog, level))


def _expired_lock_record(failed_attempts: int = 3) -> str:
    rec = lockout.LockoutRecord(identifier=ALICE, failed_attempts=failed_attempts)
    rec.set_locked_until(datetime.now(UTC) - timedelta(minutes=1))
    return json.dumps(rec.to_dict())


def _fail(n: int) -> None:
    for _ in range(n):
        lockout.check_and_record_attempt(ALICE, success=False)


# ── in-memory path (the degraded-mode store) ─────────────────────────────────────


class TestMemoryPathAudit:
    def test_a_failed_attempt_is_logged_with_the_masked_account_and_the_count(
        self, caplog, memory_store
    ):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        _fail(1)
        assert _has(caplog, logging.INFO, "Failed login attempt for", ALICE_MASKED, "attempt 1/3")

    def test_reaching_the_threshold_logs_the_lockout_at_warning(self, caplog, memory_store):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        _fail(3)
        assert _has(caplog, logging.WARNING, "Account locked", ALICE_MASKED, "lockout #1")

    def test_an_attempt_on_a_locked_account_is_logged_at_warning(self, caplog, memory_store):
        _fail(3)
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=False)
        assert _has(caplog, logging.WARNING, "Login attempt on locked account", ALICE_MASKED)

    def test_a_success_after_failures_logs_how_many_were_cleared(self, caplog, memory_store):
        _fail(1)
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert _has(caplog, logging.INFO, "Successful login for", ALICE_MASKED, "clearing 1 failed")

    def test_a_success_with_nothing_to_clear_logs_nothing(self, caplog, memory_store):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert not _has(caplog, logging.INFO, "Successful login")

    def test_a_success_after_a_lockout_expired_still_logs_the_clear(self, caplog, memory_store):
        memory_store._data[lockout._lockout_key(ALICE)] = _expired_lock_record()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert _has(caplog, logging.INFO, "Successful login for", ALICE_MASKED, "clearing 0")


# ── compare-and-set path (the distributed store) ─────────────────────────────────


class TestRedisPathAudit:
    def test_a_failed_attempt_is_logged_with_the_masked_account_and_the_count(self, caplog, redis):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        _fail(1)
        assert _has(caplog, logging.INFO, "Failed login attempt for", ALICE_MASKED, "attempt 1/3")

    def test_reaching_the_threshold_logs_the_lockout_at_warning(self, caplog, redis):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        _fail(3)
        assert _has(caplog, logging.WARNING, "Account locked", ALICE_MASKED, "lockout #1")

    def test_an_attempt_on_a_locked_account_is_logged_at_warning(self, caplog, redis):
        _fail(3)
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=False)
        assert _has(caplog, logging.WARNING, "Login attempt on locked account", ALICE_MASKED)

    def test_a_success_after_failures_logs_how_many_were_cleared(self, caplog, redis):
        _fail(2)
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert _has(caplog, logging.INFO, "Successful login for", ALICE_MASKED, "clearing 2 failed")

    def test_a_single_failure_is_still_reported_when_it_is_cleared(self, caplog, redis):
        _fail(1)
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert _has(caplog, logging.INFO, "Successful login for", ALICE_MASKED, "clearing 1 failed")

    def test_a_success_with_nothing_to_clear_logs_nothing(self, caplog, redis):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert not _has(caplog, logging.INFO, "Successful login")

    def test_a_success_after_a_lockout_expired_still_logs_the_clear(self, caplog, redis):
        redis.data[lockout._lockout_key(ALICE)] = _expired_lock_record()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=True)
        assert _has(caplog, logging.INFO, "Successful login for", ALICE_MASKED, "clearing 0")

    def test_a_lost_race_is_logged_at_debug_with_the_masked_account(self, caplog, redis):
        _fail(1)

        redis.interleave_once = lambda fake: fake.data.__setitem__(
            lockout._lockout_key(ALICE),
            json.dumps(lockout.LockoutRecord(identifier=ALICE, failed_attempts=2).to_dict()),
        )
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=False)
        assert _has(caplog, logging.DEBUG, "changed concurrently", ALICE_MASKED)
        assert any(
            m.endswith("recomputing from the winning value") for m in _events(caplog, logging.DEBUG)
        )

    def test_exhausting_the_cas_retries_warns_and_falls_back_to_memory(self, caplog, redis):
        redis.always_lose = True
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=False)
        assert _has(
            caplog,
            logging.WARNING,
            "Lockout write for",
            ALICE_MASKED,
            f"lost {lockout.CAS_MAX_RETRIES} compare-and-set races",
            "falling back to in-memory tracking",
        )

    def test_a_redis_failure_warns_with_the_cause_and_the_traceback(self, caplog, redis):
        redis.register_error = ConnectionError("redis is down for the test")
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        lockout.check_and_record_attempt(ALICE, success=False)
        degraded = [
            r
            for r in caplog.records
            if r.name == LOGGER
            and r.levelno == logging.WARNING
            and "Redis lockout transaction failed" in r.getMessage()
        ]
        assert len(degraded) == 1
        assert "falling back to in-memory" in degraded[0].getMessage()
        assert "redis is down for the test" in degraded[0].getMessage()
        assert degraded[0].exc_info and degraded[0].exc_info[0] is ConnectionError


# ── audit-only path (super admin, exempt from lockout) ───────────────────────────


def test_an_exempt_super_admin_failure_is_still_audited_with_the_masked_account(
    caplog, memory_store
):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    is_locked, _ = lockout.check_and_record_attempt(ALICE, success=False, exempt_from_lockout=True)
    assert is_locked is False
    assert _has(
        caplog,
        logging.WARNING,
        "Super admin account",
        ALICE_MASKED,
        "attempt #1",
        "exempt from lockout",
    )
